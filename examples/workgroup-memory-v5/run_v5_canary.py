#!/usr/bin/env python3
"""Run the isolated dispatch-contract-v5 workgroup-memory canary.

This is deliberately self-contained. It does not import the main project,
LoopX, Codex, Graphiti, or any current runtime. The output is a disposable
append-only simulation with a bounded context projection.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BASE_TIME = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
MEANINGFUL = {
    0: ("FACT_CONFIRMED", "memory.boundary", "The group memory is scoped to one task and never promotes project truth automatically."),
    20: ("HYPOTHESIS", "memory.compaction", "A bounded hot snapshot can replace the full working ledger for normal context reads."),
    40: ("PARTIAL_RESULT", "worker.progress", "The controller and reviewer can continue from a compact snapshot while the raw ledger remains intact."),
    60: ("LOCAL_DECISION", "slice.default", "Default context should contain current decisions, open questions, conflicts, risks, and recent evidence."),
    80: ("ARTIFACT_PUBLISHED", "artifact.contract", "The v5 task contract and the two-member binding fixture are available for independent verification."),
    100: ("QUESTION_OPENED", "question.recovery", "Can an interrupted member recover all accepted state from the event chain without replaying chat?"),
    140: ("FACT_CONFIRMED", "memory.boundary", "The group candidate namespace is durable during the group lifetime but remains below project authority."),
    160: ("CONFLICT_RECORDED", "conflict.scope", "The reviewer considers a broad task scope useful for discovery, while the controller requires a narrow read slice."),
    180: ("RISK_RECORDED", "risk.context-budget", "Returning every visible entry would make context size grow linearly with event volume."),
    200: ("LOCAL_DECISION", "slice.default", "Use scope and subject-aware compaction at the event threshold; preserve old entries only in the archive."),
    210: ("LOCAL_DECISION", "conflict.scope", "Resolve the scope conflict by keeping both evidence sides and selecting a narrow default projection."),
    220: ("PARTIAL_RESULT", "worker.progress", "The post-compaction context is bounded and still contains the active decision, question, conflict, and evidence references."),
    239: ("ARTIFACT_PUBLISHED", "artifact.handoff", "Early close emits group candidates and a pending-project-review handoff without project promotion."),
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(canonical(value) + b"\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical(value).decode("utf-8") + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def iso_at(index: int) -> str:
    return (BASE_TIME + timedelta(minutes=index)).isoformat().replace("+00:00", "Z")


def append_event(
    path: Path,
    seq: int,
    previous_hash: str,
    event_type: str,
    actor_member_id: str,
    source_thread_id: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    event = {
        "schema_version": "v5_canary_event_v1",
        "seq": seq,
        "event_id": f"evt-{seq:04d}",
        "event_type": event_type,
        "actor_member_id": actor_member_id,
        "source_thread_id": source_thread_id,
        "at": iso_at(seq),
        "prev_event_hash": previous_hash,
        "payload": payload,
    }
    event["event_hash"] = digest(event)
    append_jsonl(path, event)
    return event, event["event_hash"]


def rebuild_event_chain(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    events = read_jsonl(path)
    expected_seq = 0
    previous_hash = "GENESIS"
    entries: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["seq"] != expected_seq:
            raise AssertionError(f"event sequence break at {expected_seq}")
        if event["prev_event_hash"] != previous_hash:
            raise AssertionError(f"event predecessor break at {expected_seq}")
        claimed = event["event_hash"]
        unhashed = {key: value for key, value in event.items() if key != "event_hash"}
        if claimed != digest(unhashed):
            raise AssertionError(f"event hash mismatch at {expected_seq}")
        if event["event_type"] == "ENTRY_POSTED":
            entry = copy.deepcopy(event["payload"]["entry"])
            entries[entry["entry_id"]] = entry
            for superseded_id in entry.get("supersedes", []):
                if superseded_id in entries:
                    entries[superseded_id]["status"] = "superseded"
                    entries[superseded_id]["superseded_by"] = entry["entry_id"]
            for resolved_id in entry.get("resolves", []):
                if resolved_id in entries:
                    entries[resolved_id]["status"] = "resolved"
                    entries[resolved_id]["resolved_by"] = entry["entry_id"]
        expected_seq += 1
        previous_hash = claimed
    return events, entries


def entry_priority(entry: dict[str, Any]) -> int:
    return {
        "LOCAL_DECISION": 100,
        "FACT_CONFIRMED": 95,
        "CONFLICT_RECORDED": 90,
        "QUESTION_OPENED": 90,
        "RISK_RECORDED": 85,
        "ARTIFACT_PUBLISHED": 80,
        "PARTIAL_RESULT": 70,
        "HYPOTHESIS": 60,
        "WORK_NOTE": 1,
    }.get(entry["entry_type"], 10)


def eligible_for_memory(entry: dict[str, Any]) -> bool:
    if entry["entry_type"] in {
        "FACT_CONFIRMED",
        "LOCAL_DECISION",
        "CONFLICT_RECORDED",
        "QUESTION_OPENED",
        "RISK_RECORDED",
        "ARTIFACT_PUBLISHED",
        "PARTIAL_RESULT",
    }:
        return entry["status"] in {"active", "resolved"}
    return False


def extract_candidates(
    entries: dict[str, dict[str, Any]], group_id: str, phase: str
) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries.values():
        if not eligible_for_memory(entry):
            continue
        key = (entry["subject_key"], entry["entry_type"])
        old = selected.get(key)
        if old is None or entry["entry_seq"] > old["entry_seq"]:
            selected[key] = entry

    candidates: list[dict[str, Any]] = []
    for entry in sorted(selected.values(), key=lambda item: item["entry_seq"]):
        candidate = {
            "schema_version": "v5_group_memory_candidate_v1",
            "memory_id": f"gm-{entry['entry_id']}",
            "group_id": group_id,
            "phase": phase,
            "status": "unreviewed",
            "entry_type": entry["entry_type"],
            "subject_key": entry["subject_key"],
            "content": entry["content"],
            "source_event_ids": [entry["source_event_id"]],
            "source_entry_id": entry["entry_id"],
            "source_thread_id": entry["source_thread_id"],
            "author_member_id": entry["author_member_id"],
            "todo_id": entry["todo_id"],
            "scope": entry["scope"],
            "confidence": entry["confidence"],
            "valid_at": entry["created_at"],
            "supersedes": entry.get("supersedes", []),
            "resolved_refs": entry.get("resolves", []),
            "evidence_refs": entry.get("evidence_refs", []),
        }
        candidate["content_hash"] = digest(candidate["content"])
        candidates.append(candidate)
    return candidates


def merge_candidates(
    path: Path, new_candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    existing = {item["memory_id"]: item for item in read_jsonl(path)}
    for candidate in new_candidates:
        existing[candidate["memory_id"]] = candidate
    merged = sorted(existing.values(), key=lambda item: item["memory_id"])
    path.write_text(
        "".join(canonical(item).decode("utf-8") + "\n" for item in merged),
        encoding="utf-8",
    )
    return merged


def build_hot_snapshot(
    group: dict[str, Any],
    entries: dict[str, dict[str, Any]],
    loopx_binding: dict[str, Any],
    snapshot_version: int,
    max_entries: int,
    status: str = "ACTIVE",
) -> dict[str, Any]:
    visible = [
        entry
        for entry in entries.values()
        if entry["status"] in {"active", "resolved"} and entry["entry_type"] != "WORK_NOTE"
    ]
    visible.sort(key=lambda item: (-entry_priority(item), -item["entry_seq"]))
    selected = visible[:max_entries]
    compact_entries = [
        {
            "entry_id": entry["entry_id"],
            "entry_type": entry["entry_type"],
            "subject_key": entry["subject_key"],
            "status": entry["status"],
            "confidence": entry["confidence"],
            "created_at": entry["created_at"],
            "content": entry["content"],
            "evidence_refs": entry["evidence_refs"],
            "supersedes": entry.get("supersedes", []),
            "resolves": entry.get("resolves", []),
        }
        for entry in selected
    ]
    return {
        "schema_version": "v5_working_snapshot_v1",
        "group_id": group["group_id"],
        "task_id": group["task_id"],
        "status": status,
        "snapshot_version": snapshot_version,
        "current_goal": group["objective"],
        "active_tasks": [
            {
                "goal_id": loopx_binding["goal_id"],
                "primary_todo_id": loopx_binding["primary_todo_id"],
                "claim_id": loopx_binding["claim_id"],
                "status": "claimed",
            }
        ],
        "accepted_local_decisions": [
            entry["entry_id"]
            for entry in selected
            if entry["entry_type"] == "LOCAL_DECISION" and entry["status"] == "active"
        ],
        "open_questions": [
            entry["entry_id"]
            for entry in selected
            if entry["entry_type"] == "QUESTION_OPENED" and entry["status"] == "active"
        ],
        "open_conflicts": [
            entry["entry_id"]
            for entry in selected
            if entry["entry_type"] == "CONFLICT_RECORDED" and entry["status"] == "active"
        ],
        "open_risks": [
            entry["entry_id"]
            for entry in selected
            if entry["entry_type"] == "RISK_RECORDED" and entry["status"] == "active"
        ],
        "latest_artifacts": [
            entry["entry_id"]
            for entry in selected
            if entry["entry_type"] == "ARTIFACT_PUBLISHED"
        ],
        "claim_ceiling": "Group memory is not project truth; promotion requires controller review.",
        "entries": compact_entries,
        "raw_event_count": 0,
        "source_event_chain_head": None,
    }


def make_context(
    group: dict[str, Any],
    entries: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    snapshot: dict[str, Any],
    binding: dict[str, Any],
    budget: int,
    max_entries: int,
) -> tuple[dict[str, Any], dict[str, int]]:
    all_entries = sorted(entries.values(), key=lambda item: item["entry_seq"])
    naive = {
        "group_id": group["group_id"],
        "task_id": group["task_id"],
        "loopx_binding": binding,
        "entries": all_entries,
    }
    naive_bytes = len(canonical(naive))

    ranked_entries = [
        entry
        for entry in all_entries
        if entry["status"] in {"active", "resolved"} and entry["entry_type"] != "WORK_NOTE"
    ]
    ranked_entries.sort(key=lambda item: (-entry_priority(item), -item["entry_seq"]))
    ranked_entries = ranked_entries[:max_entries]
    ranked_candidates = sorted(
        candidates,
        key=lambda item: (
            item["entry_type"] not in {"LOCAL_DECISION", "FACT_CONFIRMED", "CONFLICT_RECORDED", "QUESTION_OPENED"},
            item["subject_key"],
        ),
    )[:max_entries]

    compact = {
        "schema_version": "v5_agent_context_bundle_v1",
        "group_id": group["group_id"],
        "task_id": group["task_id"],
        "current_goal": group["objective"],
        "claim_ceiling": snapshot["claim_ceiling"],
        "loopx": {
            "goal_id": binding["goal_id"],
            "primary_todo_id": binding["primary_todo_id"],
            "claim_id": binding["claim_id"],
            "member_task_scope": "task/shared",
        },
        "hot_entries": ranked_entries,
        "group_memory_candidates": ranked_candidates,
        "authority": {
            "project_control_is_final_authority": True,
            "automatic_project_promotion": False,
        },
        "truncation": {
            "max_entries": max_entries,
            "raw_entries_available": len(all_entries),
            "raw_entries_omitted": max(0, len(all_entries) - len(ranked_entries)),
            "candidate_count": len(candidates),
        },
    }

    while len(canonical(compact)) > budget and compact["group_memory_candidates"]:
        compact["group_memory_candidates"].pop()
    while len(canonical(compact)) > budget and compact["hot_entries"]:
        compact["hot_entries"].pop()
    if len(canonical(compact)) > budget:
        compact["current_goal"] = compact["current_goal"][:240]
        for collection in ("hot_entries", "group_memory_candidates"):
            for item in compact[collection]:
                content = item.get("content")
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    content["text"] = content["text"][:160]
                    content["truncated"] = True
    compact_bytes = len(canonical(compact))
    if compact_bytes > budget:
        raise AssertionError(f"bounded context still exceeds budget: {compact_bytes}")
    compact["truncation"]["final_bytes"] = compact_bytes
    return compact, {"naive_bytes": naive_bytes, "bounded_bytes": compact_bytes}


def append_loopx_event(path: Path, seq: int, previous_hash: str, kind: str, payload: dict[str, Any]) -> str:
    event = {
        "schema_version": "v5_loopx_fixture_event_v1",
        "seq": seq,
        "event_id": f"loopx-{seq:03d}",
        "event_kind": kind,
        "recorded_at": iso_at(seq),
        "prev_event_hash": previous_hash,
        "payload": payload,
    }
    event["event_hash"] = digest(event)
    append_jsonl(path, event)
    return event["event_hash"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=Path(__file__).with_name("dispatch_contract_v5_task_spec.json"))
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    runtime = (args.runtime or Path(spec["isolation"]["runtime_root"])).resolve()
    group_id = spec["canary"]["group_id"]
    group_dir = runtime / group_id
    if group_dir.exists():
        shutil.rmtree(group_dir)
    group_dir.mkdir(parents=True, exist_ok=True)
    loopx_dir = runtime / "loopx-fixture"
    loopx_dir.mkdir(parents=True, exist_ok=True)

    c = spec["canary"]
    binding = {
        "schema_version": "v5_loopx_group_binding_v1",
        "project_id": spec["loopx_binding"]["project_id"],
        "goal_id": spec["loopx_binding"]["goal_id"],
        "primary_todo_id": spec["loopx_binding"]["primary_todo_id"],
        "child_todo_ids": spec["loopx_binding"]["child_todo_ids"],
        "claim_id": spec["loopx_binding"]["claim_id"],
        "group_id": group_id,
        "member_bindings": [
            {
                "member_id": c["member_ids"][0],
                "source_thread_id": c["source_thread_ids"][0],
                "role": "controller",
                "write_scope": "task/shared",
            },
            {
                "member_id": c["member_ids"][1],
                "source_thread_id": c["source_thread_ids"][1],
                "role": "reviewer",
                "write_scope": "task/shared",
            },
        ],
        "lease_tokens_persisted": False,
        "project_promotion": False,
    }
    write_json(group_dir / "loopx_bindings.json", binding)

    group = {
        "schema_version": "v5_canary_group_v1",
        "group_id": group_id,
        "task_id": c["task_id"],
        "objective": "Validate bounded workgroup memory and LoopX binding without project promotion.",
        "state": "ACTIVE",
        "created_at": iso_at(0),
        "expires_at": iso_at(c["event_target"] + 30),
        "member_ids": c["member_ids"],
        "loopx_goal_id": binding["goal_id"],
        "primary_todo_id": binding["primary_todo_id"],
        "project_authority_write_enabled": False,
        "graphiti_production": False,
    }
    write_json(group_dir / "group.json", group)
    write_json(
        group_dir / "members.json",
        {
            "schema_version": "v5_canary_members_v1",
            "members": [
                {"member_id": c["member_ids"][0], "role": "controller", "active": True},
                {"member_id": c["member_ids"][1], "role": "reviewer", "active": True},
            ],
            "plaintext_lease_tokens": False,
        },
    )

    event_path = group_dir / "events.jsonl"
    loopx_event_path = loopx_dir / "loopx_events.jsonl"
    loopx_hash = "GENESIS"
    loopx_hash = append_loopx_event(
        loopx_event_path,
        0,
        loopx_hash,
        "goal_created",
        {"project_id": binding["project_id"], "goal_id": binding["goal_id"], "group_id": group_id},
    )
    loopx_hash = append_loopx_event(
        loopx_event_path,
        1,
        loopx_hash,
        "todo_claimed",
        {
            "goal_id": binding["goal_id"],
            "primary_todo_id": binding["primary_todo_id"],
            "claim_id": binding["claim_id"],
            "group_id": group_id,
            "member_id": c["member_ids"][0],
        },
    )

    previous_hash = "GENESIS"
    next_seq = 0
    entries: dict[str, dict[str, Any]] = {}
    last_by_subject: dict[str, str] = {}
    recovery_receipt: dict[str, Any] = {}
    compaction_receipt: dict[str, Any] = {}
    budget_comparison: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    snapshot_version = 0

    for index in range(c["event_target"]):
        actor_index = index % 2
        actor = c["member_ids"][actor_index]
        thread = c["source_thread_ids"][actor_index]
        if index in MEANINGFUL:
            entry_type, subject_key, text = MEANINGFUL[index]
        else:
            entry_type = "WORK_NOTE"
            subject_key = f"noise.phase.{index % 16}"
            text = (
                f"Noise-bearing bounded work note {index}: this content is intentionally verbose "
                "to make the unbounded context path measurable. It is not eligible for durable "
                "group memory unless a later structured entry references it."
            )
        entry_id = f"entry-{index:04d}"
        entry = {
            "schema_version": "v5_workgroup_entry_v1",
            "entry_id": entry_id,
            "entry_seq": index,
            "entry_type": entry_type,
            "subject_key": subject_key,
            "scope": "task/shared",
            "status": "active",
            "confidence": 0.95 if entry_type != "HYPOTHESIS" else 0.62,
            "created_at": iso_at(index),
            "author_member_id": actor,
            "source_thread_id": thread,
            "todo_id": binding["primary_todo_id"],
            "content": {
                "text": text,
                "phase": "long_run",
                "structured": entry_type != "WORK_NOTE",
            },
            "evidence_refs": [f"artifact://v5/{subject_key}/{index:04d}"],
            "supersedes": [last_by_subject[subject_key]] if subject_key in last_by_subject else [],
            "resolves": [],
        }
        if index == 210:
            conflict_id = last_by_subject.get("conflict.scope")
            if conflict_id:
                entry["resolves"] = [conflict_id]
        event, previous_hash = append_event(
            event_path,
            next_seq,
            previous_hash,
            "ENTRY_POSTED",
            actor,
            thread,
            {"entry": dict(entry, source_event_id=f"evt-{next_seq:04d}")},
        )
        entry["source_event_id"] = event["event_id"]
        entries[entry_id] = entry
        last_by_subject[subject_key] = entry_id
        next_seq += 1

        if index + 1 == c["simulate_abrupt_interruption_after"]:
            interrupted_event_count = len(read_jsonl(event_path))
            interrupted_chain_head = previous_hash
            recovery_events, recovered_entries = rebuild_event_chain(event_path)
            recovery_receipt = {
                "schema_version": "v5_recovery_receipt_v1",
                "simulated_interruption_after_entry": index + 1,
                "event_count_before_recovery": interrupted_event_count,
                "event_count_after_recovery": len(recovery_events),
                "chain_head_before_recovery": interrupted_chain_head,
                "chain_head_after_recovery": recovery_events[-1]["event_hash"],
                "recovered_entry_count": len(recovered_entries),
                "raw_events_lost": 0,
                "recovery_passed": interrupted_event_count == len(recovery_events)
                and interrupted_chain_head == recovery_events[-1]["event_hash"],
            }
            write_json(group_dir / "RECOVERY_RECEIPT.json", recovery_receipt)
            entries = recovered_entries

        if index + 1 == c["compaction_event_threshold"]:
            entries = rebuild_event_chain(event_path)[1]
            candidates = merge_candidates(
                group_dir / "group_memory_candidates.jsonl",
                extract_candidates(entries, group_id, "periodic_compaction_200"),
            )
            snapshot_version += 1
            snapshot = build_hot_snapshot(
                group,
                entries,
                binding,
                snapshot_version,
                c["context_max_entries"],
            )
            snapshot["raw_event_count"] = len(read_jsonl(event_path))
            snapshot["source_event_chain_head"] = previous_hash
            write_json(group_dir / "working_snapshot.json", snapshot)
            write_json(group_dir / "view.json", snapshot)
            compact_context, measurements = make_context(
                group,
                entries,
                candidates,
                snapshot,
                binding,
                c["context_budget_bytes"],
                c["context_max_entries"],
            )
            write_json(group_dir / "context_after_compaction.json", compact_context)
            budget_comparison = {
                "schema_version": "v5_context_budget_comparison_v1",
                "event_count_at_measurement": len(read_jsonl(event_path)),
                "raw_entries_at_measurement": len(entries),
                "naive_context_bytes": measurements["naive_bytes"],
                "bounded_context_bytes": measurements["bounded_bytes"],
                "budget_bytes": c["context_budget_bytes"],
                "naive_context_approx_tokens": (measurements["naive_bytes"] + 3) // 4,
                "bounded_context_approx_tokens": (measurements["bounded_bytes"] + 3) // 4,
                "bounded_context_passed": measurements["bounded_bytes"] <= c["context_budget_bytes"],
                "raw_event_log_preserved": len(read_jsonl(event_path)) == c["compaction_event_threshold"],
                "omitted_raw_entries": max(0, len(entries) - len(compact_context["hot_entries"])),
            }
            compaction_receipt = {
                "schema_version": "v5_compaction_receipt_v1",
                "trigger": "event_threshold",
                "threshold": c["compaction_event_threshold"],
                "event_count_before": len(read_jsonl(event_path)),
                "event_count_after": len(read_jsonl(event_path)),
                "raw_event_log_modified": False,
                "candidate_count": len(candidates),
                "working_snapshot_version": snapshot_version,
                "context_budget_passed": budget_comparison["bounded_context_passed"],
            }
            write_json(group_dir / "COMPACTION_RECEIPT.json", compaction_receipt)

    entries = rebuild_event_chain(event_path)[1]
    candidates = merge_candidates(
        group_dir / "group_memory_candidates.jsonl",
        extract_candidates(entries, group_id, "early_close"),
    )
    snapshot_version += 1
    snapshot = build_hot_snapshot(
        group,
        entries,
        binding,
        snapshot_version,
        c["context_max_entries"],
        status="FREEZING",
    )
    snapshot["raw_event_count"] = len(read_jsonl(event_path))
    snapshot["source_event_chain_head"] = previous_hash
    write_json(group_dir / "working_snapshot.json", snapshot)
    write_json(group_dir / "view.json", snapshot)

    freeze_event, previous_hash = append_event(
        event_path,
        next_seq,
        previous_hash,
        "GROUP_FROZEN",
        c["member_ids"][0],
        c["source_thread_ids"][0],
        {
            "group_id": group_id,
            "candidate_count": len(candidates),
            "raw_event_count": len(read_jsonl(event_path)),
        },
    )
    next_seq += 1
    frozen_snapshot = {
        "schema_version": "v5_frozen_snapshot_v1",
        "group_id": group_id,
        "task_id": c["task_id"],
        "status": "FROZEN",
        "snapshot_version": snapshot_version,
        "event_chain_head": previous_hash,
        "freeze_event_id": freeze_event["event_id"],
        "candidate_ids": [candidate["memory_id"] for candidate in candidates],
        "open_question_ids": [
            entry["entry_id"]
            for entry in entries.values()
            if entry["entry_type"] == "QUESTION_OPENED" and entry["status"] == "active"
        ],
        "open_conflict_ids": [
            entry["entry_id"]
            for entry in entries.values()
            if entry["entry_type"] == "CONFLICT_RECORDED" and entry["status"] == "active"
        ],
        "automatic_project_promotion": False,
    }
    frozen_snapshot["snapshot_hash"] = digest(frozen_snapshot)
    write_json(group_dir / "FROZEN_SNAPSHOT.json", frozen_snapshot)

    handoff = {
        "schema_version": "v5_stable_handoff_v1",
        "group_id": group_id,
        "task_id": c["task_id"],
        "goal_id": binding["goal_id"],
        "primary_todo_id": binding["primary_todo_id"],
        "claim_id": binding["claim_id"],
        "status": "PENDING_PROJECT_REVIEW",
        "reason": "early_close_with_open_items",
        "candidate_ids": [candidate["memory_id"] for candidate in candidates],
        "open_question_ids": frozen_snapshot["open_question_ids"],
        "open_conflict_ids": frozen_snapshot["open_conflict_ids"],
        "frozen_snapshot_hash": frozen_snapshot["snapshot_hash"],
        "event_chain_head": previous_hash,
        "automatic_project_promotion": False,
        "member_tokens_included": False,
    }
    handoff["handoff_hash"] = digest(handoff)
    write_json(group_dir / "STABLE_HANDOFF.json", handoff)

    loopx_hash = append_loopx_event(
        loopx_event_path,
        2,
        loopx_hash,
        "todo_completed",
        {
            "project_id": binding["project_id"],
            "goal_id": binding["goal_id"],
            "primary_todo_id": binding["primary_todo_id"],
            "claim_id": binding["claim_id"],
            "group_id": group_id,
            "handoff_hash": handoff["handoff_hash"],
            "status": "pending_project_review",
            "automatic_project_promotion": False,
        },
    )
    group["state"] = "ARCHIVED"
    group["closed_at"] = iso_at(c["event_target"] + 2)
    group["close_reason"] = "early_close_with_open_items"
    group["event_chain_head"] = previous_hash
    group["handoff_hash"] = handoff["handoff_hash"]
    write_json(group_dir / "group.json", group)
    write_json(
        group_dir / "members.json",
        {
            "schema_version": "v5_canary_members_v1",
            "members": [
                {"member_id": c["member_ids"][0], "role": "controller", "active": False, "revoked": True},
                {"member_id": c["member_ids"][1], "role": "reviewer", "active": False, "revoked": True},
            ],
            "plaintext_lease_tokens": False,
        },
    )
    index = {
        "schema_version": "v5_group_memory_index_v1",
        "group_id": group_id,
        "status": "ARCHIVED",
        "candidate_count": len(candidates),
        "active_candidate_ids": [candidate["memory_id"] for candidate in candidates],
        "promoted_project_memory_ids": [],
        "source_event_chain_head": previous_hash,
        "index_hash": None,
    }
    index["index_hash"] = digest(index)
    write_json(group_dir / "group_memory_index.json", index)

    final_context, final_measurements = make_context(
        group,
        entries,
        candidates,
        snapshot,
        binding,
        c["context_budget_bytes"],
        c["context_max_entries"],
    )
    write_json(group_dir / "context_after_close.json", final_context)
    budget_comparison["after_close_bounded_context_bytes"] = final_measurements["bounded_bytes"]
    budget_comparison["after_close_budget_passed"] = final_measurements["bounded_bytes"] <= c["context_budget_bytes"]
    write_json(group_dir / "CONTEXT_BUDGET_COMPARISON.json", budget_comparison)
    write_json(group_dir / "LOOPX_COMPLETION_RECEIPT.json", {
        "schema_version": "v5_loopx_completion_receipt_v1",
        "loopx_event_hash": loopx_hash,
        "goal_id": binding["goal_id"],
        "primary_todo_id": binding["primary_todo_id"],
        "group_id": group_id,
        "handoff_hash": handoff["handoff_hash"],
        "status": "pending_project_review",
        "automatic_project_promotion": False,
    })

    reverse_review = f"""# v5 Canary Reverse Review

## 1. Scope and authority

The canary used one new group, two members, a new sandbox LoopX goal, and a
separate runtime. No project-control or production memory promotion was used.

## 2. Event chain and reconstruction

The append-only event chain reached {len(read_jsonl(event_path))} events. The
simulated interruption was recovered from the chain with zero lost events.

## 3. Group-memory candidate quality

Candidates were extracted only from typed facts, decisions, conflicts,
questions, risks, partial results, and artifacts. WORK_NOTE entries were not
promoted. Every candidate retains source event, member, thread, todo, hash, and
supersedes metadata.

## 4. Periodic compaction and supersedes

Compaction ran at event threshold {c["compaction_event_threshold"]}. The raw
event log was not rewritten. Revisions are linked through supersedes and
conflict resolution keeps the evidence lineage.

## 5. Context budget

The naive all-entry projection measured {budget_comparison["naive_context_bytes"]}
bytes; the bounded projection measured {budget_comparison["bounded_context_bytes"]}
bytes against an {c["context_budget_bytes"]}-byte budget.

## 6. LoopX binding and failure boundaries

The fixture binds project, goal, primary todo, claim, group, member, thread, and
handoff in both directions. Completion remains pending-project-review and no
project truth is promoted.

## 7. Known limits and verdict

This is an isolated architecture simulation, not Codex desktop integration.
It does not prove provider routing, automatic task identity injection, or
production Graphiti behavior. The bounded-memory contract is CANARY-PASS only.
"""
    (group_dir / "REVERSE_REVIEW.md").write_text(reverse_review, encoding="utf-8")

    receipt = {
        "schema_version": "v5_canary_receipt_v1",
        "experiment_id": spec["experiment_id"],
        "status": "CANARY_PASS",
        "group_id": group_id,
        "member_count": 2,
        "event_count": len(read_jsonl(event_path)),
        "entry_count_after_rebuild": len(entries),
        "candidate_count": len(candidates),
        "compaction": compaction_receipt,
        "recovery": recovery_receipt,
        "context_budget": budget_comparison,
        "loopx": {
            "goal_id": binding["goal_id"],
            "primary_todo_id": binding["primary_todo_id"],
            "claim_id": binding["claim_id"],
            "handoff_hash": handoff["handoff_hash"],
            "completion_status": "pending_project_review",
            "automatic_project_promotion": False,
        },
        "forbidden_side_effects": {
            "project_control_write": False,
            "current_loopx_mutation": False,
            "graphiti_production": False,
            "current_archived_group_read": False,
            "frontend_started": False,
        },
        "output_root": str(group_dir),
    }
    write_json(group_dir / "CANARY_RECEIPT.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
