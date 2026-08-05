from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_brain.workgroup_status_frontend import build_workgroup_status


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class FrontendProjectionTest(unittest.TestCase):
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
