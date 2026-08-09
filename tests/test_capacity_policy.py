from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BRAIN = REPO / "src" / "agent_brain" / "workgroup_brain.py"


class CapacityPolicyTest(unittest.TestCase):
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
    def actor_auth(token: str) -> list[str]:
        return [
            "--actor-member-id",
            "controller",
            f"--lease-token={token}",
            "--actor-host-id",
            "host-controller",
            "--actor-thread-id",
            "thread-controller",
        ]

    def test_defaults_are_ten_parallel_and_fifteen_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            created = self.run_brain(
                root,
                "create",
                "--group-id",
                "capacity-defaults",
                "--task-id",
                "task-capacity-defaults",
                "--objective",
                "Verify capacity defaults",
                "--controller-member-id",
                "controller",
                "--host-id",
                "host-controller",
                "--thread-id",
                "thread-controller",
                "--scope",
                "task/shared",
                "--authority-bundle-sha256",
                "a" * 64,
                "--loopx-goal-id",
                "goal-capacity-defaults",
            )
            status = self.run_brain(
                root,
                "status",
                "--group-id",
                "capacity-defaults",
            )
            self.assertEqual(
                status["capacity_policy"]["parallel_active_task_limit"], 10
            )
            self.assertEqual(status["capacity_policy"]["member_limit"], 15)
            self.assertTrue(
                status["capacity_policy"]["member_limit_includes_controller"]
            )
            self.assertEqual(status["context_policy"]["mode"], "adaptive")
            self.assertEqual(
                status["context_policy"]["minimum_budget_bytes"], 32768
            )
            self.assertEqual(status["context_policy"]["budget_bytes"], 1048576)
            self.assertEqual(status["context_policy"]["max_entries"], 4096)
            self.assertEqual(
                status["context_policy"]["budget_ladder_bytes"],
                [32768, 65536, 131072, 262144, 524288, 1048576],
            )
            self.assertEqual(
                status["context_policy"]["member_position_card_limit"], 15
            )
            self.assertTrue(
                status["context_policy"][
                    "context_is_injection_slice_not_complete_memory"
                ]
            )
            self.assertTrue(created["lease_token"].startswith("wg_"))

    def test_member_limit_includes_controller_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            created = self.run_brain(
                root,
                "create",
                "--group-id",
                "capacity-small",
                "--task-id",
                "task-capacity-small",
                "--objective",
                "Verify member limit",
                "--controller-member-id",
                "controller",
                "--host-id",
                "host-controller",
                "--thread-id",
                "thread-controller",
                "--scope",
                "task/shared",
                "--member-limit",
                "2",
                "--parallel-active-task-limit",
                "1",
                "--authority-bundle-sha256",
                "b" * 64,
                "--loopx-goal-id",
                "goal-capacity-small",
            )
            self.run_brain(
                root,
                "add-member",
                "--group-id",
                "capacity-small",
                *self.actor_auth(created["lease_token"]),
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
            rejected = self.run_brain(
                root,
                "add-member",
                "--group-id",
                "capacity-small",
                *self.actor_auth(created["lease_token"]),
                "--member-id",
                "worker-two",
                "--role",
                "worker",
                "--host-id",
                "host-worker-two",
                "--thread-id",
                "thread-worker-two",
                "--scope",
                "task/shared",
                expected_code=2,
            )
            self.assertEqual(
                rejected["error_code"], "WORKGROUP_MEMBER_LIMIT_REACHED"
            )


if __name__ == "__main__":
    unittest.main()
