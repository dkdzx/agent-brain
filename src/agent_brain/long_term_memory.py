#!/usr/bin/env python3
"""Append-only, provenance-aware long-term memory ledger.

This module does not read raw chat and does not write a host project's
authority files. Promotion is an explicit command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "agent_brain_long_term_memory_v1"
VALID_STATUSES = {"active", "uncertain", "superseded", "rejected", "archived"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_events(root: Path) -> list[dict[str, Any]]:
    path = root / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            if raw.strip():
                events.append(json.loads(raw))
    return events


def verify_chain(events: list[dict[str, Any]]) -> None:
    previous = "GENESIS"
    for seq, event in enumerate(events, 1):
        if event.get("seq") != seq:
            raise ValueError(f"bad sequence at {seq}")
        if event.get("previous_event_hash") != previous:
            raise ValueError(f"bad previous hash at {seq}")
        unhashed = {k: v for k, v in event.items() if k != "event_hash"}
        expected = digest(unhashed)
        if event.get("event_hash") != expected:
            raise ValueError(f"bad event hash at {seq}")
        previous = expected


def append_event(root: Path, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    events = read_events(root)
    verify_chain(events)
    event = {
        "schema_version": SCHEMA_VERSION,
        "seq": len(events) + 1,
        "event_id": "memory-event-" + os.urandom(8).hex(),
        "event_type": event_type,
        "payload": payload,
        "created_at": now_iso(),
        "previous_event_hash": events[-1]["event_hash"] if events else "GENESIS",
    }
    event["event_hash"] = digest(event)
    path = root / "events.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    rebuild_index(root)
    return event


def rebuild_index(root: Path) -> dict[str, Any]:
    events = read_events(root)
    verify_chain(events)
    memories: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event["payload"]
        if event["event_type"] == "MEMORY_ADDED":
            memories[payload["memory_id"]] = dict(payload)
        elif event["event_type"] == "MEMORY_SUPERSEDED":
            old_id = payload["old_memory_id"]
            if old_id in memories:
                memories[old_id]["status"] = "superseded"
                memories[old_id]["invalid_at"] = payload["at"]
                memories[old_id]["superseded_by"] = payload["new_memory_id"]
        elif event["event_type"] == "MEMORY_STATUS_CHANGED":
            memory_id = payload["memory_id"]
            if memory_id in memories:
                memories[memory_id]["status"] = payload["status"]
    index = {
        "schema_version": SCHEMA_VERSION,
        "event_count": len(events),
        "event_chain_head": events[-1]["event_hash"] if events else "GENESIS",
        "memories": memories,
    }
    index["semantic_hash"] = digest(index)
    atomic_json(root / "current_index.json", index)
    return index


def command_add(args: argparse.Namespace) -> dict[str, Any]:
    if args.status not in VALID_STATUSES:
        raise ValueError("invalid status")
    root = Path(args.root)
    index = rebuild_index(root)
    if args.memory_id in index["memories"]:
        raise ValueError("memory_id already exists")
    memory = {
        "memory_id": args.memory_id,
        "author_type": args.author_type,
        "agent_id": args.agent_id,
        "source_thread": args.source_thread,
        "task_id": args.task_id,
        "content": args.content,
        "status": args.status,
        "confidence": args.confidence,
        "created_at": now_iso(),
        "valid_at": args.valid_at or now_iso(),
        "invalid_at": None,
        "evidence_refs": args.evidence_ref or [],
        "supersedes": args.supersedes or [],
        "scope": args.scope or [],
    }
    memory["content_hash"] = digest(memory)
    return append_event(root, "MEMORY_ADDED", memory)


def command_supersede(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    index = rebuild_index(root)
    if args.old_memory_id not in index["memories"]:
        raise ValueError("old memory does not exist")
    if args.new_memory_id not in index["memories"]:
        raise ValueError("new memory does not exist")
    return append_event(
        root,
        "MEMORY_SUPERSEDED",
        {
            "old_memory_id": args.old_memory_id,
            "new_memory_id": args.new_memory_id,
            "at": now_iso(),
        },
    )


def command_query(args: argparse.Namespace) -> dict[str, Any]:
    index = rebuild_index(Path(args.root))
    tokens = [token.casefold() for token in args.text.split() if token.strip()]
    rows = []
    for memory in index["memories"].values():
        haystack = " ".join(
            [
                memory.get("content", ""),
                " ".join(memory.get("scope", [])),
                memory.get("task_id", ""),
            ]
        ).casefold()
        if all(token in haystack for token in tokens):
            rows.append(memory)
    rows.sort(key=lambda row: (-float(row["confidence"]), row["created_at"]))
    return {"count": len(rows[: args.limit]), "memories": rows[: args.limit]}


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    events = read_events(root)
    verify_chain(events)
    rebuilt = rebuild_index(root)
    stored = json.loads((root / "current_index.json").read_text(encoding="utf-8"))
    passed = rebuilt["semantic_hash"] == stored["semantic_hash"]
    return {
        "status": "PASS" if passed else "FAIL",
        "event_count": len(events),
        "memory_count": len(rebuilt["memories"]),
        "semantic_hash": rebuilt["semantic_hash"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", default=str(Path.home() / ".agent-brain" / "memory")
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("--memory-id", required=True)
    add.add_argument("--author-type", required=True)
    add.add_argument("--agent-id", required=True)
    add.add_argument("--source-thread", required=True)
    add.add_argument("--task-id", required=True)
    add.add_argument("--content", required=True)
    add.add_argument("--status", choices=sorted(VALID_STATUSES), required=True)
    add.add_argument("--confidence", type=float, required=True)
    add.add_argument("--valid-at")
    add.add_argument("--evidence-ref", action="append")
    add.add_argument("--supersedes", action="append")
    add.add_argument("--scope", action="append")
    add.set_defaults(handler=command_add)

    supersede = sub.add_parser("supersede")
    supersede.add_argument("--old-memory-id", required=True)
    supersede.add_argument("--new-memory-id", required=True)
    supersede.set_defaults(handler=command_supersede)

    query = sub.add_parser("query")
    query.add_argument("--text", required=True)
    query.add_argument("--limit", type=int, default=20)
    query.set_defaults(handler=command_query)

    verify = sub.add_parser("verify")
    verify.set_defaults(handler=command_verify)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "REJECTED", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

