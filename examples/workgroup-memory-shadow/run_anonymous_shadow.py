#!/usr/bin/env python3
"""Run the complete anonymous workgroup-memory shadow flow.

The example creates its source projections in a temporary directory.  It does
not read a user's Codex runtime, a project directory, LoopX, or Graphiti.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_brain.workgroup_memory_shadow import (  # noqa: E402
    ShadowError,
    build_injection_slice,
    checkpoint_memory,
    diagnostics_projection,
    external_reader_verify,
    get_entry,
    import_source_group,
    recover_shadow_view,
)


GROUP_ID = "synthetic-shadow-group-001"
CONTROLLER_THREAD = "synthetic-thread-controller"
WORKER_THREAD = "synthetic-thread-worker"
HISTORICAL_THREAD = "synthetic-thread-historical"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def create_source(root: Path) -> None:
    """Create the smallest source projection set accepted by the adapter."""

    write_json(
        root / "group.json",
        {
            "schema": "synthetic_workgroup_v1",
            "group_id": GROUP_ID,
            "display_name": "Synthetic bounded-memory review",
            "project_id": "synthetic-project",
            "status": "ACTIVE",
            "controller_thread_id": CONTROLLER_THREAD,
            "work_package_id": "SYNTHETIC-WORK-PACKAGE",
            "task_spec_sha256": "a" * 64,
            "member_count": 3,
            "active_task_count": 1,
            "queued_task_count": 0,
            "step_order": 4,
            "step5_allowed": False,
            "business_world_write_allowed": False,
            "business_k3_effect": 0,
        },
    )
    write_json(
        root / "members.json",
        {
            "schema": "synthetic_members_v1",
            "group_id": GROUP_ID,
            "members": [
                {
                    "thread_id": CONTROLLER_THREAD,
                    "display_name": "Synthetic controller task",
                    "role": "controller",
                    "status": "ACTIVE",
                },
                {
                    "thread_id": WORKER_THREAD,
                    "display_name": "Synthetic evidence task",
                    "role": "worker",
                    "status": "ACTIVE",
                },
                {
                    "thread_id": HISTORICAL_THREAD,
                    "display_name": "Synthetic historical task",
                    "role": "reviewer",
                    "status": "QUEUED",
                },
            ],
        },
    )
    write_json(
        root / "task_pool.json",
        {
            "schema": "synthetic_task_pool_v1",
            "group_id": GROUP_ID,
            "one_member_one_task": True,
            "tasks": [
                {
                    "task_id": "SYNTH-TASK-01",
                    "status": "CLAIMED_ACTIVE",
                    "owner_thread_id": WORKER_THREAD,
                    "scope": "bounded-recovery",
                },
                {
                    "task_id": "SYNTH-TASK-02",
                    "status": "QUEUED",
                    "owner_thread_id": None,
                    "scope": "evidence-review",
                },
            ],
            "queued_next_step_tasks": [],
        },
    )
    write_json(
        root / "working_snapshot.json",
        {
            "schema": "synthetic_snapshot_v1",
            "group_id": GROUP_ID,
            "snapshot_version": 7,
            "current_goal": "Keep accepted state recoverable without replaying chat.",
            "active_tasks": ["SYNTH-TASK-01"],
            "latest_decisions": [
                "Use append-only evidence references.",
                "Regenerate a bounded slice before continuation.",
            ],
            "open_risks": [
                "A counterevidence entry must remain retrievable.",
            ],
            "step5_allowed": False,
            "business_world_write_allowed": False,
            "business_k3_effect": 0,
        },
    )
    write_json(
        root / "lanes" / "SYNTH-TASK-01" / "LANE_HANDOFF.json",
        {
            "status": "READY_FOR_REVIEW",
            "summary": "Synthetic handoff with a bounded evidence claim.",
            "evidence_refs": ["synthetic/evidence/contract"],
        },
    )
    write_json(
        root / "lanes" / "SYNTH-TASK-01" / "RECEIPT.json",
        {"verdict": "PASS", "checks": ["source", "scope", "counterevidence"]},
    )


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agent-brain-shadow-") as temp:
        root = Path(temp)
        source = root / "source-group"
        shadow = root / "shadow-group"
        source.mkdir()
        create_source(source)

        imported = import_source_group(source, shadow)
        reader = external_reader_verify(source, shadow)
        first_slice = build_injection_slice(
            shadow,
            target_thread_id=WORKER_THREAD,
            model_window_tokens=353000,
            remaining_token_reserve=64000,
        )
        included_ids = first_slice["receipt"]["included_entry_ids"]
        exact_lookup = get_entry(shadow, included_ids[0]) if included_ids else None
        candidate = checkpoint_memory(shadow, reason="synthetic-before-freeze")

        # Simulate loss of rebuildable projections, not loss of the event archive.
        (shadow / "view.json").unlink()
        (shadow / "position_cards.json").unlink()
        recovery = recover_shadow_view(shadow)
        projection = diagnostics_projection(shadow)
        return {
            "status": "PASS",
            "synthetic_only": True,
            "event_chain": {
                "count": projection["event_chain"]["count"],
                "head_present": bool(projection["event_chain"]["head"]),
            },
            "source_reader": {
                "status": reader["status"],
                "tasks_source": reader["tasks_source"],
                "tasks_imported": reader["tasks_imported"],
                "members_source": reader["members_source"],
                "members_imported": reader["members_imported"],
            },
            "position_cards": projection["position_cards"],
            "injection": {
                "status": first_slice["receipt"]["injection_state"],
                "platform_consumption": first_slice["receipt"]["platform_consumption_state"],
                "bytes": first_slice["receipt"]["bytes"],
                "approx_tokens": first_slice["receipt"]["approx_tokens"],
                "included": first_slice["receipt"]["included_count"],
                "omitted": first_slice["receipt"]["omitted_count"],
                "exact_get_entry": bool(exact_lookup and exact_lookup["exact"]),
            },
            "checkpoint": {
                "memory_id": candidate["memory_id"],
                "status": candidate["status"],
                "automatic_project_promotion": False,
                "graphiti_pending": projection["graphiti"]["pending_review"],
            },
            "recovery": recovery,
            "authority": projection["authority"],
        }


def main() -> int:
    try:
        print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
    except (ShadowError, OSError, ValueError) as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
