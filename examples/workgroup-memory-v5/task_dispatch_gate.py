#!/usr/bin/env python3
"""Fail-closed gate for the isolated workgroup-memory v5 canary."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(canonical(value) + b"\n")
    os.replace(tmp, path)


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    spec_path = Path(__file__).with_name("dispatch_contract_v5_task_spec.json")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: str) -> None:
        checks.append({"name": name, "status": "PASS" if condition else "FAIL", "detail": detail})

    isolation = spec["isolation"]
    canary = spec["canary"]
    policy = spec["policy"]
    main_root = Path(isolation["main_root"]).resolve()
    worktree_root = Path(isolation["worktree_root"]).resolve()
    experiment_root = Path(isolation["experiment_root"]).resolve()
    runtime_root = Path(isolation["runtime_root"]).resolve()

    check(
        "dispatch_contract_version",
        spec.get("dispatch_contract_version") == 5,
        f"expected 5, got {spec.get('dispatch_contract_version')!r}",
    )
    check(
        "worktree_is_independent",
        worktree_root != main_root and not inside(worktree_root, main_root),
        f"worktree={worktree_root}; main={main_root}",
    )
    check(
        "experiment_is_inside_worktree",
        inside(experiment_root, worktree_root),
        f"experiment={experiment_root}; worktree={worktree_root}",
    )
    check(
        "runtime_is_independent",
        runtime_root != main_root and not inside(runtime_root, main_root),
        f"runtime={runtime_root}; main={main_root}",
    )
    check(
        "runtime_is_not_current_runtime",
        not any(inside(runtime_root, Path(root)) for root in isolation["forbidden_runtime_roots"]),
        f"runtime={runtime_root}",
    )
    check(
        "runtime_is_empty_or_new",
        not runtime_root.exists()
        or all(
            item.name == "TASK_DISPATCH_GATE_RECEIPT.json"
            for item in runtime_root.iterdir()
        ),
        f"runtime_entries={sorted(item.name for item in runtime_root.iterdir()) if runtime_root.exists() else []}",
    )
    check(
        "two_member_canary",
        len(canary["member_ids"]) == 2
        and len(canary["source_thread_ids"]) == 2
        and len(set(canary["member_ids"])) == 2,
        f"members={canary['member_ids']}",
    )
    check(
        "bounded_context_contract",
        0 < canary["context_budget_bytes"] <= 8192
        and 0 < canary["context_max_entries"] <= 20,
        f"budget={canary['context_budget_bytes']}; max_entries={canary['context_max_entries']}",
    )
    check(
        "long_run_and_recovery_contract",
        canary["event_target"] >= 200
        and 0 < canary["simulate_abrupt_interruption_after"] < canary["event_target"],
        f"events={canary['event_target']}; interruption={canary['simulate_abrupt_interruption_after']}",
    )
    check(
        "no_project_control_write",
        policy["project_control_write"] is False
        and policy["automatic_project_promotion"] is False,
        "project control and promotion are disabled",
    )
    check(
        "no_graphiti_production",
        policy["graphiti_production"] is False,
        "Graphiti production is disabled",
    )
    check(
        "no_current_loopx_mutation",
        policy["current_loopx_state_write"] is False,
        "only sandbox LoopX fixtures are allowed",
    )
    check(
        "no_current_group_access",
        policy["current_archived_group_read"] is False
        and not any(
            forbidden
            in json.dumps(
                {
                    key: value
                    for key, value in spec.items()
                    if key != "isolation"
                },
                ensure_ascii=False,
            )
            for forbidden in isolation["forbidden_identifiers"]
        ),
        "current group identifiers are absent from the task spec",
    )
    check(
        "write_targets_are_isolated",
        all(
            inside(Path(target).resolve(), experiment_root)
            or inside(Path(target).resolve(), runtime_root)
            for target in spec["write_targets"]
        ),
        "all declared write targets are inside the experiment worktree or runtime",
    )
    check(
        "primary_todo_is_sandboxed",
        spec["loopx_binding"]["goal_id"].startswith("v5-")
        and spec["loopx_binding"]["primary_todo_id"].startswith("v5-"),
        "LoopX fixture identifiers use the v5 sandbox namespace",
    )

    passed = all(item["status"] == "PASS" for item in checks)
    receipt = {
        "schema_version": "codex_workgroup_dispatch_gate_v5",
        "dispatch_contract_version": 5,
        "experiment_id": spec["experiment_id"],
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "write_scope": {
            "experiment_root": str(experiment_root),
            "runtime_root": str(runtime_root),
            "main_root": str(main_root),
        },
    }
    receipt_path = runtime_root / "TASK_DISPATCH_GATE_RECEIPT.json"
    write_json(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
