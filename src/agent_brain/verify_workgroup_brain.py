#!/usr/bin/env python3
"""Independent reader for the generic ephemeral workgroup state.

This file intentionally does not import workgroup_brain.py.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    temp.replace(path)


def scan_forbidden_secret_keys(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.lower()
            if lowered in {
                "lease_token",
                "token",
                "authorization_header",
                "access_token",
                "refresh_token",
            }:
                hits.append(f"{path}.{key}")
            hits.extend(scan_forbidden_secret_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(scan_forbidden_secret_keys(child, f"{path}[{index}]"))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group-dir", required=True)
    parser.add_argument("--protected-snapshot", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    group_dir = Path(args.group_dir)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    meta = read_json(group_dir / "group.json")
    members_doc = read_json(group_dir / "members.json")
    view = read_json(group_dir / "view.json")
    snapshot = read_json(group_dir / "FROZEN_SNAPSHOT.json")
    handoff = read_json(group_dir / "STABLE_HANDOFF.json")
    protected = read_json(Path(args.protected_snapshot))

    events = []
    previous = "GENESIS"
    with (group_dir / "events.jsonl").open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            event = json.loads(raw)
            expected_seq = len(events) + 1
            unhashed = {
                key: value for key, value in event.items() if key != "event_hash"
            }
            actual_hash = digest(unhashed)
            check(
                f"event_seq_{line_number}",
                event.get("seq") == expected_seq,
                {"expected": expected_seq, "actual": event.get("seq")},
            )
            check(
                f"event_prev_hash_{line_number}",
                event.get("prev_event_hash") == previous,
                {
                    "expected": previous,
                    "actual": event.get("prev_event_hash"),
                },
            )
            check(
                f"event_hash_{line_number}",
                hmac.compare_digest(event.get("event_hash", ""), actual_hash),
                {"expected": actual_hash, "actual": event.get("event_hash")},
            )
            previous = event["event_hash"]
            events.append(event)

    entries: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    event_type_counts: dict[str, int] = {}
    for event in events:
        event_type_counts[event["event_type"]] = (
            event_type_counts.get(event["event_type"], 0) + 1
        )
        if event["event_type"] == "ENTRY_POSTED":
            entry = event["payload"]["entry"]
            entries[entry["entry_id"]] = dict(entry)
        elif event["event_type"] == "ENTRY_RESOLVED":
            target = event["payload"]["target_entry_id"]
            resolutions[target] = {
                "status": event["payload"]["status"],
                "resolution": event["payload"]["resolution"],
                "resolved_by": event["actor_member_id"],
                "resolved_at": event["at"],
                "resolution_event_id": event["event_id"],
            }
    for entry_id, resolution in resolutions.items():
        if entry_id in entries:
            entries[entry_id]["resolution"] = resolution
            entries[entry_id]["status"] = resolution["status"]
    ordered_entries = sorted(entries.values(), key=lambda item: item["entry_seq"])
    expected_open_questions = [
        item["entry_id"]
        for item in ordered_entries
        if item["entry_type"] == "QUESTION_OPENED" and item["status"] == "active"
    ]
    expected_open_conflicts = [
        item["entry_id"]
        for item in ordered_entries
        if item["entry_type"] == "CONFLICT_RECORDED"
        and item["status"] == "active"
    ]
    expected_best = [
        item["entry_id"]
        for item in ordered_entries
        if item["entry_type"] == "CURRENT_BEST_MODEL"
        and item["status"] == "active"
    ]

    check(
        "event_chain_head",
        view["event_chain_head"] == previous,
        {"expected": previous, "actual": view["event_chain_head"]},
    )
    check(
        "view_version",
        view["view_version"] == len(events),
        {"events": len(events), "view_version": view["view_version"]},
    )
    check(
        "view_entries_rebuilt",
        view["entries"] == ordered_entries,
        {"expected": len(ordered_entries), "actual": len(view["entries"])},
    )
    check(
        "open_questions_rebuilt",
        view["open_question_entry_ids"] == expected_open_questions,
        {
            "expected": expected_open_questions,
            "actual": view["open_question_entry_ids"],
        },
    )
    check(
        "open_conflicts_rebuilt",
        view["open_conflict_entry_ids"] == expected_open_conflicts,
        {
            "expected": expected_open_conflicts,
            "actual": view["open_conflict_entry_ids"],
        },
    )
    check(
        "current_best_model_rebuilt",
        view["current_best_model_entry_ids"] == expected_best,
        {
            "expected": expected_best,
            "actual": view["current_best_model_entry_ids"],
        },
    )
    view_hash_payload = {
        key: value
        for key, value in view.items()
        if key not in {"generated_at", "semantic_hash"}
    }
    expected_view_hash = digest(view_hash_payload)
    check(
        "view_semantic_hash",
        hmac.compare_digest(view["semantic_hash"], expected_view_hash),
        {"expected": expected_view_hash, "actual": view["semantic_hash"]},
    )
    required_group_fields = {
        "group_id",
        "task_id",
        "objective",
        "status",
        "created_at",
        "expires_at",
        "controller_member_id",
        "authority_bundle_sha256",
        "loopx_goal_id",
        "member_registry_sha256",
    }
    check(
        "required_group_fields",
        required_group_fields.issubset(meta),
        sorted(required_group_fields - set(meta)),
    )
    member_registry_hash = digest(members_doc)
    check(
        "member_registry_hash",
        meta.get("member_registry_sha256") == member_registry_hash,
        {
            "expected": member_registry_hash,
            "actual": meta.get("member_registry_sha256"),
        },
    )
    required_member_fields = {
        "member_id",
        "host_id",
        "thread_id",
        "role",
        "read_scope",
        "write_scope",
        "joined_at",
        "lease_expires_at",
        "status",
    }
    missing_member_fields = {
        member["member_id"]: sorted(required_member_fields - set(member))
        for member in members_doc["members"].values()
        if not required_member_fields.issubset(member)
    }
    check("required_member_fields", not missing_member_fields, missing_member_fields)
    required_entry_fields = {
        "group_id",
        "task_id",
        "entry_id",
        "author_member_id",
        "entry_type",
        "content",
        "status",
        "confidence",
        "created_at",
        "evidence_refs",
        "supersedes",
        "scope",
        "content_hash",
    }
    missing_entry_fields = {
        entry["entry_id"]: sorted(required_entry_fields - set(entry))
        for entry in ordered_entries
        if not required_entry_fields.issubset(entry)
    }
    check("required_entry_fields", not missing_entry_fields, missing_entry_fields)
    bad_confidence = {
        entry["entry_id"]: entry.get("confidence")
        for entry in ordered_entries
        if not isinstance(entry.get("confidence"), (int, float))
        or isinstance(entry.get("confidence"), bool)
        or not 0 <= entry["confidence"] <= 1
    }
    check("entry_confidence_range", not bad_confidence, bad_confidence)
    expected_sections = {
        "objective_and_scope",
        "frozen_authority_context",
        "current_claims",
        "confirmed_facts",
        "current_best_hypotheses",
        "rejected_routes",
        "partial_results",
        "artifact_and_evidence_refs",
        "open_questions",
        "conflicts",
        "next_actions",
        "handoff_readiness",
    }
    actual_sections = set(view.get("shared_view_sections", {}))
    check(
        "shared_view_sections",
        expected_sections == actual_sections,
        {
            "missing": sorted(expected_sections - actual_sections),
            "extra": sorted(actual_sections - expected_sections),
        },
    )

    snapshot_payload = {
        key: value for key, value in snapshot.items() if key != "snapshot_sha256"
    }
    expected_snapshot_hash = digest(snapshot_payload)
    check(
        "frozen_snapshot_hash",
        hmac.compare_digest(
            snapshot.get("snapshot_sha256", ""), expected_snapshot_hash
        ),
        {
            "expected": expected_snapshot_hash,
            "actual": snapshot.get("snapshot_sha256"),
        },
    )
    check(
        "meta_snapshot_pin",
        meta.get("frozen_snapshot_sha256") == snapshot.get("snapshot_sha256"),
        {
            "meta": meta.get("frozen_snapshot_sha256"),
            "snapshot": snapshot.get("snapshot_sha256"),
        },
    )

    handoff_payload = {
        key: value for key, value in handoff.items() if key != "handoff_sha256"
    }
    expected_handoff_hash = digest(handoff_payload)
    check(
        "handoff_hash",
        hmac.compare_digest(handoff.get("handoff_sha256", ""), expected_handoff_hash),
        {
            "expected": expected_handoff_hash,
            "actual": handoff.get("handoff_sha256"),
        },
    )
    check(
        "handoff_snapshot_pin",
        handoff.get("frozen_snapshot_sha256") == snapshot.get("snapshot_sha256"),
        {
            "handoff": handoff.get("frozen_snapshot_sha256"),
            "snapshot": snapshot.get("snapshot_sha256"),
        },
    )
    check(
        "handoff_loopx_reconciliation_ref",
        bool(handoff.get("loopx_reconciliation_ref")),
        handoff.get("loopx_reconciliation_ref"),
    )
    check(
        "meta_handoff_pin",
        meta.get("handoff_sha256") == handoff.get("handoff_sha256"),
        {
            "meta": meta.get("handoff_sha256"),
            "handoff": handoff.get("handoff_sha256"),
        },
    )

    members = list(members_doc["members"].values())
    roles = sorted(member["role"] for member in members)
    check(
        "member_registry_nonempty",
        len(members) >= 1,
        {"count": len(members), "member_ids": sorted(m["member_id"] for m in members)},
    )
    check(
        "exactly_one_controller",
        roles.count("controller") == 1,
        {"roles": roles},
    )
    terminal_state = meta.get("state") in {
        "ARCHIVED",
        "CLOSED",
        "EXPIRED",
        "EXPIRED_OR_ARCHIVED",
        "MEMBERS_REVOKED",
    }
    check(
        "member_revocation_matches_lifecycle",
        (
            all(
                not member["active"] and member.get("revoked_at")
                for member in members
            )
            if terminal_state
            else any(member["active"] for member in members)
        ),
        {
            "terminal_state": terminal_state,
            "members": {
                member["member_id"]: {
                    "active": member["active"],
                    "revoked_at": member.get("revoked_at"),
                }
                for member in members
            }
        },
    )
    check(
        "group_and_view_state_agree",
        meta.get("state") == view.get("state"),
        {"meta": meta.get("state"), "view": view.get("state")},
    )

    required_event_counts = {
        "GROUP_CREATED": 1,
        "MEMBER_ADDED": max(0, len(members) - 1),
        "GROUP_FROZEN": 1,
        "HANDOFF_CREATED": 1,
    }
    for event_type, expected in required_event_counts.items():
        check(
            f"event_count_{event_type}",
            event_type_counts.get(event_type, 0) == expected,
            {
                "expected": expected,
                "actual": event_type_counts.get(event_type, 0),
            },
        )
    check(
        "group_close_event_matches_lifecycle",
        (
            event_type_counts.get("GROUP_CLOSED", 0) == 1
            if terminal_state
            else event_type_counts.get("GROUP_CLOSED", 0) == 0
        ),
        {
            "terminal_state": terminal_state,
            "actual": event_type_counts.get("GROUP_CLOSED", 0),
        },
    )
    entry_types = {entry["entry_type"] for entry in ordered_entries}
    check(
        "entry_types_declared",
        all(bool(entry_type) for entry_type in entry_types),
        sorted(entry_types),
    )
    check(
        "conflict_index_consistent",
        expected_open_conflicts == view["open_conflict_entry_ids"],
        {
            "open": expected_open_conflicts,
            "resolved_conflicts": [
                entry["entry_id"]
                for entry in ordered_entries
                if entry["entry_type"] == "CONFLICT_RECORDED"
                and entry["status"] == "resolved"
            ],
        },
    )

    secret_hits: list[str] = []
    for file_name, value in {
        "group.json": meta,
        "members.json": members_doc,
        "view.json": view,
        "FROZEN_SNAPSHOT.json": snapshot,
        "STABLE_HANDOFF.json": handoff,
        "events.jsonl": events,
    }.items():
        secret_hits.extend(
            f"{file_name}:{hit}" for hit in scan_forbidden_secret_keys(value)
        )
    check("no_plaintext_tokens", not secret_hits, secret_hits)

    protected_differences = []
    for raw_path, expected in protected["files"].items():
        path = Path(raw_path)
        actual = file_sha256(path)
        if actual != expected:
            protected_differences.append(
                {"path": raw_path, "expected": expected, "actual": actual}
            )
    check(
        "protected_authority_hashes_unchanged",
        not protected_differences,
        protected_differences,
    )
    check(
        "no_long_term_memory_mount",
        meta.get("long_term_memory_mounted") is False,
        meta.get("long_term_memory_mounted"),
    )
    check(
        "no_project_authority_write_capability",
        meta.get("project_authority_write_enabled") is False,
        meta.get("project_authority_write_enabled"),
    )
    check(
        "no_visible_message_transport",
        meta.get("visible_message_transport_enabled") is False,
        meta.get("visible_message_transport_enabled"),
    )

    passed = all(item["passed"] for item in checks)
    report = {
        "schema_version": "agent_brain_workgroup_external_reader_v1",
        "status": "PASS" if passed else "FAIL",
        "group_id": meta["group_id"],
        "group_dir": str(group_dir),
        "event_count": len(events),
        "entry_count": len(ordered_entries),
        "member_count": len(members),
        "checks_passed": sum(item["passed"] for item in checks),
        "checks_total": len(checks),
        "checks": checks,
        "claim_ceiling": (
            "ephemeral_process_identity_canary_only_not_real_multi_thread_injection_"
            "not_project_authority_not_long_term_memory"
        ),
    }
    report["report_sha256"] = digest(
        {key: value for key, value in report.items() if key != "report_sha256"}
    )
    atomic_write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
