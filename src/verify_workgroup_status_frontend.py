from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from workgroup_status_frontend import (
    build_loopx_projection,
    build_workgroup_status,
    render_html,
)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    checks: list[str] = []
    now = datetime.now().astimezone()
    future = (now + timedelta(hours=2)).isoformat(timespec="seconds")
    past = (now - timedelta(hours=2)).isoformat(timespec="seconds")

    with tempfile.TemporaryDirectory(prefix="agent-brain-workgroup-status-") as temp:
        root = Path(temp)
        state_db = root / "state.sqlite"
        connection = sqlite3.connect(state_db)
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT)"
        )
        connection.execute(
            "INSERT INTO threads (id, name, title) VALUES (?, ?, ?)",
            ("thread-a", None, "施工｜人口与家户"),
        )
        connection.commit()
        connection.close()
        title_map = root / "CODEX_THREAD_TITLE_MAP.json"
        write_json(
            title_map,
            {
                "threads": {
                    "thread-a": "施工V2｜Luna Max｜人口与家户"
                }
            },
        )
        active = root / "active-group"
        write_json(
            active / "group.json",
            {
                "group_id": "active-group",
                "task_id": "TASK-A",
                "objective": "fixture",
                "state": "ACTIVE",
                "expires_at": future,
            },
        )
        write_json(
            active / "members.json",
            {
                "members": {
                    "controller": {
                        "active": True,
                        "status": "ACTIVE",
                        "role": "controller",
                        "lease_expires_at": future,
                        "host_id": "must-not-leak",
                        "thread_id": "thread-a",
                        "token_hash": "must-not-leak",
                    },
                    "worker": {
                        "active": True,
                        "status": "ACTIVE",
                        "role": "worker",
                        "lease_expires_at": future,
                    },
                    "expired": {
                        "active": True,
                        "status": "ACTIVE",
                        "role": "reviewer",
                        "lease_expires_at": past,
                    },
                }
            },
        )
        archived = root / "archived-group"
        write_json(
            archived / "group.json",
            {
                "group_id": "archived-group",
                "task_id": "TASK-OLD",
                "state": "ARCHIVED",
                "closed_at": now.isoformat(timespec="seconds"),
            },
        )
        write_json(
            archived / "members.json",
            {
                "members": {
                    "reviewer": {
                        "active": False,
                        "status": "REVOKED",
                        "role": "reviewer",
                    }
                }
            },
        )

        status = build_workgroup_status(
            root,
            codex_state_db=state_db,
            title_map_path=title_map,
        )
        assert status["active_group_count"] == 1
        checks.append("active_group_count")
        assert status["active_member_count"] == 2
        checks.append("active_member_count")
        assert status["role_counts"] == {"controller": 1, "worker": 1}
        checks.append("role_counts")
        assert status["active_groups"][0]["display_title"] == "工作组"
        assert status["active_groups"][0]["display_instance"] == "工作组实例"
        checks.append("chinese_group_display")
        assert (
            status["active_groups"][0]["members"][0]["conversation_title"]
            == "施工V2｜Luna Max｜人口与家户"
        )
        checks.append("codex_conversation_title")
        assert status["archived_group_count"] == 1
        assert status["recent_groups"][0]["members"] == []
        assert status["recent_groups"][0]["total_member_count"] == 0
        checks.append("exited_members_removed_from_frontend")
        checks.append("archived_group_count")

        serialized = json.dumps(status, ensure_ascii=False)
        for forbidden in (
            "must-not-leak",
            '"host_id":',
            '"thread_id":',
            '"token_hash":',
        ):
            assert forbidden not in serialized
        checks.append("private_identity_not_exposed")

        loopx = build_loopx_projection(status)
        agents = loopx["agent_management_projection"]["agents"]
        assert len(agents) == 2
        checks.append("loopx_member_projection")
        assert (
            loopx["agent_management_projection"]["workgroup_summary"][
                "active_member_count"
            ]
            == 2
        )
        checks.append("loopx_summary")

        active_group = json.loads(
            (active / "group.json").read_text(encoding="utf-8")
        )
        active_group["state"] = "ARCHIVED"
        active_group["closed_at"] = now.isoformat(timespec="seconds")
        write_json(active / "group.json", active_group)
        closed_status = build_workgroup_status(
            root,
            codex_state_db=state_db,
            title_map_path=title_map,
        )
        assert closed_status["active_group_count"] == 0
        assert closed_status["active_member_count"] == 0
        checks.append("close_transition_returns_to_zero")

        html = render_html()
        assert "工作组" in html
        assert "共享脑" not in html
        assert "/api/status" in html
        assert "roleLabels" not in html
        assert "member.role_label" not in html
        assert 'member.is_controller ? `<div class="member-role">总控</div>`' in html
        checks.append("human_frontend_copy")

    print(
        json.dumps(
            {
                "ok": True,
                "checks_passed": len(checks),
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
