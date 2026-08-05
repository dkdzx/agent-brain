#!/usr/bin/env python3
"""Synthetic controller/worker/reviewer lifecycle with independent verification."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BRAIN = REPO / "src" / "agent_brain" / "workgroup_brain.py"
VERIFY = REPO / "src" / "agent_brain" / "verify_workgroup_brain.py"
RUNTIME = REPO / ".demo-runtime"
GROUP_ID = "demo-group-001"


def run(script: Path, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=REPO,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def brain(*args: str) -> dict:
    return run(BRAIN, "--root", str(RUNTIME), *args)


def auth(member: str, token: str, host: str, thread: str) -> list[str]:
    return [
        "--member-id",
        member,
        f"--lease-token={token}",
        "--host-id",
        host,
        "--thread-id",
        thread,
    ]


def context_version(member_auth: list[str], scope: str = "task/shared") -> int:
    context = brain(
        "context",
        "--group-id",
        GROUP_ID,
        *member_auth,
        "--scope",
        scope,
    )
    return context["view_version"]


def coordinated_post(member_auth: list[str], *args: str) -> dict:
    version = context_version(member_auth)
    return brain(
        "post",
        "--group-id",
        GROUP_ID,
        *member_auth,
        "--expected-view-version",
        str(version),
        *args,
    )


def main() -> int:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    protected = REPO / "README.md"
    protected_snapshot = RUNTIME / "protected_snapshot.json"
    RUNTIME.mkdir(parents=True)
    protected_snapshot.write_text(
        json.dumps(
            {
                "files": {
                    str(protected): hashlib.sha256(protected.read_bytes()).hexdigest()
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    authority_hash = hashlib.sha256(protected_snapshot.read_bytes()).hexdigest()

    created = brain(
        "create",
        "--group-id",
        GROUP_ID,
        "--task-id",
        "synthetic-task-001",
        "--objective",
        "Demonstrate recoverable workgroup state",
        "--controller-member-id",
        "controller-001",
        "--host-id",
        "synthetic-host-controller",
        "--thread-id",
        "synthetic-thread-controller",
        "--scope",
        "task/shared",
        "--lease-hours",
        "4",
        "--expires-hours",
        "24",
        "--authority-bundle-sha256",
        authority_hash,
        "--loopx-goal-id",
        "synthetic-goal-001",
    )
    controller_token = created["lease_token"]

    worker = brain(
        "add-member",
        "--group-id",
        GROUP_ID,
        "--actor-member-id",
        "controller-001",
        f"--lease-token={controller_token}",
        "--actor-host-id",
        "synthetic-host-controller",
        "--actor-thread-id",
        "synthetic-thread-controller",
        "--member-id",
        "worker-001",
        "--role",
        "worker",
        "--host-id",
        "synthetic-host-worker",
        "--thread-id",
        "synthetic-thread-worker",
        "--scope",
        "task/shared",
    )
    reviewer = brain(
        "add-member",
        "--group-id",
        GROUP_ID,
        "--actor-member-id",
        "controller-001",
        f"--lease-token={controller_token}",
        "--actor-host-id",
        "synthetic-host-controller",
        "--actor-thread-id",
        "synthetic-thread-controller",
        "--member-id",
        "reviewer-001",
        "--role",
        "reviewer",
        "--host-id",
        "synthetic-host-reviewer",
        "--thread-id",
        "synthetic-thread-reviewer",
        "--scope",
        "task/shared",
    )

    worker_auth = auth(
        "worker-001",
        worker["lease_token"],
        "synthetic-host-worker",
        "synthetic-thread-worker",
    )
    reviewer_auth = auth(
        "reviewer-001",
        reviewer["lease_token"],
        "synthetic-host-reviewer",
        "synthetic-thread-reviewer",
    )
    controller_auth = auth(
        "controller-001",
        controller_token,
        "synthetic-host-controller",
        "synthetic-thread-controller",
    )

    coordinated_post(
        worker_auth,
        "--entry-type",
        "PARTIAL_RESULT",
        "--subject-key",
        "demo.interface",
        "--scope",
        "task/shared",
        "--content",
        "Worker produced a candidate interface.",
        "--evidence-ref",
        "synthetic/interface.json",
        "--confidence",
        "0.85",
    )
    coordinated_post(
        worker_auth,
        "--entry-type",
        "FACT_CONFIRMED",
        "--subject-key",
        "demo.fixture",
        "--scope",
        "task/shared",
        "--content",
        "Synthetic fixture exists and is readable.",
        "--evidence-ref",
        "synthetic/fixture.json",
        "--confidence",
        "1",
    )
    conflict = coordinated_post(
        reviewer_auth,
        "--entry-type",
        "CONFLICT_RECORDED",
        "--subject-key",
        "demo.interface",
        "--scope",
        "task/shared",
        "--content",
        "The artifact exists, but evidence is insufficient for completion.",
        "--evidence-ref",
        "synthetic/review.json",
        "--confidence",
        "0.9",
    )
    coordinated_post(
        controller_auth,
        "--entry-type",
        "LOCAL_DECISION",
        "--subject-key",
        "demo.interface",
        "--scope",
        "task/shared",
        "--content",
        "Keep the candidate and preserve the review disagreement.",
        "--confidence",
        "1",
    )
    coordinated_post(
        controller_auth,
        "--entry-type",
        "CURRENT_BEST_MODEL",
        "--subject-key",
        "demo.interface",
        "--scope",
        "task/shared",
        "--content",
        "Candidate exists; completion remains unclaimed.",
        "--confidence",
        "0.8",
    )
    controller_resolve_version = context_version(controller_auth)
    brain(
        "resolve",
        "--group-id",
        GROUP_ID,
        *controller_auth,
        "--scope",
        "task/shared",
        "--expected-view-version",
        str(controller_resolve_version),
        "--target-entry-id",
        conflict["entry_id"],
        "--status",
        "resolved",
        "--resolution",
        "Preserve both claims; accept only candidate status.",
    )

    # A new process reads shared state; it does not receive prior chat context.
    recovered = brain(
        "context",
        "--group-id",
        GROUP_ID,
        *reviewer_auth,
        "--scope",
        "task/shared",
    )
    assert recovered["shared_semantic_hash"]

    brain(
        "freeze",
        "--group-id",
        GROUP_ID,
        *controller_auth,
        "--reason",
        "Synthetic demonstration complete.",
    )
    brain(
        "handoff",
        "--group-id",
        GROUP_ID,
        *controller_auth,
        "--summary",
        "Three synthetic roles shared state and preserved disagreement.",
        "--evidence-ref",
        "synthetic/interface.json",
        "--evidence-ref",
        "synthetic/review.json",
        "--loopx-reconciliation-ref",
        "synthetic/loopx-reconciliation.json",
    )
    brain(
        "close",
        "--group-id",
        GROUP_ID,
        *controller_auth,
        "--reason",
        "Handoff created; revoke all memberships.",
        "--retention",
        "archive",
    )
    receipt = run(
        VERIFY,
        "--group-dir",
        str(RUNTIME / GROUP_ID),
        "--protected-snapshot",
        str(protected_snapshot),
        "--output",
        str(RUNTIME / "verification-receipt.json"),
    )
    status = brain("status", "--group-id", GROUP_ID)
    print(
        json.dumps(
            {
                "demo_status": "PASS",
                "runtime_state": status["state"],
                "active_members": status["counts"]["active_members"],
                "event_count": status["counts"]["events"],
                "verification": receipt["status"],
                "semantic_hash": status["semantic_hash"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
