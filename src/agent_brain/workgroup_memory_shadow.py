#!/usr/bin/env python3
"""Read-only shadow integration for a live workgroup.

The live workgroup runtime used by the project is not an ``agent_brain``
runtime: it is a collection of source projections (group, members, task pool,
snapshot and lane receipts).  This module makes that boundary explicit.  It
copies source bytes into a private shadow directory, imports provenance-only
events into a new append-only ledger, and rebuilds views/cards/slices from that
ledger.

The shadow is deliberately below project authority.  It never writes the
source workgroup, project_control, World/canonical data, or a Graphiti store.
Graphiti output is a review queue and an optional dry-run receipt only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from .workgroup_brain import (
        BrainError,
        build_member_position_cards,
        canonical_bytes,
        compact_context_entry,
        filtered_context,
        materialize_view,
        semantic_hash,
    )
except ImportError:  # pragma: no cover - direct script execution
    from workgroup_brain import (  # type: ignore[no-redef]
        BrainError,
        build_member_position_cards,
        canonical_bytes,
        compact_context_entry,
        filtered_context,
        materialize_view,
        semantic_hash,
    )


SHADOW_SCHEMA = "agent_brain_workgroup_memory_shadow_v1"
SHADOW_EVENT_SCHEMA = "agent_brain_workgroup_shadow_event_v1"
SHADOW_AUTHORITY = "PRIVATE_SHADOW"
SHADOW_CONTEXT_KIND = "bounded_injection_slice"
NORMAL_BUDGET_353K_BYTES = 384 * 1024
HARD_BUDGET_353K_BYTES = 512 * 1024
OFFLINE_EXPORT_BYTES = 1024 * 1024
DEFAULT_MIN_BUDGET_BYTES = 32 * 1024
DEFAULT_BUDGET_LADDER = [
    32 * 1024,
    64 * 1024,
    128 * 1024,
    256 * 1024,
    384 * 1024,
    512 * 1024,
]
ENTRY_TYPES = {
    "FACT_CONFIRMED",
    "HYPOTHESIS",
    "HYPOTHESIS_REJECTED",
    "PARTIAL_RESULT",
    "ARTIFACT_PUBLISHED",
    "EVIDENCE_ATTACHED",
    "QUESTION_OPENED",
    "QUESTION_RESOLVED",
    "CONFLICT_RECORDED",
    "LOCAL_DECISION",
    "SCOPE_WARNING",
    "HANDOFF_READY",
    "CURRENT_BEST_MODEL",
}
CONTROL_EVENT_TYPES = {
    "GROUP_CREATED",
    "MEMBER_ADDED",
    "MEMBER_REMOVED",
    "ENTRY_POSTED",
    "ENTRY_RESOLVED",
    "GROUP_FROZEN",
    "HANDOFF_CREATED",
    "GROUP_CLOSED",
}
FORBIDDEN_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9]{12,}|-----BEGIN [^-]+PRIVATE KEY-----)", re.I
)


class ShadowError(RuntimeError):
    """Closed-world error for the shadow adapter."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> bytes:
    return canonical_bytes(value)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    try:
        return _sha_bytes(path.read_bytes())
    except FileNotFoundError as exc:
        raise ShadowError("SOURCE_MISSING", str(path)) from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ShadowError("SOURCE_MISSING", str(path)) from exc
    except json.JSONDecodeError as exc:
        raise ShadowError("SOURCE_INVALID_JSON", f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ShadowError("SOURCE_NOT_OBJECT", str(path))
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(_json(value) + b"\n")
    os.replace(temp, path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_json(value).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            raise ShadowError("LEDGER_BLANK_LINE", f"{path}:{line_number}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ShadowError("LEDGER_INVALID_JSON", f"{path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ShadowError("LEDGER_ROW_NOT_OBJECT", f"{path}:{line_number}")
        rows.append(value)
    return rows


def _safe_text(value: Any, limit: int = 720) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _assert_no_secret(value: Any) -> None:
    if FORBIDDEN_SECRET.search(_safe_text(value, 100000)):
        raise ShadowError("SECRET_MATERIAL_FORBIDDEN", "shadow output contains credential-like material")


def _relative_source(source_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(source_root.resolve()).as_posix()
    except ValueError as exc:
        raise ShadowError("SOURCE_PATH_OUTSIDE_ROOT", str(path)) from exc


def _source_ref(source_root: Path, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ShadowError("SOURCE_MISSING", str(path))
    raw = path.read_bytes()
    return {
        "source_path": str(path.resolve()),
        "source_relpath": _relative_source(source_root, path),
        "source_sha256": _sha_bytes(raw),
        "source_size": len(raw),
    }


def _source_files(source_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required = [
        source_root / "group.json",
        source_root / "members.json",
        source_root / "task_pool.json",
        source_root / "working_snapshot.json",
    ]
    refs = [_source_ref(source_root, item) for item in required]
    lane_root = source_root / "lanes"
    if not lane_root.is_dir():
        raise ShadowError("SOURCE_MISSING", str(lane_root))
    for path in sorted(lane_root.rglob("*")):
        if not path.is_file():
            continue
        upper = path.name.upper()
        if any(marker in upper for marker in ("HANDOFF", "RECEIPT")):
            refs.append(_source_ref(source_root, path))
    return refs, {item["source_relpath"]: item["source_sha256"] for item in refs}


def _load_source(source_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    refs, _ = _source_files(source_root)
    return (
        _read_json(source_root / "group.json"),
        _read_json(source_root / "members.json"),
        _read_json(source_root / "task_pool.json"),
        _read_json(source_root / "working_snapshot.json"),
        refs,
    )


def _member_id(thread_id: str) -> str:
    if not thread_id:
        raise ShadowError("MEMBER_THREAD_MISSING", "source member has no thread_id")
    return "member-" + thread_id


def _member_role(member: dict[str, Any]) -> str:
    role = str(member.get("role") or "")
    name = str(member.get("display_name") or "")
    if "总控" in role or "总控" in name:
        return "controller"
    if "审查" in role or "验收" in role or "整合" in role:
        return "reviewer"
    if "观察" in role:
        return "observer"
    return "worker"


def _normalize_members(source: dict[str, Any], group_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    rows = source.get("members")
    if not isinstance(rows, list):
        raise ShadowError("SOURCE_SCHEMA_INVALID", "members.json.members must be a list")
    normalized: dict[str, Any] = {}
    thread_to_member: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ShadowError("SOURCE_SCHEMA_INVALID", "member row is not an object")
        thread_id = str(row.get("thread_id") or "")
        member_id = _member_id(thread_id)
        if member_id in normalized:
            raise ShadowError("DUPLICATE_MEMBER", member_id)
        status = str(row.get("status") or "UNKNOWN")
        active = status == "ACTIVE"
        role = _member_role(row)
        normalized[member_id] = {
            "member_id": member_id,
            "role": role,
            "host_id": "shadow-source",
            "thread_id": thread_id,
            "codex_task_title": str(row.get("display_name") or "任务名称待同步"),
            "source_display_name": row.get("display_name"),
            "source_role": row.get("role"),
            "source_status": status,
            "scopes": [f"workgroup/{group_id}"],
            "read_scope": [f"workgroup/{group_id}"],
            "write_scope": [f"workgroup/{group_id}"] if active and role != "observer" else [],
            "token_hash": semantic_hash({"shadow": True, "thread_id": thread_id}),
            "active": active,
            "status": status,
            "added_at": row.get("created_at") or now_iso(),
            "joined_at": row.get("created_at") or now_iso(),
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "revoked_at": None if active else row.get("updated_at"),
            "source_member": dict(row),
        }
        thread_to_member[thread_id] = member_id
    return {"schema_version": "shadow_members_v1", "group_id": group_id, "members": normalized}, thread_to_member


def _normalize_group(
    source: dict[str, Any], snapshot: dict[str, Any], group_id: str, member_doc: dict[str, Any]
) -> dict[str, Any]:
    source_status = str(source.get("status") or "ACTIVE")
    current_goal = str(snapshot.get("current_goal") or source.get("display_name") or "shadow workgroup")
    return {
        "schema_version": "shadow_group_v1",
        "group_id": group_id,
        "task_id": str(source.get("work_package_id") or group_id),
        "objective": current_goal,
        "state": "ACTIVE" if source_status == "ACTIVE" else source_status,
        "status": source_status,
        "scope": f"workgroup/{group_id}",
        "authority_bundle_sha256": str(source.get("task_spec_sha256") or semantic_hash(source)),
        "loopx_goal_id": f"shadow:{source.get('project_id', 'unknown')}:{group_id}",
        "member_registry_sha256": semantic_hash(member_doc),
        "coordination_mode": "strict",
        "context_budget_mode": "adaptive",
        "context_budget_bytes": HARD_BUDGET_353K_BYTES,
        "context_min_budget_bytes": DEFAULT_MIN_BUDGET_BYTES,
        "context_budget_ladder_bytes": DEFAULT_BUDGET_LADDER,
        "context_target_coverage": 0.95,
        "context_min_marginal_gain": 0.03,
        "context_max_entries": 4096,
        "member_position_card_limit": 15,
        "source_group": dict(source),
    }


def _source_projection(
    group: dict[str, Any], members: dict[str, Any], task_pool: dict[str, Any], snapshot: dict[str, Any], refs: list[dict[str, Any]]
) -> dict[str, Any]:
    tasks = task_pool.get("tasks")
    if not isinstance(tasks, list):
        raise ShadowError("SOURCE_SCHEMA_INVALID", "task_pool.json.tasks must be a list")
    return {
        "group_id": group.get("group_id"),
        "project_id": group.get("project_id"),
        "group_status": group.get("status"),
        "member_count": group.get("member_count"),
        "members": [dict(item) for item in members.get("members", [])],
        "tasks": [dict(item) for item in tasks],
        "queued_next_step_tasks": [dict(item) for item in task_pool.get("queued_next_step_tasks", [])],
        "working_snapshot": dict(snapshot),
        "evidence_sources": [
            {
                "source_relpath": item["source_relpath"],
                "source_sha256": item["source_sha256"],
                "source_size": item["source_size"],
            }
            for item in refs
        ],
    }


def _event_key(event: dict[str, Any]) -> str:
    return str((event.get("shadow_import") or {}).get("import_key") or "")


def verify_shadow_events(shadow_root: Path) -> list[dict[str, Any]]:
    events = _read_jsonl(shadow_root / "events.jsonl")
    previous = "GENESIS"
    for seq, event in enumerate(events, 1):
        if event.get("seq") != seq:
            raise ShadowError("EVENT_SEQUENCE_BREAK", f"expected {seq}, got {event.get('seq')}")
        if event.get("prev_event_hash") != previous:
            raise ShadowError("EVENT_CHAIN_BREAK", str(seq))
        if event.get("event_type") not in CONTROL_EVENT_TYPES:
            raise ShadowError("EVENT_TYPE_INVALID", str(event.get("event_type")))
        shadow_import = event.get("shadow_import")
        if not isinstance(shadow_import, dict) or shadow_import.get("authority") != SHADOW_AUTHORITY:
            raise ShadowError("EVENT_AUTHORITY_INVALID", str(seq))
        if not shadow_import.get("source_path") or not shadow_import.get("source_sha256"):
            raise ShadowError("EVENT_SOURCE_PROVENANCE_MISSING", str(seq))
        claimed = event.get("event_hash")
        actual = semantic_hash({key: value for key, value in event.items() if key != "event_hash"})
        if not isinstance(claimed, str) or claimed != actual:
            raise ShadowError("EVENT_HASH_MISMATCH", str(seq))
        previous = claimed
    # The import receipt is a lower-bound checkpoint.  A later runtime event
    # may legitimately extend the ledger, but a shorter ledger proves that a
    # previously imported tail was deleted.
    receipt_path = shadow_root / "import_receipt.json"
    if receipt_path.exists():
        receipt = _read_json(receipt_path)
        expected_count = int(receipt.get("event_count", 0))
        if len(events) < expected_count:
            raise ShadowError(
                "EVENT_LEDGER_TRUNCATED",
                f"expected at least {expected_count} events, found {len(events)}",
            )
    return events


def _append_event(
    shadow_root: Path,
    *,
    event_type: str,
    actor_member_id: str,
    payload: dict[str, Any],
    source_ref: dict[str, Any],
    import_key: str,
    source_thread_id: str | None = None,
    source_task_id: str | None = None,
) -> dict[str, Any]:
    if event_type not in CONTROL_EVENT_TYPES:
        raise ShadowError("EVENT_TYPE_INVALID", event_type)
    events = verify_shadow_events(shadow_root) if (shadow_root / "events.jsonl").exists() else []
    if any(_event_key(event) == import_key for event in events):
        return next(event for event in events if _event_key(event) == import_key)
    source_hash = str(source_ref.get("source_sha256") or "")
    source_path = str(source_ref.get("source_path") or "")
    if not source_hash or not source_path:
        raise ShadowError("SOURCE_PROVENANCE_MISSING", import_key)
    event = {
        "schema_version": SHADOW_EVENT_SCHEMA,
        "seq": len(events) + 1,
        "event_id": f"shadow:{semantic_hash({'group': import_key, 'seq': len(events) + 1})[:24]}",
        "event_type": event_type,
        "actor_member_id": actor_member_id,
        "at": now_iso(),
        "source_thread_id": source_thread_id,
        "source_task_id": source_task_id,
        "payload": payload,
        "prev_event_hash": events[-1]["event_hash"] if events else "GENESIS",
        "shadow_import": {
            **source_ref,
            "authority": SHADOW_AUTHORITY,
            "import_key": import_key,
            "source_kind": "runtime_source_projection",
        },
    }
    _assert_no_secret(event)
    event["event_hash"] = semantic_hash(event)
    _append_jsonl(shadow_root / "events.jsonl", event)
    return event


def _copy_source_snapshot(source_root: Path, shadow_root: Path, refs: list[dict[str, Any]]) -> None:
    snapshot_root = shadow_root / "source_snapshot"
    for ref in refs:
        source = Path(ref["source_path"])
        target = snapshot_root / ref["source_relpath"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _entry(
    *,
    entry_id: str,
    entry_seq: int,
    group_id: str,
    task_id: str,
    entry_type: str,
    scope: str,
    author_member_id: str,
    author_role: str,
    content: dict[str, Any],
    evidence_refs: list[str],
    subject_key: str,
    source_event_key: str,
    status: str = "active",
    confidence: float = 0.8,
) -> dict[str, Any]:
    if entry_type not in ENTRY_TYPES:
        raise ShadowError("ENTRY_TYPE_INVALID", entry_type)
    payload = dict(content)
    return {
        "entry_id": entry_id,
        "entry_seq": entry_seq,
        "group_id": group_id,
        "task_id": task_id,
        "entry_type": entry_type,
        "subject_key": subject_key,
        "scope": scope,
        "author_member_id": author_member_id,
        "author_role": author_role,
        "created_at": now_iso(),
        "status": status,
        "confidence": confidence,
        "content": payload,
        "payload": payload,
        "payload_sha256": semantic_hash(payload),
        "content_hash": semantic_hash(payload),
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "supersedes": [],
        "supersedes_entry_ids": [],
        "source_event_key": source_event_key,
    }


def _source_task_entries(
    source_root: Path,
    shadow_root: Path,
    group: dict[str, Any],
    task_pool: dict[str, Any],
    thread_to_member: dict[str, str],
    normalized_members: dict[str, Any],
    source_ref: dict[str, Any],
) -> list[dict[str, Any]]:
    task_rows = task_pool.get("tasks")
    if not isinstance(task_rows, list):
        raise ShadowError("SOURCE_SCHEMA_INVALID", "tasks is not a list")
    entries: list[dict[str, Any]] = []
    group_id = str(group["group_id"])
    task_id = str(group.get("work_package_id") or group_id)
    scope = f"workgroup/{group_id}"
    for index, task in enumerate(task_rows, 1):
        if not isinstance(task, dict) or not task.get("task_id"):
            raise ShadowError("TASK_SCHEMA_INVALID", f"task row {index}")
        owner_thread = str(task.get("owner_thread_id") or "")
        author = thread_to_member.get(owner_thread)
        if not author:
            author = next(
                (member_id for member_id, member in normalized_members.items() if member["role"] == "controller"),
                "shadow-controller",
            )
        member = normalized_members.get(author, {})
        source_key = f"task:{task['task_id']}:{semantic_hash(task)}"
        entry_id = f"shadow-entry-task-{task['task_id'].lower()}-{semantic_hash(task)[:12]}"
        entry = _entry(
            entry_id=entry_id,
            entry_seq=index,
            group_id=group_id,
            task_id=task_id,
            entry_type="FACT_CONFIRMED",
            scope=scope,
            author_member_id=author,
            author_role=str(member.get("role") or "worker"),
            content={
                "core_claim": f"任务 {task['task_id']} 的状态镜像为 {task.get('status', 'UNKNOWN')}。",
                "claim": _safe_text(task.get("scope") or task.get("task_id")),
                "strongest_evidence": f"source task_pool.json sha256={source_ref['source_sha256']}",
                "strongest_counterevidence": "源任务池未提供反证字段；不能将镜像状态解释为项目事实。",
                "scope": scope,
                "claim_ceiling": "PRIVATE_SHADOW task-status mirror; no project truth",
                "evidence_status": "diagnostic_valid",
                "model_gate_status": "not_applicable",
                "signing_authority": "none",
                "task": dict(task),
            },
            evidence_refs=[f"{source_ref['source_relpath']}#{source_ref['source_sha256']}"],
            subject_key=f"task/{task['task_id']}",
            source_event_key=source_key,
            confidence=0.92,
        )
        entries.append(entry)
        _append_event(
            shadow_root,
            event_type="ENTRY_POSTED",
            actor_member_id=author,
            payload={"entry": entry, "task": dict(task)},
            source_ref=source_ref,
            import_key=source_key,
            source_thread_id=owner_thread or None,
            source_task_id=str(task.get("task_id")),
        )
    return entries


def _snapshot_entries(
    shadow_root: Path,
    group: dict[str, Any],
    snapshot: dict[str, Any],
    controller_id: str,
    source_ref: dict[str, Any],
    first_entry_seq: int,
) -> list[dict[str, Any]]:
    group_id = str(group["group_id"])
    workgroup_task = str(group.get("work_package_id") or group_id)
    scope = f"workgroup/{group_id}"
    entries: list[dict[str, Any]] = []
    raw_items: list[tuple[str, str, Any]] = []
    raw_items.append(("FACT_CONFIRMED", "goal", snapshot.get("current_goal")))
    for index, value in enumerate(snapshot.get("latest_decisions") or [], 1):
        raw_items.append(("LOCAL_DECISION", f"decision/{index}", value))
    for index, value in enumerate(snapshot.get("open_risks") or [], 1):
        raw_items.append(("SCOPE_WARNING", f"risk/{index}", value))
    for index, value in enumerate(
        [snapshot.get("step5_allowed"), snapshot.get("business_world_write_allowed"), snapshot.get("business_k3_effect")],
        1,
    ):
        raw_items.append(("FACT_CONFIRMED", f"boundary/{index}", value))
    for offset, (entry_type, subject, value) in enumerate(raw_items):
        source_key = f"snapshot:{subject}:{semantic_hash(value)}"
        entry_id = f"shadow-entry-snapshot-{semantic_hash(source_key)[:20]}"
        text = _safe_text(value, 1000)
        entry = _entry(
            entry_id=entry_id,
            entry_seq=first_entry_seq + offset,
            group_id=group_id,
            task_id=workgroup_task,
            entry_type=entry_type,
            scope=scope,
            author_member_id=controller_id,
            author_role="controller",
            content={
                "core_claim": text,
                "strongest_evidence": f"source working_snapshot.json sha256={source_ref['source_sha256']}",
                "strongest_counterevidence": "快照只表达工作组局部状态；项目正式裁决仍在project_control。",
                "claim_ceiling": "PRIVATE_SHADOW workgroup snapshot only",
                "evidence_status": "diagnostic_valid",
                "model_gate_status": "not_applicable",
                "signing_authority": "none",
            },
            evidence_refs=[f"{source_ref['source_relpath']}#{source_ref['source_sha256']}"],
            subject_key=subject,
            source_event_key=source_key,
            confidence=0.9 if entry_type != "SCOPE_WARNING" else 0.84,
        )
        entries.append(entry)
        _append_event(
            shadow_root,
            event_type="ENTRY_POSTED",
            actor_member_id=controller_id,
            payload={"entry": entry, "snapshot_field": subject},
            source_ref=source_ref,
            import_key=source_key,
            source_thread_id=str(group.get("controller_thread_id") or "") or None,
            source_task_id=workgroup_task,
        )
    return entries


def _lane_entries(
    source_root: Path,
    shadow_root: Path,
    group: dict[str, Any],
    lane_refs: Iterable[dict[str, Any]],
    controller_id: str,
    start_seq: int,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    scope = f"workgroup/{group['group_id']}"
    task_id = str(group.get("work_package_id") or group["group_id"])
    for offset, ref in enumerate(lane_refs):
        source_path = Path(ref["source_path"])
        relative = ref["source_relpath"]
        source_key = f"artifact:{relative}:{ref['source_sha256']}"
        artifact = None
        try:
            parsed = json.loads(source_path.read_text(encoding="utf-8"))
            artifact = parsed if isinstance(parsed, dict) else {"row_count": len(parsed)}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            artifact = {"format": "opaque", "size": ref["source_size"]}
        status = _safe_text(artifact.get("verdict") or artifact.get("status") or artifact.get("state") or "recorded", 160)
        entry = _entry(
            entry_id=f"shadow-entry-artifact-{semantic_hash(source_key)[:20]}",
            entry_seq=start_seq + offset,
            group_id=str(group["group_id"]),
            task_id=task_id,
            entry_type="EVIDENCE_ATTACHED",
            scope=scope,
            author_member_id=controller_id,
            author_role="controller",
            content={
                "core_claim": f"已登记来源工件 {relative}，状态={status}。",
                "strongest_evidence": f"sha256={ref['source_sha256']}; size={ref['source_size']}",
                "strongest_counterevidence": "工件仅作为诊断证据引用，不能替代项目正式验收。",
                "claim_ceiling": "evidence reference only",
                "evidence_status": "diagnostic_valid",
                "model_gate_status": "not_applicable",
                "signing_authority": "none",
                "artifact_status": status,
            },
            evidence_refs=[f"{relative}#{ref['source_sha256']}"],
            subject_key=f"artifact/{relative}",
            source_event_key=source_key,
            confidence=0.75,
        )
        entries.append(entry)
        _append_event(
            shadow_root,
            event_type="ENTRY_POSTED",
            actor_member_id=controller_id,
            payload={"entry": entry, "artifact": {"relative": relative, "sha256": ref["source_sha256"]}},
            source_ref=ref,
            import_key=source_key,
            source_thread_id=str(group.get("controller_thread_id") or "") or None,
            source_task_id=task_id,
        )
    return entries


def _ensure_shadow_metadata(
    shadow_root: Path,
    source_root: Path,
    group: dict[str, Any],
    source_members: dict[str, Any],
    task_pool: dict[str, Any],
    snapshot: dict[str, Any],
    refs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, str]]:
    normalized_members, thread_to_member = _normalize_members(source_members, str(group["group_id"]))
    normalized_group = _normalize_group(group, snapshot, str(group["group_id"]), normalized_members)
    _write_json(shadow_root / "group.json", normalized_group)
    _write_json(shadow_root / "members.json", normalized_members)
    _write_json(shadow_root / "task_pool.json", task_pool)
    _write_json(shadow_root / "working_snapshot.json", snapshot)
    projection = _source_projection(group, source_members, task_pool, snapshot, refs)
    _write_json(shadow_root / "source_projection.json", projection)
    _copy_source_snapshot(source_root, shadow_root, refs)
    _write_json(
        shadow_root / "source_manifest.json",
        {
            "schema_version": "agent_brain_shadow_source_manifest_v1",
            "source_root": str(source_root.resolve()),
            "group_id": group["group_id"],
            "files": refs,
            "source_projection_sha256": semantic_hash(projection),
            "generated_at": now_iso(),
        },
    )
    # Callers need the member map for attribution; the on-disk document keeps
    # the wrapper/schema above intact.
    return normalized_members["members"], thread_to_member


def rebuild_shadow_view(shadow_root: Path) -> dict[str, Any]:
    """Rebuild all projections from the shadow append-only event ledger."""
    verify_shadow_events(shadow_root)
    view = materialize_view(shadow_root)
    events = verify_shadow_events(shadow_root)
    members = view.get("members", [])
    cards = build_member_position_cards(
        view.get("entries", []),
        members,
        limit=max(1, int(view.get("context_policy", {}).get("member_position_card_limit", 15))),
    )
    member_titles = {
        member.get("member_id"): member.get("codex_task_title") or "任务名称待同步"
        for member in members
    }
    for card in cards:
        if not card.get("codex_task_title"):
            card["codex_task_title"] = member_titles.get(card.get("member_id"), "任务名称待同步")
    source_manifest = _read_json(shadow_root / "source_manifest.json")
    candidates = _read_jsonl(shadow_root / "group_memory_candidates.jsonl")
    pending = _read_jsonl(shadow_root / "GRAPHITI_PENDING_IMPORT_REQUESTS.jsonl")
    imported = _read_jsonl(shadow_root / "GRAPHITI_EPISODE_RECEIPTS.jsonl")
    receipts = _read_jsonl(shadow_root / "INJECTION_RECEIPTS.jsonl")
    view["member_position_cards"] = cards
    view["shadow_memory"] = {
        "schema_version": SHADOW_SCHEMA,
        "authority": SHADOW_AUTHORITY,
        "source_projection_sha256": source_manifest.get("source_projection_sha256"),
        "event_chain_head": events[-1]["event_hash"] if events else "GENESIS",
        "event_count": len(events),
        "position_card_count": len(cards),
        "active_position_card_count": sum(1 for card in cards if card.get("member_active")),
        "historical_position_card_count": sum(1 for card in cards if not card.get("member_active")),
        "candidate_count": len(candidates),
        "graphiti_pending_count": len(pending),
        "graphiti_import_receipt_count": len(imported),
        "injection_receipt_count": len(receipts),
        "context_recovery": {
            "project_level_regeneration_supported": True,
            "codex_platform_compression_hook_controlled": False,
            "codex_platform_consumption_unknown": True,
        },
        "promotion": {
            "automatic_project_promotion": False,
            "automatic_long_term_memory_write": False,
            "project_control_written": False,
        },
    }
    hash_payload = {key: value for key, value in view.items() if key != "generated_at"}
    view["semantic_hash"] = semantic_hash(hash_payload)
    _write_json(shadow_root / "position_cards.json", {"cards": cards, "view_version": view["view_version"]})
    _write_json(shadow_root / "view.json", view)
    return view


def import_source_group(source_root: Path, shadow_root: Path) -> dict[str, Any]:
    """Import source projections into an independent shadow, idempotently."""
    source_root = source_root.resolve()
    shadow_root = shadow_root.resolve()
    if source_root == shadow_root or shadow_root.is_relative_to(source_root):
        raise ShadowError("SHADOW_MUST_BE_OUTSIDE_SOURCE", str(shadow_root))
    group, source_members, task_pool, snapshot, refs = _load_source(source_root)
    group_id = str(group.get("group_id") or "")
    if not group_id:
        raise ShadowError("GROUP_ID_MISSING", str(source_root))
    shadow_root.mkdir(parents=True, exist_ok=True)
    normalized_members, thread_to_member = _ensure_shadow_metadata(
        shadow_root, source_root, group, source_members, task_pool, snapshot, refs
    )
    events = verify_shadow_events(shadow_root) if (shadow_root / "events.jsonl").exists() else []
    controller_thread = str(group.get("controller_thread_id") or "")
    controller_id = thread_to_member.get(controller_thread)
    if not controller_id:
        controller_id = next(
            (member_id for member_id, member in normalized_members.items() if member["role"] == "controller"),
            "shadow-controller",
        )
    group_ref = next(item for item in refs if item["source_relpath"] == "group.json")
    members_ref = next(item for item in refs if item["source_relpath"] == "members.json")
    task_ref = next(item for item in refs if item["source_relpath"] == "task_pool.json")
    snapshot_ref = next(item for item in refs if item["source_relpath"] == "working_snapshot.json")
    _append_event(
        shadow_root,
        event_type="GROUP_CREATED",
        actor_member_id=controller_id,
        payload={"group": {"group_id": group_id, "status": group.get("status"), "project_id": group.get("project_id")}},
        source_ref=group_ref,
        import_key=f"group:{group_ref['source_sha256']}",
        source_thread_id=controller_thread or None,
        source_task_id=str(group.get("work_package_id") or group_id),
    )
    for member in source_members.get("members", []):
        thread_id = str(member.get("thread_id") or "")
        member_ref_key = f"member:{thread_id}:{semantic_hash(member)}"
        _append_event(
            shadow_root,
            event_type="MEMBER_ADDED",
            actor_member_id=controller_id,
            payload={"member": dict(member), "member_id": thread_to_member.get(thread_id)},
            source_ref=members_ref,
            import_key=member_ref_key,
            source_thread_id=thread_id,
            source_task_id=str(group.get("work_package_id") or group_id),
        )
    _source_task_entries(source_root, shadow_root, group, task_pool, thread_to_member, normalized_members, task_ref)
    task_count = len(task_pool.get("tasks") or [])
    _snapshot_entries(shadow_root, group, snapshot, controller_id, snapshot_ref, task_count + 1)
    lane_refs = [ref for ref in refs if ref["source_relpath"].startswith("lanes/")]
    _lane_entries(source_root, shadow_root, group, lane_refs, controller_id, task_count + 1 + 1 + len(snapshot.get("latest_decisions") or []) + len(snapshot.get("open_risks") or []) + 3)
    view = rebuild_shadow_view(shadow_root)
    receipt = {
        "schema_version": "agent_brain_shadow_import_receipt_v1",
        "status": "PASS",
        "authority": SHADOW_AUTHORITY,
        "source_root": str(source_root),
        "shadow_root": str(shadow_root),
        "group_id": group_id,
        "source_file_count": len(refs),
        "event_count": len(verify_shadow_events(shadow_root)),
        "event_chain_head": view["event_chain_head"],
        "source_projection_sha256": view["shadow_memory"]["source_projection_sha256"],
        "idempotent": True,
        "raw_source_bytes_modified": False,
        "project_control_written": False,
        "generated_at": now_iso(),
    }
    _write_json(shadow_root / "import_receipt.json", receipt)
    return receipt


def source_projection(shadow_root: Path) -> dict[str, Any]:
    return _read_json(shadow_root / "source_projection.json")


def external_reader_verify(source_root: Path, shadow_root: Path) -> dict[str, Any]:
    """Independent-reader check: source projection and shadow projection match."""
    group, members, task_pool, snapshot, refs = _load_source(source_root.resolve())
    expected = _source_projection(group, members, task_pool, snapshot, refs)
    actual = source_projection(shadow_root.resolve())
    if semantic_hash(expected) != semantic_hash(actual):
        raise ShadowError("SOURCE_PROJECTION_MISMATCH", "source and shadow projection differ")
    events = verify_shadow_events(shadow_root.resolve())
    view = _read_json(shadow_root.resolve() / "view.json")
    source_manifest = _read_json(shadow_root.resolve() / "source_manifest.json")
    if source_manifest.get("source_projection_sha256") != semantic_hash(expected):
        raise ShadowError("SOURCE_MANIFEST_HASH_MISMATCH", "source manifest does not match reader")
    task_entries = [
        event for event in events
        if event.get("event_type") == "ENTRY_POSTED"
        and isinstance(event.get("payload", {}).get("task"), dict)
    ]
    evidence_entries = [
        event for event in events
        if event.get("event_type") == "ENTRY_POSTED"
        and isinstance(event.get("payload", {}).get("entry"), dict)
        and event["payload"]["entry"].get("entry_type") == "EVIDENCE_ATTACHED"
    ]
    result = {
        "status": "PASS",
        "group_id": expected["group_id"],
        "tasks_source": len(expected["tasks"]),
        "tasks_imported": len(task_entries),
        "members_source": len(expected["members"]),
        "members_imported": len(view.get("members", [])),
        "evidence_source_files": len(expected["evidence_sources"]),
        "evidence_events": len(evidence_entries),
        "event_chain_head": events[-1]["event_hash"] if events else "GENESIS",
        "view_version": view.get("view_version"),
        "source_projection_sha256": semantic_hash(expected),
        "raw_source_bytes_modified": False,
        "project_control_written": False,
    }
    _write_json(shadow_root / "external_reader_receipt.json", result)
    return result


def _find_member(view: dict[str, Any], *, member_id: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
    for member in view.get("members", []):
        if member_id and member.get("member_id") == member_id:
            return member
        if thread_id and member.get("thread_id") == thread_id:
            return member
    raise ShadowError("MEMBER_NOT_FOUND", member_id or thread_id or "")


def _context_budget(
    *, model_window_tokens: int | None, remaining_token_reserve: int | None, offline_export: bool
) -> tuple[int, str, list[int]]:
    if offline_export:
        return OFFLINE_EXPORT_BYTES, "offline_export", DEFAULT_BUDGET_LADDER + [OFFLINE_EXPORT_BYTES]
    if model_window_tokens is None:
        base = HARD_BUDGET_353K_BYTES
        mode = "unknown_model_window_compatibility"
    elif model_window_tokens == 353000:
        base = NORMAL_BUDGET_353K_BYTES
        mode = "known_353k_normal"
    else:
        base = min(HARD_BUDGET_353K_BYTES, max(NORMAL_BUDGET_353K_BYTES, model_window_tokens * 4 // 2))
        mode = "known_window_compatibility"
    if remaining_token_reserve is not None:
        if remaining_token_reserve < 0:
            raise ShadowError("REMAINING_RESERVE_INVALID", str(remaining_token_reserve))
        if model_window_tokens is not None:
            available = max(DEFAULT_MIN_BUDGET_BYTES, (model_window_tokens - remaining_token_reserve) * 4)
            base = min(base, available)
            mode += "_reserve_adjusted"
    base = max(DEFAULT_MIN_BUDGET_BYTES, min(HARD_BUDGET_353K_BYTES, base))
    ladder = sorted({item for item in DEFAULT_BUDGET_LADDER if item <= base} | {base})
    return base, mode, ladder


def build_injection_slice(
    shadow_root: Path,
    *,
    target_thread_id: str,
    requested_scope: str | None = None,
    expected_view_version: int | None = None,
    expected_event_chain_head: str | None = None,
    model_window_tokens: int | None = 353000,
    remaining_token_reserve: int | None = None,
    offline_export: bool = False,
    controller_injected: bool = False,
) -> dict[str, Any]:
    """Regenerate a bounded slice before a continuation/recovery message.

    ``controller_injected`` is intentionally an explicit caller assertion.  A
    generated receipt never implies that the Codex platform consumed it.
    """
    shadow_root = shadow_root.resolve()
    events = verify_shadow_events(shadow_root)
    view = _read_json(shadow_root / "view.json")
    if expected_view_version is not None and view.get("view_version") != expected_view_version:
        raise ShadowError("CONTEXT_STALE", f"expected view {expected_view_version}, current {view.get('view_version')}")
    if expected_event_chain_head is not None and view.get("event_chain_head") != expected_event_chain_head:
        raise ShadowError("CONTEXT_STALE", "event chain head changed")
    member = _find_member(view, thread_id=target_thread_id)
    scope = requested_scope or f"workgroup/{view['group_id']}"
    if scope not in member.get("read_scope", []) and f"workgroup/{view['group_id']}" not in member.get("read_scope", []):
        raise ShadowError("CONTEXT_SCOPE_DENIED", scope)
    budget, mode, ladder = _context_budget(
        model_window_tokens=model_window_tokens,
        remaining_token_reserve=remaining_token_reserve,
        offline_export=offline_export,
    )
    view_copy = json.loads(json.dumps(view, ensure_ascii=False))
    policy = dict(view_copy.get("context_policy") or {})
    policy.update(
        {
            "budget_bytes": budget,
            "minimum_budget_bytes": DEFAULT_MIN_BUDGET_BYTES,
            "budget_ladder_bytes": ladder,
            "mode": "fixed" if offline_export else "adaptive",
        }
    )
    view_copy["context_policy"] = policy
    context = filtered_context(view_copy, member, scope)
    # Keep the core compaction contract explicit at this boundary.  The native
    # builder already compacts entries; this is idempotent and protects older
    # adapters that return source entries directly.
    context["entries"] = [
        compact_context_entry(entry) for entry in context.get("entries", [])
    ]
    context["context_kind"] = SHADOW_CONTEXT_KIND
    context["shadow_authority"] = SHADOW_AUTHORITY
    context["recovery"] = {
        "regenerated_before_continuation": True,
        "controller_injected": bool(controller_injected),
        "codex_platform_consumption_unknown": True,
    }
    included = [entry.get("entry_id") for entry in context.get("entries", []) if entry.get("entry_id")]
    omitted = list(context.get("retrieval", {}).get("omitted_entry_id_sample") or [])
    receipt = {
        "schema_version": "agent_brain_injection_receipt_v1",
        "receipt_id": f"injection:{semantic_hash({'thread': target_thread_id, 'view': view['view_version'], 'time': now_iso()})[:24]}",
        "group_id": view["group_id"],
        "target_thread": target_thread_id,
        "requested_scope": scope,
        "slice_hash": semantic_hash(context),
        "source_event_chain_head": events[-1]["event_hash"] if events else "GENESIS",
        "view_version": view["view_version"],
        "included_entry_ids": included,
        "omitted_entry_ids": omitted,
        "included_count": len(included),
        "omitted_count": len(omitted),
        "bytes": len(_json(context)),
        "approx_tokens": (len(_json(context)) + 3) // 4,
        "budget_bytes": budget,
        "budget_mode": mode,
        "offline_export": offline_export,
        "injection_status": "CONTROLLER_ASSERTED_INJECTED" if controller_injected else "GENERATED_NOT_INJECTED",
        "injection_state": "SENT_BY_CONTROLLER" if controller_injected else "GENERATED_ONLY",
        "platform_consumption_state": "PLATFORM_CONSUMPTION_UNKNOWN",
        "codex_platform_consumption_unknown": True,
        "generated_at": now_iso(),
    }
    _assert_no_secret(context)
    _append_jsonl(shadow_root / "INJECTION_RECEIPTS.jsonl", receipt)
    _write_json(shadow_root / "latest_injection_context.json", context)
    rebuild_shadow_view(shadow_root)
    return {"context": context, "receipt": receipt}


def get_entry(shadow_root: Path, entry_id: str) -> dict[str, Any]:
    for event in verify_shadow_events(shadow_root.resolve()):
        if event.get("event_type") == "ENTRY_POSTED":
            entry = event.get("payload", {}).get("entry")
            if isinstance(entry, dict) and entry.get("entry_id") == entry_id:
                return {
                    "entry": entry,
                    "event_id": event["event_id"],
                    "source": event["shadow_import"],
                    "exact": True,
                }
    raise ShadowError("ENTRY_NOT_FOUND", entry_id)


def recover_shadow_view(shadow_root: Path) -> dict[str, Any]:
    """Recover projections after view/context memory is destroyed."""
    if not (shadow_root / "events.jsonl").exists():
        raise ShadowError("RECOVERY_SOURCE_MISSING", str(shadow_root / "events.jsonl"))
    view = rebuild_shadow_view(shadow_root)
    receipt = {
        "schema_version": "agent_brain_shadow_recovery_receipt_v1",
        "status": "PASS",
        "raw_events_lost": 0,
        "view_rebuilt_from_event_chain": True,
        "event_chain_head": view["event_chain_head"],
        "view_version": view["view_version"],
        "codex_platform_compression_hook_controlled": False,
        "generated_at": now_iso(),
    }
    _write_json(shadow_root / "RECOVERY_RECEIPT.json", receipt)
    return receipt


def checkpoint_memory(shadow_root: Path, *, reason: str, force: bool = True) -> dict[str, Any]:
    """Append candidate memories; never replace prior candidates."""
    shadow_root = shadow_root.resolve()
    view = _read_json(shadow_root / "view.json")
    events = verify_shadow_events(shadow_root)
    entries = [
        item for item in view.get("entries", [])
        if item.get("entry_type") in {"FACT_CONFIRMED", "LOCAL_DECISION", "CONFLICT_RECORDED", "QUESTION_OPENED", "SCOPE_WARNING", "EVIDENCE_ATTACHED"}
    ]
    if not entries:
        raise ShadowError("NO_CANDIDATE_MATERIAL", reason)
    entries.sort(key=lambda item: (item.get("entry_seq", 0), item.get("entry_id", "")), reverse=True)
    selected = entries[:8]
    source = selected[0]
    subject_key = str(source.get("subject_key") or "workgroup/current")
    content_lines = [
        f"checkpoint={reason}; scope={source.get('scope')}; claim ceiling=PRIVATE_SHADOW only.",
        *[
            f"[{item.get('entry_type')}] {_safe_text((item.get('content') or {}).get('core_claim') or (item.get('content') or {}).get('text'), 360)}"
            for item in reversed(selected)
        ],
    ]
    content = "\n".join(content_lines)
    evidence_refs = list(dict.fromkeys(ref for item in selected for ref in item.get("evidence_refs", [])))[:12]
    entry_ids = [str(item["entry_id"]) for item in reversed(selected)]
    previous = _read_jsonl(shadow_root / "group_memory_candidates.jsonl")
    previous_same = [item for item in previous if item.get("subject_key") == subject_key]
    supersedes = previous_same[-1].get("memory_id") if previous_same else None
    candidate_base = {
        "memory_id": f"groupmem:{view['group_id']}:{semantic_hash({'reason': reason, 'entries': entry_ids, 'content': content})[:24]}",
        "group_id": view["group_id"],
        "task_id": view["task_id"],
        "source_thread": source.get("thread_id") or next((member.get("thread_id") for member in view.get("members", []) if member.get("member_id") == source.get("author_member_id")), None),
        "source_member": source.get("author_member_id"),
        "content": content,
        "status": "candidate",
        "confidence": round(sum(float(item.get("confidence") or 0.0) for item in selected) / len(selected), 4),
        "valid_at": source.get("created_at") or now_iso(),
        "invalid_at": None,
        "evidence_refs": evidence_refs,
        "entry_ids": entry_ids,
        "source_event_ids": [
            event["event_id"] for event in events if event.get("event_type") == "ENTRY_POSTED" and event.get("payload", {}).get("entry", {}).get("entry_id") in set(entry_ids)
        ],
        "supersedes": supersedes,
        "scope": source.get("scope"),
        "claim_ceiling": "PRIVATE_SHADOW candidate; requires host-control-plane review",
        "subject_key": subject_key,
        "checkpoint_reason": reason,
    }
    _assert_no_secret(candidate_base)
    candidate_hash = semantic_hash(candidate_base)
    existing_memory = next(
        (item for item in previous if item.get("memory_id") == candidate_base["memory_id"]),
        None,
    )
    if existing_memory is not None:
        candidate = existing_memory
    elif any(item.get("hash") == candidate_hash for item in previous):
        candidate = next(item for item in previous if item.get("hash") == candidate_hash)
    else:
        candidate = {**candidate_base, "hash": candidate_hash, "generated_at": now_iso()}
        _append_jsonl(shadow_root / "group_memory_candidates.jsonl", candidate)
    all_candidates = _read_jsonl(shadow_root / "group_memory_candidates.jsonl")
    _write_json(
        shadow_root / "group_memory_candidates.json",
        {
            "schema_version": "agent_brain_group_memory_candidates_v1",
            "group_id": view["group_id"],
            "automatic_project_promotion": False,
            "candidates": all_candidates,
            "candidate_count": len(all_candidates),
        },
    )
    _write_json(
        shadow_root / "group_memory_index.json",
        {
            "schema_version": "agent_brain_group_memory_index_v1",
            "group_id": view["group_id"],
            "event_chain_head": view["event_chain_head"],
            "candidates": [item.get("memory_id") for item in all_candidates],
            "promoted_project_memory_ids": [],
            "project_control_written": False,
            "semantic_hash": semantic_hash({"candidates": all_candidates, "group_id": view["group_id"]}),
        },
    )
    generate_graphiti_requests(shadow_root)
    rebuild_shadow_view(shadow_root)
    return candidate


def generate_graphiti_requests(shadow_root: Path) -> dict[str, Any]:
    shadow_root = shadow_root.resolve()
    candidates = _read_jsonl(shadow_root / "group_memory_candidates.jsonl")
    existing = _read_jsonl(shadow_root / "GRAPHITI_PENDING_IMPORT_REQUESTS.jsonl")
    existing_ids = {item.get("candidate_id") for item in existing}
    for candidate in candidates:
        candidate_id = candidate.get("memory_id")
        if candidate_id in existing_ids:
            continue
        request = {
            "schema_version": "agent_brain_graphiti_pending_request_v1",
            "request_id": f"graphiti-request:{semantic_hash({'candidate': candidate_id})[:24]}",
            "candidate_id": candidate_id,
            "project_id": "shadow-only:" + str(candidate.get("group_id")),
            "workgroup_id": candidate.get("group_id"),
            "status": "PENDING_REVIEW",
            "episode_body": _safe_text(candidate.get("content"), 2400),
            "reference_time": candidate.get("valid_at"),
            "metadata": {
                "task_id": candidate.get("task_id"),
                "source_thread": candidate.get("source_thread"),
                "confidence": candidate.get("confidence"),
                "scope": candidate.get("scope"),
                "evidence_refs": candidate.get("evidence_refs", []),
                "claim_ceiling": candidate.get("claim_ceiling"),
            },
            "live_writes_performed": 0,
            "generated_at": now_iso(),
        }
        _assert_no_secret(request)
        request["request_hash"] = semantic_hash(request)
        _append_jsonl(shadow_root / "GRAPHITI_PENDING_IMPORT_REQUESTS.jsonl", request)
    pending = _read_jsonl(shadow_root / "GRAPHITI_PENDING_IMPORT_REQUESTS.jsonl")
    approvals = _read_jsonl(shadow_root / "GRAPHITI_APPROVALS.jsonl")
    receipts = _read_jsonl(shadow_root / "GRAPHITI_EPISODE_RECEIPTS.jsonl")
    queue = {
        "schema_version": "agent_brain_graphiti_review_queue_v1",
        "group_id": _read_json(shadow_root / "group.json")["group_id"],
        "pending": pending,
        "approved_candidate_ids": [item.get("candidate_id") for item in approvals],
        "episode_receipt_ids": [item.get("receipt_id") for item in receipts],
        "live_writes_performed": 0,
        "project_control_written": False,
    }
    _write_json(shadow_root / "GRAPHITI_REVIEW_QUEUE.json", queue)
    return queue


def approve_graphiti_candidate(shadow_root: Path, *, memory_id: str, reviewed_by: str) -> dict[str, Any]:
    candidates = _read_jsonl(shadow_root.resolve() / "group_memory_candidates.jsonl")
    if not any(item.get("memory_id") == memory_id for item in candidates):
        raise ShadowError("CANDIDATE_NOT_FOUND", memory_id)
    approvals = _read_jsonl(shadow_root.resolve() / "GRAPHITI_APPROVALS.jsonl")
    if any(item.get("candidate_id") == memory_id for item in approvals):
        return next(item for item in approvals if item.get("candidate_id") == memory_id)
    approval = {
        "schema_version": "agent_brain_graphiti_approval_v1",
        "approval_id": f"graphiti-approval:{semantic_hash({'memory_id': memory_id, 'reviewed_by': reviewed_by})[:24]}",
        "candidate_id": memory_id,
        "reviewed_by": reviewed_by,
        "status": "APPROVED_FOR_EXPLICIT_IMPORT_COMMAND",
        "project_control_write_allowed": False,
        "approved_at": now_iso(),
    }
    approval["approval_hash"] = semantic_hash(approval)
    _append_jsonl(shadow_root.resolve() / "GRAPHITI_APPROVALS.jsonl", approval)
    generate_graphiti_requests(shadow_root)
    return approval


def import_approved_graphiti_candidate(
    shadow_root: Path, *, memory_id: str, importer: Callable[[dict[str, Any]], dict[str, Any]] | None = None
) -> dict[str, Any]:
    approvals = _read_jsonl(shadow_root.resolve() / "GRAPHITI_APPROVALS.jsonl")
    if not any(item.get("candidate_id") == memory_id for item in approvals):
        raise ShadowError("GRAPHITI_APPROVAL_REQUIRED", memory_id)
    candidate = next(
        (item for item in _read_jsonl(shadow_root.resolve() / "group_memory_candidates.jsonl") if item.get("memory_id") == memory_id),
        None,
    )
    if candidate is None:
        raise ShadowError("CANDIDATE_NOT_FOUND", memory_id)
    if importer is None:
        result = {"status": "APPROVED_NOT_IMPORTED_NO_LIVE_ADAPTER", "live_writes_performed": 0}
    else:
        result = dict(importer(candidate))
        result.setdefault("live_writes_performed", 1)
    receipt = {
        "schema_version": "agent_brain_graphiti_episode_receipt_v1",
        "receipt_id": f"graphiti-episode:{semantic_hash({'memory_id': memory_id, 'at': now_iso()})[:24]}",
        "candidate_id": memory_id,
        "group_id": candidate.get("group_id"),
        "status": result.get("status"),
        "live_writes_performed": int(result.get("live_writes_performed", 0)),
        "project_control_written": False,
        "episode_result": result,
        "created_at": now_iso(),
    }
    _assert_no_secret(receipt)
    _append_jsonl(shadow_root.resolve() / "GRAPHITI_EPISODE_RECEIPTS.jsonl", receipt)
    generate_graphiti_requests(shadow_root)
    rebuild_shadow_view(shadow_root)
    return receipt


def record_shadow_task_status(
    shadow_root: Path, *, task_id: str, status: str, owner_thread_id: str | None = None
) -> dict[str, Any]:
    view = _read_json(shadow_root.resolve() / "view.json")
    member = _find_member(view, thread_id=owner_thread_id) if owner_thread_id else next(
        (item for item in view["members"] if item.get("role") == "controller"), view["members"][0]
    )
    source_ref = {
        "source_path": str((shadow_root / "shadow://task-status").resolve()),
        "source_relpath": "shadow://task-status",
        "source_sha256": semantic_hash({"task_id": task_id, "status": status, "owner": owner_thread_id}),
        "source_size": 0,
    }
    entry = _entry(
        entry_id=f"shadow-entry-task-status-{semantic_hash(source_ref)[:20]}",
        entry_seq=len(view.get("entries", [])) + 1,
        group_id=view["group_id"],
        task_id=view["task_id"],
        entry_type="PARTIAL_RESULT",
        scope=f"workgroup/{view['group_id']}",
        author_member_id=member["member_id"],
        author_role=member["role"],
        content={
            "core_claim": f"任务 {task_id} shadow status={status}",
            "strongest_evidence": "controller-generated shadow status event",
            "strongest_counterevidence": "not project truth",
            "claim_ceiling": "PRIVATE_SHADOW only",
            "evidence_status": "diagnostic_valid",
            "model_gate_status": "not_applicable",
            "signing_authority": "none",
            "task_status": status,
        },
        evidence_refs=["shadow://task-status"],
        subject_key=f"task/{task_id}",
        source_event_key=f"shadow-task-status:{task_id}:{status}",
    )
    event = _append_event(
        shadow_root.resolve(),
        event_type="ENTRY_POSTED",
        actor_member_id=member["member_id"],
        payload={"entry": entry, "task_id": task_id, "status": status},
        source_ref=source_ref,
        import_key=entry["source_event_key"],
        source_thread_id=owner_thread_id,
        source_task_id=task_id,
    )
    rebuild_shadow_view(shadow_root.resolve())
    return event


def append_shadow_entry(
    shadow_root: Path, *, member_id: str, entry_type: str, content: dict[str, Any], evidence_refs: list[str] | None = None
) -> dict[str, Any]:
    view = _read_json(shadow_root.resolve() / "view.json")
    member = _find_member(view, member_id=member_id)
    source_ref = {
        "source_path": str((shadow_root / "shadow://runtime-entry").resolve()),
        "source_relpath": "shadow://runtime-entry",
        "source_sha256": semantic_hash(content),
        "source_size": len(_json(content)),
    }
    key = f"runtime-entry:{entry_type}:{semantic_hash({'member': member_id, 'content': content})}"
    entry = _entry(
        entry_id=f"shadow-entry-runtime-{semantic_hash(key)[:20]}",
        entry_seq=len(view.get("entries", [])) + 1,
        group_id=view["group_id"],
        task_id=view["task_id"],
        entry_type=entry_type,
        scope=f"workgroup/{view['group_id']}",
        author_member_id=member_id,
        author_role=member.get("role", "worker"),
        content=content,
        evidence_refs=evidence_refs or [],
        subject_key=f"runtime/{entry_type}",
        source_event_key=key,
    )
    event = _append_event(
        shadow_root.resolve(),
        event_type="ENTRY_POSTED",
        actor_member_id=member_id,
        payload={"entry": entry},
        source_ref=source_ref,
        import_key=key,
        source_thread_id=member.get("thread_id"),
        source_task_id=view["task_id"],
    )
    rebuild_shadow_view(shadow_root.resolve())
    return event


def resolve_shadow_entry(shadow_root: Path, *, member_id: str, entry_id: str, resolution: str, status: str = "resolved") -> dict[str, Any]:
    if status not in {"resolved", "rejected", "superseded"}:
        raise ShadowError("RESOLUTION_STATUS_INVALID", status)
    view = _read_json(shadow_root.resolve() / "view.json")
    _find_member(view, member_id=member_id)
    get_entry(shadow_root, entry_id)
    source_ref = {
        "source_path": str((shadow_root / "shadow://resolution").resolve()),
        "source_relpath": "shadow://resolution",
        "source_sha256": semantic_hash({"entry_id": entry_id, "resolution": resolution, "status": status}),
        "source_size": len(resolution.encode("utf-8")),
    }
    event = _append_event(
        shadow_root.resolve(),
        event_type="ENTRY_RESOLVED",
        actor_member_id=member_id,
        payload={"target_entry_id": entry_id, "status": status, "resolution": resolution},
        source_ref=source_ref,
        import_key=f"resolution:{entry_id}:{source_ref['source_sha256']}",
        source_thread_id=_find_member(view, member_id=member_id).get("thread_id"),
        source_task_id=view["task_id"],
    )
    rebuild_shadow_view(shadow_root.resolve())
    return event


def record_shadow_handoff(shadow_root: Path, *, member_id: str, summary: str) -> dict[str, Any]:
    view = _read_json(shadow_root.resolve() / "view.json")
    member = _find_member(view, member_id=member_id)
    payload = {"summary": summary, "view_version": view["view_version"], "handoff_sha256": semantic_hash({"summary": summary, "view": view["view_version"]})}
    source_ref = {
        "source_path": str((shadow_root / "shadow://handoff").resolve()),
        "source_relpath": "shadow://handoff",
        "source_sha256": semantic_hash(payload),
        "source_size": len(_json(payload)),
    }
    event = _append_event(
        shadow_root.resolve(),
        event_type="HANDOFF_CREATED",
        actor_member_id=member_id,
        payload=payload,
        source_ref=source_ref,
        import_key=f"handoff:{payload['handoff_sha256']}",
        source_thread_id=member.get("thread_id"),
        source_task_id=view["task_id"],
    )
    rebuild_shadow_view(shadow_root.resolve())
    return event


def diagnostics_projection(shadow_root: Path) -> dict[str, Any]:
    """Read-only payload for a future/attached frontend panel."""
    view = _read_json(shadow_root.resolve() / "view.json")
    shadow = view.get("shadow_memory", {})
    context = _read_json(shadow_root / "latest_injection_context.json") if (shadow_root / "latest_injection_context.json").exists() else {}
    receipt = _read_jsonl(shadow_root / "INJECTION_RECEIPTS.jsonl")
    latest_receipt = receipt[-1] if receipt else {}
    curve = list(context.get("context_budget_curve") or [])
    selected_budget = (context.get("context_budget") or {}).get("selected_budget_bytes")
    selected_curve = next(
        (row for row in curve if row.get("budget_bytes") == selected_budget),
        {},
    )
    entry_statuses: dict[str, int] = {}
    for entry in view.get("entries", []):
        status = str(entry.get("status") or "unknown")
        entry_statuses[status] = entry_statuses.get(status, 0) + 1
    return {
        "schema_version": "agent_brain_shadow_memory_frontend_projection_v1",
        "group_id": view["group_id"],
        "event_chain": {"head": shadow.get("event_chain_head"), "count": shadow.get("event_count", 0)},
        "position_cards": {"count": shadow.get("position_card_count", 0), "active": shadow.get("active_position_card_count", 0), "historical": shadow.get("historical_position_card_count", 0)},
        "context_slice": {
            "bytes": latest_receipt.get("bytes", 0),
            "approx_tokens": latest_receipt.get("approx_tokens", 0),
            "included": latest_receipt.get("included_count", 0),
            "omitted": latest_receipt.get("omitted_count", 0),
            "selected_budget_bytes": selected_budget or latest_receipt.get("budget_bytes", 0),
            "coverage_score": selected_curve.get("coverage_score"),
            "get_entry_available": True,
            "kind": context.get("context_kind", SHADOW_CONTEXT_KIND),
        },
        "view_memory": {
            "complete_workgroup_memory": True,
            "entry_statuses": entry_statuses,
            "raw_archive_is_authoritative_for_rebuild": True,
        },
        "checkpoints": {"candidate_count": shadow.get("candidate_count", 0)},
        "graphiti": {"pending_review": shadow.get("graphiti_pending_count", 0), "episode_receipts": shadow.get("graphiti_import_receipt_count", 0)},
        "recovery": {**shadow.get("context_recovery", {}), "last_receipt": latest_receipt or None},
        "authority": {"project_control_written": False, "automatic_promotion": False, "codex_platform_consumption_unknown": True},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--source-group-root", required=True)
    imp.add_argument("--shadow-root", required=True)
    imp.set_defaults(handler=lambda args: import_source_group(Path(args.source_group_root), Path(args.shadow_root)))
    reader = sub.add_parser("external-reader")
    reader.add_argument("--source-group-root", required=True)
    reader.add_argument("--shadow-root", required=True)
    reader.set_defaults(handler=lambda args: external_reader_verify(Path(args.source_group_root), Path(args.shadow_root)))
    recover = sub.add_parser("recover")
    recover.add_argument("--shadow-root", required=True)
    recover.set_defaults(handler=lambda args: recover_shadow_view(Path(args.shadow_root)))
    context = sub.add_parser("context")
    context.add_argument("--shadow-root", required=True)
    context.add_argument("--thread-id", required=True)
    context.add_argument("--remaining-token-reserve", type=int)
    context.set_defaults(handler=lambda args: build_injection_slice(Path(args.shadow_root), target_thread_id=args.thread_id, remaining_token_reserve=args.remaining_token_reserve))
    candidate = sub.add_parser("checkpoint-memory")
    candidate.add_argument("--shadow-root", required=True)
    candidate.add_argument("--reason", required=True)
    candidate.set_defaults(handler=lambda args: checkpoint_memory(Path(args.shadow_root), reason=args.reason))
    diagnostic = sub.add_parser("diagnostics")
    diagnostic.add_argument("--shadow-root", required=True)
    diagnostic.set_defaults(handler=lambda args: diagnostics_projection(Path(args.shadow_root)))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (ShadowError, BrainError) as exc:
        print(json.dumps({"status": "REJECTED", "error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
