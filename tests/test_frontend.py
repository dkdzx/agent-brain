from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_brain.workgroup_status_frontend import build_workgroup_status, render_html


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class FrontendProjectionTest(unittest.TestCase):
    def test_status_page_separates_running_and_archived_groups(self) -> None:
        html = render_html()
        self.assertIn("运行中的工作组", html)
        self.assertIn("已归档的工作组", html)
        self.assertNotIn("最近结束的工作组", html)

    def test_verified_member_task_title_wins_over_delegation_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-brain-title-") as temp:
            root = Path(temp)
            group = root / "group-001"
            now = datetime.now().astimezone()
            write_json(
                group / "group.json",
                {
                    "group_id": "group-001",
                    "task_id": "task-001",
                    "state": "ACTIVE",
                    "controller_member_id": "controller",
                    "created_at": now.isoformat(timespec="seconds"),
                },
            )
            write_json(
                group / "members.json",
                {
                    "members": {
                        "controller": {
                            "member_id": "controller",
                            "thread_id": "thread-001",
                            "codex_task_title": "工作组一｜总控",
                            "codex_task_title_source": "codex_app.list_threads",
                            "role": "controller",
                            "active": True,
                            "status": "ACTIVE",
                            "lease_expires_at": (
                                now + timedelta(hours=1)
                            ).isoformat(timespec="seconds"),
                        }
                    }
                },
            )
            write_json(
                root / "titles.json",
                {
                    "threads": {
                        "thread-001": (
                            "<codex_delegation><source_thread_id>bad</source_thread_id>"
                        )
                    }
                },
            )

            status = build_workgroup_status(
                root,
                codex_state_db=root / "missing.sqlite",
                title_map_path=root / "titles.json",
            )
            member = status["active_groups"][0]["members"][0]

            self.assertEqual(member["conversation_title"], "工作组一｜总控")
            self.assertEqual(
                member["conversation_title_source"],
                "member_verified_codex_task_title",
            )
            self.assertNotIn(
                "codex_delegation",
                json.dumps(status, ensure_ascii=False),
            )

    def test_delegation_text_fails_closed_when_no_actual_title(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-brain-title-") as temp:
            root = Path(temp)
            group = root / "group-001"
            now = datetime.now().astimezone()
            write_json(
                group / "group.json",
                {
                    "group_id": "group-001",
                    "task_id": "task-001",
                    "state": "ACTIVE",
                    "controller_member_id": "controller",
                    "created_at": now.isoformat(timespec="seconds"),
                },
            )
            write_json(
                group / "members.json",
                {
                    "members": {
                        "controller": {
                            "member_id": "controller",
                            "thread_id": "thread-001",
                            "role": "controller",
                            "active": True,
                            "status": "ACTIVE",
                            "lease_expires_at": (
                                now + timedelta(hours=1)
                            ).isoformat(timespec="seconds"),
                        }
                    }
                },
            )
            write_json(
                root / "titles.json",
                {
                    "threads": {
                        "thread-001": (
                            "<codex_delegation><source_thread_id>bad</source_thread_id>"
                        )
                    }
                },
            )

            status = build_workgroup_status(
                root,
                codex_state_db=root / "missing.sqlite",
                title_map_path=root / "titles.json",
            )
            member = status["active_groups"][0]["members"][0]

            self.assertEqual(member["conversation_title"], "任务名称待同步")
            self.assertEqual(
                member["conversation_title_source"],
                "unresolved_fail_closed",
            )
            self.assertNotIn(
                "codex_delegation",
                json.dumps(status, ensure_ascii=False),
            )

    def test_removed_members_are_not_visible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-brain-frontend-") as temp:
            root = Path(temp)
            group = root / "group-001"
            now = datetime.now().astimezone()
            write_json(
                group / "group.json",
                {
                    "group_id": "group-001",
                    "task_id": "task-001",
                    "objective": "frontend filtering",
                    "state": "ACTIVE",
                    "controller_member_id": "controller",
                    "created_at": now.isoformat(timespec="seconds"),
                },
            )
            write_json(
                group / "members.json",
                {
                    "members": {
                        "controller": {
                            "member_id": "controller",
                            "role": "controller",
                            "active": True,
                            "status": "ACTIVE",
                            "lease_expires_at": (
                                now + timedelta(hours=1)
                            ).isoformat(timespec="seconds"),
                        },
                        "removed-reviewer": {
                            "member_id": "removed-reviewer",
                            "role": "reviewer",
                            "active": False,
                            "status": "REVOKED",
                            "revoked_at": now.isoformat(timespec="seconds"),
                        },
                    }
                },
            )

            status = build_workgroup_status(
                root,
                codex_state_db=root / "missing.sqlite",
                title_map_path=root / "missing-titles.json",
            )
            projected = status["active_groups"][0]

            self.assertEqual(projected["active_member_count"], 1)
            self.assertEqual(projected["total_member_count"], 1)
            self.assertEqual(
                [member["member_id"] for member in projected["members"]],
                ["controller"],
            )
            self.assertNotIn(
                "removed-reviewer",
                json.dumps(status, ensure_ascii=False),
            )


if __name__ == "__main__":
    unittest.main()
