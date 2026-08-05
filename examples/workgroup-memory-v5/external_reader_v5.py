#!/usr/bin/env python3
"""Independent package-out reader for the v5 canary.

This verifier intentionally does not import run_v5_canary.py or any main
project implementation. It reconstructs the event chain and checks the
published artifacts as an external consumer would.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=Path(__file__).with_name("dispatch_contract_v5_task_spec.json"))
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    spec = read_json(args.spec)
    runtime = (args.runtime or Path(spec["isolation"]["runtime_root"])).resolve()
    group_dir = runtime / spec["canary"]["group_id"]
    results: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: str) -> None:
        results.append({"name": name, "status": "PASS" if condition else "FAIL", "detail": detail})

    required = [
        "events.jsonl",
        "working_snapshot.json",
        "group_memory_candidates.jsonl",
        "group_memory_index.json",
        "loopx_bindings.json",
        "STABLE_HANDOFF.json",
        "FROZEN_SNAPSHOT.json",
        "CONTEXT_BUDGET_COMPARISON.json",
        "RECOVERY_RECEIPT.json",
        "REVERSE_REVIEW.md",
        "CANARY_RECEIPT.json",
    ]
    check(
        "required_artifacts_present",
        all((group_dir / name).exists() for name in required),
        "all canary artifacts exist",
    )

    events = read_jsonl(group_dir / "events.jsonl")
    previous = "GENESIS"
    chain_ok = True
    for expected_seq, event in enumerate(events):
        if event.get("seq") != expected_seq or event.get("prev_event_hash") != previous:
            chain_ok = False
            break
        claimed = event.get("event_hash")
        unhashed = {key: value for key, value in event.items() if key != "event_hash"}
        if claimed != digest(unhashed):
            chain_ok = False
            break
        previous = claimed
    check("append_only_event_chain", chain_ok, f"verified_events={len(events)}")
    check(
        "long_run_event_target",
        len(events) >= spec["canary"]["event_target"],
        f"event_count={len(events)}; target={spec['canary']['event_target']}",
    )

    entries: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "ENTRY_POSTED":
            continue
        entry = json.loads(json.dumps(event["payload"]["entry"]))
        entries[entry["entry_id"]] = entry
        for old_id in entry.get("supersedes", []):
            if old_id in entries:
                entries[old_id]["status"] = "superseded"
        for resolved_id in entry.get("resolves", []):
            if resolved_id in entries:
                entries[resolved_id]["status"] = "resolved"
    check("external_rebuild", len(entries) > 0, f"rebuilt_entries={len(entries)}")
    check(
        "supersedes_present",
        any(entry.get("supersedes") for entry in entries.values()),
        "at least one revision preserves a supersedes edge",
    )
    check(
        "noise_not_promoted",
        all(
            candidate["entry_type"] != "WORK_NOTE"
            for candidate in read_jsonl(group_dir / "group_memory_candidates.jsonl")
        ),
        "WORK_NOTE entries are absent from group-memory candidates",
    )

    candidates = read_jsonl(group_dir / "group_memory_candidates.jsonl")
    event_ids = {event["event_id"] for event in events}
    candidate_sources_ok = all(
        set(candidate.get("source_event_ids", [])).issubset(event_ids)
        and candidate.get("status") == "unreviewed"
        for candidate in candidates
    )
    check(
        "candidate_lineage_and_status",
        bool(candidates) and candidate_sources_ok,
        f"candidates={len(candidates)}; all_unreviewed_with_sources={candidate_sources_ok}",
    )
    index = read_json(group_dir / "group_memory_index.json")
    check(
        "no_automatic_project_promotion",
        index.get("promoted_project_memory_ids") == []
        and not (runtime / "project_control").exists(),
        "candidate index has no promoted project memory and no project-control output",
    )

    recovery = read_json(group_dir / "RECOVERY_RECEIPT.json")
    check(
        "abrupt_recovery",
        recovery.get("recovery_passed") is True
        and recovery.get("raw_events_lost") == 0,
        json.dumps(recovery, ensure_ascii=False, sort_keys=True),
    )
    compaction = read_json(group_dir / "COMPACTION_RECEIPT.json")
    check(
        "raw_events_survive_compaction",
        compaction.get("raw_event_log_modified") is False
        and compaction.get("event_count_before") == compaction.get("event_count_after"),
        json.dumps(compaction, ensure_ascii=False, sort_keys=True),
    )

    budget = read_json(group_dir / "CONTEXT_BUDGET_COMPARISON.json")
    check(
        "context_budget_reduction",
        budget["naive_context_bytes"] > budget["bounded_context_bytes"]
        and budget["bounded_context_bytes"] <= budget["budget_bytes"]
        and budget["bounded_context_passed"] is True,
        json.dumps(budget, ensure_ascii=False, sort_keys=True),
    )
    snapshot = read_json(group_dir / "working_snapshot.json")
    check(
        "hot_snapshot_bounded",
        len(snapshot.get("entries", [])) <= spec["canary"]["context_max_entries"],
        f"hot_entries={len(snapshot.get('entries', []))}",
    )
    check(
        "archive_state_and_handoff",
        read_json(group_dir / "group.json").get("state") == "ARCHIVED"
        and read_json(group_dir / "STABLE_HANDOFF.json").get("status") == "PENDING_PROJECT_REVIEW",
        "group archived with pending review handoff",
    )

    binding = read_json(group_dir / "loopx_bindings.json")
    loopx_events = read_jsonl(runtime / "loopx-fixture" / "loopx_events.jsonl")
    completed = [
        event
        for event in loopx_events
        if event.get("event_kind") == "todo_completed"
    ]
    handoff = read_json(group_dir / "STABLE_HANDOFF.json")
    bidirectional = (
        binding["group_id"] == spec["canary"]["group_id"]
        and binding["goal_id"] == spec["loopx_binding"]["goal_id"]
        and binding["primary_todo_id"] == spec["loopx_binding"]["primary_todo_id"]
        and handoff["primary_todo_id"] == binding["primary_todo_id"]
        and completed
        and completed[-1]["payload"]["handoff_hash"] == handoff["handoff_hash"]
    )
    check(
        "loopx_bidirectional_binding",
        bool(bidirectional),
        "group->LoopX and LoopX->handoff references agree",
    )
    check(
        "two_member_scope",
        len(binding.get("member_bindings", [])) == 2
        and len(set(item["member_id"] for item in binding["member_bindings"])) == 2,
        "exactly two sandbox members are bound",
    )

    forbidden_pattern = re.compile(r"sk-[A-Za-z0-9]{12,}|-----BEGIN .*PRIVATE KEY-----")
    output_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in group_dir.rglob("*")
        if path.is_file() and path.name != "EXTERNAL_READER_REPORT.json"
    )
    check(
        "no_credential_values",
        forbidden_pattern.search(output_text) is None,
        "no API-key-like or private-key material found in canary output",
    )
    review_text = (group_dir / "REVERSE_REVIEW.md").read_text(encoding="utf-8")
    check(
        "seven_section_reverse_review",
        all(f"## {index}." in review_text for index in range(1, 8)),
        "reverse review contains seven required sections",
    )

    passed = all(item["status"] == "PASS" for item in results)
    report = {
        "schema_version": "v5_external_reader_report_v1",
        "status": "PASS" if passed else "FAIL",
        "checks_total": len(results),
        "checks_passed": sum(item["status"] == "PASS" for item in results),
        "checks_failed": sum(item["status"] == "FAIL" for item in results),
        "checks": results,
        "claim_ceiling": "isolated architecture canary only; not production integration",
    }
    (group_dir / "EXTERNAL_READER_REPORT.json").write_bytes(canonical(report) + b"\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
