from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_brain.workgroup_status_frontend import (
    anonymous_demo_runtime_root,
    build_workgroup_status,
    exact_entry_projection,
    group_detail_projection,
    inject_demo_return_link,
    render_html,
    structural_issues_projection,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class FrontendProjectionTest(unittest.TestCase):
    def test_anonymous_demo_has_return_project_state_machine_link(self) -> None:
        html = inject_demo_return_link(render_html())
        self.assertIn('href="http://127.0.0.1:8766/?demo=1"', html)
        self.assertIn("返回项目状态机", html)
        self.assertIn("position:fixed", html)

    def test_status_page_separates_running_and_archived_groups(self) -> None:
        html = render_html()
        self.assertIn("运行中的工作组", html)
        self.assertIn("已归档的工作组", html)
        self.assertNotIn("最近结束的工作组", html)

    def test_anonymous_demo_exposes_bounded_context_and_event_semantics(self) -> None:
        root = anonymous_demo_runtime_root()
        status = build_workgroup_status(
            root,
            codex_state_db=root / "missing.sqlite",
            title_map_path=root / "missing-titles.json",
        )
        detail = group_detail_projection(
            root,
            "demo-active-group",
            codex_state_db=root / "missing.sqlite",
            title_map_path=root / "missing-titles.json",
        )
        self.assertEqual(status["active_group_count"], 1)
        self.assertEqual(status["archived_group_count"], 1)
        self.assertIsNotNone(detail)
        assert detail is not None
        context = detail["context"]
        self.assertTrue(context["available"])
        self.assertFalse(context["context_is_complete_workgroup_memory"])
        self.assertTrue(context["complete_workgroup_memory"]["retrieval_only"])
        self.assertGreater(context["context_budget"]["final_bytes"], 0)
        self.assertGreater(context["context_budget"]["approx_tokens_div4"], 0)
        self.assertEqual(
            context["context_budget"]["selected_budget_bytes"],
            524288,
        )
        self.assertEqual(context["context_budget"]["elbow"], 524288)
        self.assertEqual(
            context["context_budget"]["doubling_marginal_gain"],
            0.0,
        )
        self.assertEqual(
            [item["budget_bytes"] for item in context["context_budget"]["budget_tiers"]],
            [32768, 65536, 131072, 262144, 524288, 1048576],
        )
        self.assertEqual(len(context["budget_curve"]), 6)
        self.assertEqual(
            context["context_budget"]["selected_reason"],
            "target_coverage_and_marginal_gain_elbow_reached",
        )
        self.assertEqual(
            context["event_summary"]["total_stream_count"],
            180,
        )
        self.assertEqual(context["event_summary"]["core_event_count"], 4)
        self.assertEqual(context["event_summary"]["real_effect_count"], 1)
        self.assertEqual(context["event_summary"]["evidence_count"], 4)
        self.assertEqual(detail["structural_issues"]["counts"]["all"], 2)
        self.assertEqual(detail["structural_issues"]["counts"]["active"], 1)
        self.assertEqual(detail["structural_issues"]["issues"][0]["status"], "active")
        self.assertEqual(
            sorted(
                entry["core_event_seq"]
                for entry in context["entries"]
                if entry["is_core_event"]
            ),
            [1, 2, 3, 4],
        )
        projection = next(
            entry
            for entry in context["entries"]
            if entry["entry_id"] == "demo-entry-projection"
        )
        self.assertFalse(projection["is_core_event"])
        self.assertEqual(projection["event_category"], "核算/投影/ABSTAIN")
        self.assertEqual(len(context["historical_diagnostic_evidence"]), 1)
        self.assertEqual(
            context["historical_diagnostic_evidence"][0]["codex_task_title"],
            "已退出匿名诊断任务",
        )
        self.assertEqual(detail["task_pool"]["conflict_count"], 0)
        self.assertTrue(detail["task_pool"]["one_person_one_task"])
        rendered = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("demo-thread", rendered)
        self.assertNotIn("demo-host", rendered)

    def test_exact_entry_is_reference_read_and_does_not_leak_identity(self) -> None:
        root = anonymous_demo_runtime_root()
        payload = exact_entry_projection(
            root,
            "demo-active-group",
            "demo-entry-decision",
            codex_state_db=root / "missing.sqlite",
            title_map_path=root / "missing-titles.json",
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(
            payload["schema_version"],
            "agent_brain_workgroup_frontend_exact_entry_v1",
        )
        self.assertIn("exact_content", payload["entry"])
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("demo-thread", rendered)
        self.assertNotIn("demo-host", rendered)
        self.assertIn("按引用读取", payload["notice"])

    def test_empty_active_groups_are_not_listed_as_running_projects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-brain-empty-group-") as temp:
            root = Path(temp)
            group = root / "empty-group"
            now = datetime.now().astimezone()
            write_json(
                group / "group.json",
                {
                    "group_id": "empty-group",
                    "task_id": "DECISION150_AB_FINAL_CONVERGENCE_PREFLIGHT_FIRST_MODULE",
                    "display_name": "DECISION150_AB_FINAL_CONVERGENCE_PREFLIGHT_FIRST_MODULE",
                    "objective": "这是一个没有有效活动成员的过期投影",
                    "state": "ACTIVE",
                    "created_at": now.isoformat(timespec="seconds"),
                },
            )
            write_json(group / "members.json", {"members": {}})

            status = build_workgroup_status(
                root,
                codex_state_db=root / "missing.sqlite",
                title_map_path=root / "missing-titles.json",
            )

            self.assertEqual(status["active_group_count"], 0)
            self.assertEqual(status["archived_group_count"], 0)
            self.assertEqual(status["hidden_empty_group_count"], 1)
            self.assertEqual(status["active_member_count"], 0)
            self.assertEqual(status["hidden_empty_groups"][0]["reason"], "ACTIVE_NO_ACTIVE_MEMBERS")
            self.assertNotIn("empty-group", [item["group_id"] for item in status["active_groups"]])

    def test_render_contract_contains_event_toggle_and_memory_boundary(self) -> None:
        html = render_html()
        for expected in (
            "当前注入切片",
            "完整工作组记忆",
            "当前核心事件",
            "工作组总流水号",
            "查看全部流水",
            "真实效果",
            "证据事件",
            "member_position_cards",
            "/api/entry",
            "任务池",
            "历史诊断证据区",
            "显示模块",
            "data-module-toggle",
            "module-scroll",
            "position-timeline",
            "重大结构性问题",
            "data-issue-filter",
            "/api/structural-issues",
        ):
            self.assertIn(expected, html)

    def test_structural_issue_projection_filters_by_status_and_severity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-brain-issues-") as temp:
            root = Path(temp)
            group = root / "group-001"
            write_json(group / "group.json", {"group_id": "group-001", "state": "ACTIVE"})
            write_json(
                group / "structural_issues.json",
                {
                    "schema_version": "agent_brain_structural_issue_v1",
                    "issues": [
                        {"issue_id": "issue-a", "title": "active high", "severity": "high", "status": "active"},
                        {"issue_id": "issue-b", "title": "resolved medium", "severity": "medium", "status": "resolved"},
                    ],
                },
            )
            active = structural_issues_projection(root, "group-001", status="active")
            high = structural_issues_projection(root, "group-001", severity="high")
            self.assertEqual([item["issue_id"] for item in active["issues"]], ["issue-a"])
            self.assertEqual([item["issue_id"] for item in high["issues"]], ["issue-a"])
            self.assertEqual(active["counts"]["all"], 2)

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
