from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BRAIN = REPO / "src" / "agent_brain" / "workgroup_brain.py"


class StrictCoordinationTest(unittest.TestCase):
    def run_brain(
        self,
        root: Path,
        *args: str,
        expected_code: int = 0,
    ) -> dict:
        completed = subprocess.run(
            [sys.executable, str(BRAIN), "--root", str(root), *args],
            cwd=REPO,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            expected_code,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        stream = completed.stdout if expected_code == 0 else completed.stderr
        return json.loads(stream)

    @staticmethod
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

    def create_group(
        self,
        root: Path,
        group_id: str,
        controller_id: str,
        host_id: str,
        thread_id: str,
    ) -> dict:
        return self.run_brain(
            root,
            "create",
            "--group-id",
            group_id,
            "--task-id",
            f"task-{group_id}",
            "--objective",
            "Exercise strict coordination",
            "--controller-member-id",
            controller_id,
            "--host-id",
            host_id,
            "--thread-id",
            thread_id,
            "--scope",
            "task/shared",
            "--authority-bundle-sha256",
            "a" * 64,
            "--loopx-goal-id",
            f"goal-{group_id}",
        )

    def test_context_version_gate_and_single_group_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            created = self.create_group(
                root,
                "group-one",
                "controller-one",
                "host-controller-one",
                "thread-controller-one",
            )
            controller_auth = self.auth(
                "controller-one",
                created["lease_token"],
                "host-controller-one",
                "thread-controller-one",
            )
            worker = self.run_brain(
                root,
                "add-member",
                "--group-id",
                "group-one",
                "--actor-member-id",
                "controller-one",
                f"--lease-token={created['lease_token']}",
                "--actor-host-id",
                "host-controller-one",
                "--actor-thread-id",
                "thread-controller-one",
                "--member-id",
                "worker-one",
                "--role",
                "worker",
                "--host-id",
                "host-worker-one",
                "--thread-id",
                "thread-worker-one",
                "--scope",
                "task/shared",
            )
            worker_auth = self.auth(
                "worker-one",
                worker["lease_token"],
                "host-worker-one",
                "thread-worker-one",
            )

            rejected = self.run_brain(
                root,
                "post",
                "--group-id",
                "group-one",
                *worker_auth,
                "--entry-type",
                "PARTIAL_RESULT",
                "--subject-key",
                "strict.unsynced",
                "--scope",
                "task/shared",
                "--content",
                "Must not publish without context.",
                expected_code=2,
            )
            self.assertEqual(rejected["error_code"], "CONTEXT_VERSION_REQUIRED")

            worker_context = self.run_brain(
                root,
                "context",
                "--group-id",
                "group-one",
                *worker_auth,
                "--scope",
                "task/shared",
            )
            self.run_brain(
                root,
                "post",
                "--group-id",
                "group-one",
                *worker_auth,
                "--expected-view-version",
                str(worker_context["view_version"]),
                "--entry-type",
                "PARTIAL_RESULT",
                "--subject-key",
                "strict.first",
                "--scope",
                "task/shared",
                "--content",
                "Published from fresh context.",
            )

            controller_context = self.run_brain(
                root,
                "context",
                "--group-id",
                "group-one",
                *controller_auth,
                "--scope",
                "task/shared",
            )
            self.run_brain(
                root,
                "post",
                "--group-id",
                "group-one",
                *controller_auth,
                "--expected-view-version",
                str(controller_context["view_version"]),
                "--entry-type",
                "LOCAL_DECISION",
                "--subject-key",
                "strict.advance",
                "--scope",
                "task/shared",
                "--content",
                "Advance the shared view.",
            )

            stale = self.run_brain(
                root,
                "post",
                "--group-id",
                "group-one",
                *worker_auth,
                "--expected-view-version",
                str(worker_context["view_version"]),
                "--entry-type",
                "PARTIAL_RESULT",
                "--subject-key",
                "strict.stale",
                "--scope",
                "task/shared",
                "--content",
                "This write is based on stale context.",
                expected_code=2,
            )
            self.assertEqual(stale["error_code"], "CONTEXT_STALE")

            second = self.create_group(
                root,
                "group-two",
                "controller-two",
                "host-controller-two",
                "thread-controller-two",
            )
            conflict = self.run_brain(
                root,
                "add-member",
                "--group-id",
                "group-two",
                "--actor-member-id",
                "controller-two",
                f"--lease-token={second['lease_token']}",
                "--actor-host-id",
                "host-controller-two",
                "--actor-thread-id",
                "thread-controller-two",
                "--member-id",
                "worker-copy",
                "--role",
                "worker",
                "--host-id",
                "host-worker-one",
                "--thread-id",
                "thread-worker-one",
                "--scope",
                "task/shared",
                expected_code=2,
            )
            self.assertEqual(
                conflict["error_code"],
                "THREAD_ALREADY_ACTIVE_IN_OTHER_GROUP",
            )


if __name__ == "__main__":
    unittest.main()
