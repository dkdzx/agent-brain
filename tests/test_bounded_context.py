from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_brain.workgroup_brain import canonical_bytes, filtered_context


class BoundedContextTest(unittest.TestCase):
    def test_adaptive_budget_selects_measured_coverage_step(self) -> None:
        members = [
            {
                "member_id": f"member-{index}",
                "role": "reviewer",
                "host_id": "local",
                "thread_id": f"thread-{index}",
                "codex_task_title": f"Task {index}",
                "scopes": ["task/shared"],
                "read_scope": ["task/shared"],
                "write_scope": ["task/shared"],
                "active": True,
                "status": "ACTIVE",
                "added_at": "2026-01-01T00:00:00+00:00",
                "joined_at": "2026-01-01T00:00:00+00:00",
                "lease_expires_at": "2030-01-01T00:00:00+00:00",
                "revoked_at": None,
            }
            for index in range(3)
        ]
        entries = [
            {
                "entry_id": f"adaptive-entry-{seq:03d}",
                "entry_seq": seq,
                "entry_type": "FACT_CONFIRMED",
                "subject_key": f"subject-{seq % 7}",
                "scope": "task/shared",
                "author_member_id": f"member-{seq % 3}",
                "author_role": "reviewer",
                "created_at": "2026-01-01T00:00:00+00:00",
                "status": "active",
                "confidence": 0.9,
                "content": {
                    "core_claim": f"Claim {seq} " + ("x" * 900),
                    "strongest_evidence": f"evidence-{seq}",
                },
                "evidence_refs": [f"evidence/{seq}.json"],
            }
            for seq in range(1, 121)
        ]
        view = {
            "group_id": "adaptive-context",
            "task_id": "task-adaptive",
            "state": "ACTIVE",
            "status": "ACTIVE",
            "view_version": 120,
            "event_chain_head": "a" * 64,
            "semantic_hash": "b" * 64,
            "members": members,
            "entries": entries,
            "open_question_entry_ids": [],
            "open_conflict_entry_ids": [],
            "current_best_model_entry_ids": [],
            "context_policy": {
                "mode": "adaptive",
                "minimum_budget_bytes": 32768,
                "budget_bytes": 1048576,
                "budget_ladder_bytes": [
                    32768,
                    65536,
                    131072,
                    262144,
                    524288,
                    1048576,
                ],
                "target_coverage": 0.9,
                "minimum_marginal_gain": 0.03,
                "max_entries": 512,
                "member_position_card_limit": 15,
            },
        }

        context = filtered_context(view, members[0], "task/shared")

        selected = context["context_budget"]["selected_budget_bytes"]
        available_curve = [
            row
            for row in context["context_budget_curve"]
            if row["available"]
        ]
        selected_row = next(
            row for row in available_curve if row["budget_bytes"] == selected
        )
        self.assertIn(
            selected,
            {32768, 65536, 131072, 262144, 524288, 1048576},
        )
        self.assertGreaterEqual(selected_row["coverage_score"], 0.9)
        selected_curve_index = available_curve.index(selected_row)
        if selected_curve_index + 1 < len(available_curve):
            next_row = available_curve[selected_curve_index + 1]
            self.assertLess(
                next_row["coverage_score"] - selected_row["coverage_score"],
                0.03,
            )
        self.assertLessEqual(len(canonical_bytes(context)), selected)
        self.assertEqual(context["retrieval"]["full_visible_entry_count"], 120)
        self.assertTrue(context["retrieval"]["exact_entry_lookup_available"])
        self.assertIn(
            context["context_budget"]["selected_reason"],
            {
                "target_coverage_reached",
                "target_coverage_and_marginal_gain_elbow_reached",
            },
        )

    def test_position_cards_survive_bounded_injection_slice(self) -> None:
        members = []
        entries = []
        for member_index in range(15):
            member_id = f"member-{member_index:02d}"
            members.append(
                {
                    "member_id": member_id,
                    "role": "reviewer",
                    "host_id": "local",
                    "thread_id": f"thread-{member_index:02d}",
                    "codex_task_title": f"Actual task {member_index:02d}",
                    "scopes": ["task/shared"],
                    "read_scope": ["task/shared"],
                    "write_scope": ["task/shared"],
                    "active": member_index < 10,
                    "status": "ACTIVE" if member_index < 10 else "REVOKED",
                    "added_at": "2026-01-01T00:00:00+00:00",
                    "joined_at": "2026-01-01T00:00:00+00:00",
                    "lease_expires_at": "2030-01-01T00:00:00+00:00",
                    "revoked_at": (
                        None
                        if member_index < 10
                        else "2026-01-02T00:00:00+00:00"
                    ),
                }
            )
            for item_index in range(8):
                seq = member_index * 8 + item_index + 1
                entry_type = (
                    "CONFLICT_RECORDED"
                    if item_index == 7
                    else "FACT_CONFIRMED"
                )
                entries.append(
                    {
                        "entry_id": f"entry-{seq:03d}",
                        "entry_seq": seq,
                        "entry_type": entry_type,
                        "subject_key": f"subject-{member_index:02d}",
                        "scope": "task/shared",
                        "author_member_id": member_id,
                        "author_role": "reviewer",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "status": "active",
                        "confidence": 0.9,
                        "content": {
                            "core_claim": (
                                f"Member {member_index} claim {item_index} "
                                + "x" * 600
                            ),
                            "strongest_evidence": f"evidence-{seq}",
                            "strongest_counterevidence": f"counter-{seq}",
                            "claim_ceiling": "diagnostic only",
                            "model_gate_status": "compliant",
                            "signing_authority": "none",
                        },
                        "evidence_refs": [f"evidence/{seq}.json"],
                    }
                )

        view = {
            "group_id": "bounded-context",
            "task_id": "task-001",
            "state": "ACTIVE",
            "status": "ACTIVE",
            "view_version": 120,
            "event_chain_head": "a" * 64,
            "semantic_hash": "b" * 64,
            "members": members,
            "entries": entries,
            "open_question_entry_ids": [],
            "open_conflict_entry_ids": [
                item["entry_id"]
                for item in entries
                if item["entry_type"] == "CONFLICT_RECORDED"
            ],
            "current_best_model_entry_ids": [],
            "context_policy": {
                "budget_bytes": 32768,
                "max_entries": 48,
                "member_position_card_limit": 15,
            },
        }
        requesting_member = members[0]
        context = filtered_context(
            view,
            requesting_member,
            "task/shared",
        )

        self.assertFalse(context["context_is_complete_workgroup_memory"])
        self.assertLessEqual(len(canonical_bytes(context)), 32768)
        self.assertEqual(len(context["member_position_cards"]), 15)
        self.assertEqual(
            context["member_position_cards"][0]["codex_task_title"],
            "Actual task 00",
        )
        historical = [
            card
            for card in context["member_position_cards"]
            if not card["member_active"]
        ]
        self.assertEqual(len(historical), 5)
        self.assertTrue(
            all(
                card["participation_status"]
                == "historical_diagnostic_evidence_only"
                for card in historical
            )
        )
        self.assertGreater(context["retrieval"]["omitted_entry_count"], 0)
        self.assertTrue(context["retrieval"]["exact_entry_lookup_available"])


if __name__ == "__main__":
    unittest.main()
