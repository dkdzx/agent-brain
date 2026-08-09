from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_brain.workgroup_memory_shadow import (  # noqa: E402
    ShadowError,
    append_shadow_entry,
    approve_graphiti_candidate,
    build_injection_slice,
    checkpoint_memory,
    diagnostics_projection,
    external_reader_verify,
    get_entry,
    import_approved_graphiti_candidate,
    import_source_group,
    rebuild_shadow_view,
    recover_shadow_view,
    verify_shadow_events,
)
from agent_brain.workgroup_memory_shadow_frontend import render_html  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ShadowMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source-group"
        self.shadow = root / "shadow-group"
        self.source.mkdir()
        self.thread_controller = "thread-controller-anon"
        self.thread_worker = "thread-worker-anon"
        self.thread_historical = "thread-historical-anon"
        write_json(
            self.source / "group.json",
            {
                "schema": "synthetic_workgroup_v1",
                "group_id": "synthetic-shadow-group-001",
                "display_name": "Synthetic bounded memory review",
                "project_id": "synthetic-project",
                "status": "ACTIVE",
                "controller_thread_id": self.thread_controller,
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
            self.source / "members.json",
            {
                "schema": "synthetic_members_v1",
                "group_id": "synthetic-shadow-group-001",
                "members": [
                    {"thread_id": self.thread_controller, "display_name": "Synthetic controller task", "role": "controller", "status": "ACTIVE"},
                    {"thread_id": self.thread_worker, "display_name": "Synthetic worker task", "role": "worker", "status": "ACTIVE"},
                    {"thread_id": self.thread_historical, "display_name": "Synthetic historical task", "role": "reviewer", "status": "QUEUED"},
                ],
            },
        )
        write_json(
            self.source / "task_pool.json",
            {
                "schema": "synthetic_task_pool_v1",
                "group_id": "synthetic-shadow-group-001",
                "one_member_one_task": True,
                "tasks": [
                    {"task_id": "SYNTH-TASK-01", "status": "CLAIMED_ACTIVE", "owner_thread_id": self.thread_worker, "scope": "bounded-recovery"},
                    {"task_id": "SYNTH-TASK-02", "status": "QUEUED", "owner_thread_id": None, "scope": "evidence-review"},
                ],
                "queued_next_step_tasks": [],
            },
        )
        write_json(
            self.source / "working_snapshot.json",
            {
                "schema": "synthetic_snapshot_v1",
                "group_id": "synthetic-shadow-group-001",
                "snapshot_version": 7,
                "current_goal": "Keep accepted state recoverable without replaying chat.",
                "active_tasks": ["SYNTH-TASK-01"],
                "latest_decisions": ["Use append-only evidence references.", "Regenerate a bounded slice before continuation."],
                "open_risks": ["A counterevidence entry must remain retrievable."],
                "step5_allowed": False,
                "business_world_write_allowed": False,
                "business_k3_effect": 0,
            },
        )
        write_json(
            self.source / "lanes" / "SYNTH-TASK-01" / "LANE_HANDOFF.json",
            {"status": "READY_FOR_REVIEW", "summary": "synthetic handoff", "evidence_refs": ["synthetic/evidence"]},
        )
        write_json(
            self.source / "lanes" / "SYNTH-TASK-01" / "RECEIPT.json",
            {"verdict": "PASS", "checks": ["source", "scope"]},
        )
        self.source_hashes = {
            path.relative_to(self.source): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def import_once(self) -> dict[str, object]:
        return import_source_group(self.source, self.shadow)

    def test_import_is_idempotent_and_external_reader_matches(self) -> None:
        first = self.import_once()
        count_before = len(verify_shadow_events(self.shadow))
        second = self.import_once()
        self.assertEqual(first["event_chain_head"], second["event_chain_head"])
        self.assertEqual(count_before, len(verify_shadow_events(self.shadow)))
        reader = external_reader_verify(self.source, self.shadow)
        self.assertEqual(reader["status"], "PASS")
        self.assertEqual(reader["members_source"], reader["members_imported"])
        self.assertEqual(reader["tasks_source"], reader["tasks_imported"])
        for relative, raw in self.source_hashes.items():
            self.assertEqual(raw, (self.source / relative).read_bytes())

    def test_cards_keep_real_titles_and_history_is_not_active(self) -> None:
        self.import_once()
        view = json.loads((self.shadow / "view.json").read_text(encoding="utf-8"))
        titles = {card["codex_task_title"] for card in view["member_position_cards"]}
        self.assertIn("Synthetic worker task", titles)
        historical = [card for card in view["member_position_cards"] if not card["member_active"]]
        self.assertEqual(len(historical), 1)
        self.assertEqual(historical[0]["participation_status"], "historical_diagnostic_evidence_only")
        self.assertTrue(all("<codex_delegation" not in str(card["codex_task_title"]) for card in view["member_position_cards"]))
        self.assertTrue(all(card["strongest_counterevidence"] for card in view["member_position_cards"]))

    def test_context_recovery_budget_get_entry_and_stale_rejection(self) -> None:
        self.import_once()
        result = build_injection_slice(
            self.shadow,
            target_thread_id=self.thread_worker,
            model_window_tokens=353000,
            remaining_token_reserve=100000,
        )
        context = result["context"]
        receipt = result["receipt"]
        self.assertEqual(receipt["injection_status"], "GENERATED_NOT_INJECTED")
        self.assertTrue(receipt["codex_platform_consumption_unknown"])
        self.assertLessEqual(receipt["bytes"], receipt["budget_bytes"])
        self.assertIn(receipt["budget_mode"], {"known_353k_normal", "known_353k_normal_reserve_adjusted"})
        self.assertTrue(any("strongest_counterevidence" in item.get("content", {}) for item in context["entries"]))
        entry_id = context["entries"][0]["entry_id"]
        self.assertTrue(get_entry(self.shadow, entry_id)["exact"])
        old_view = json.loads((self.shadow / "view.json").read_text(encoding="utf-8"))
        append_shadow_entry(
            self.shadow,
            member_id="member-" + self.thread_controller,
            entry_type="LOCAL_DECISION",
            content={"core_claim": "A new synthetic decision supersedes no project truth.", "strongest_counterevidence": "review pending"},
        )
        with self.assertRaises(ShadowError) as error:
            build_injection_slice(
                self.shadow,
                target_thread_id=self.thread_worker,
                expected_view_version=old_view["view_version"],
            )
        self.assertEqual(error.exception.code, "CONTEXT_STALE")
        (self.shadow / "view.json").unlink()
        recovered = recover_shadow_view(self.shadow)
        self.assertEqual(recovered["status"], "PASS")
        self.assertEqual(recovered["raw_events_lost"], 0)

    def test_candidates_graphiti_review_and_no_promotion(self) -> None:
        self.import_once()
        first = checkpoint_memory(self.shadow, reason="initial-checkpoint")
        same = checkpoint_memory(self.shadow, reason="initial-checkpoint")
        self.assertEqual(first["memory_id"], same["memory_id"])
        candidates_before = json.loads((self.shadow / "group_memory_candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(candidates_before["candidate_count"], 1)
        append_shadow_entry(
            self.shadow,
            member_id="member-" + self.thread_controller,
            entry_type="LOCAL_DECISION",
            content={"core_claim": "Second checkpoint decision.", "strongest_counterevidence": "still reviewable"},
        )
        newer = checkpoint_memory(self.shadow, reason="second-checkpoint")
        append_shadow_entry(
            self.shadow,
            member_id="member-" + self.thread_controller,
            entry_type="LOCAL_DECISION",
            content={"core_claim": "Third checkpoint decision.", "strongest_counterevidence": "still reviewable"},
        )
        newest = checkpoint_memory(self.shadow, reason="third-checkpoint")
        self.assertEqual(newer["subject_key"], newest["subject_key"])
        self.assertEqual(newest["supersedes"], newer["memory_id"])
        pending = json.loads((self.shadow / "GRAPHITI_REVIEW_QUEUE.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(pending["pending"]), 1)
        with self.assertRaises(ShadowError) as error:
            import_approved_graphiti_candidate(self.shadow, memory_id=first["memory_id"])
        self.assertEqual(error.exception.code, "GRAPHITI_APPROVAL_REQUIRED")
        approve_graphiti_candidate(self.shadow, memory_id=first["memory_id"], reviewed_by="synthetic-reviewer")
        receipt = import_approved_graphiti_candidate(self.shadow, memory_id=first["memory_id"])
        self.assertEqual(receipt["live_writes_performed"], 0)
        self.assertFalse(json.loads((self.shadow / "group_memory_index.json").read_text(encoding="utf-8"))["promoted_project_memory_ids"])
        self.assertFalse((self.shadow / "project_control").exists())

    def test_frontend_projection_has_chain_slice_checkpoint_and_recovery(self) -> None:
        self.import_once()
        checkpoint_memory(self.shadow, reason="frontend-checkpoint")
        build_injection_slice(self.shadow, target_thread_id=self.thread_worker)
        projection = diagnostics_projection(self.shadow)
        self.assertEqual(projection["event_chain"]["count"], 15)
        self.assertGreater(projection["position_cards"]["count"], 0)
        self.assertGreater(projection["context_slice"]["bytes"], 0)
        self.assertTrue(projection["context_slice"]["get_entry_available"])
        self.assertEqual(projection["checkpoints"]["candidate_count"], 1)
        self.assertGreater(projection["graphiti"]["pending_review"], 0)
        self.assertTrue(projection["recovery"]["codex_platform_consumption_unknown"])
        page = render_html(projection)
        self.assertIn("Complete workgroup memory", page)
        self.assertIn("Current injection slice", page)
        self.assertIn("Long-term candidates", page)
        self.assertIn("PLATFORM_CONSUMPTION_UNKNOWN", page)
        self.assertIn("setTimeout", page)

    def test_tamper_and_delete_event_fail_closed(self) -> None:
        self.import_once()
        tampered = Path(self.temp.name) / "tampered"
        shutil.copytree(self.shadow, tampered)
        rows = [json.loads(line) for line in (tampered / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        rows[0]["payload"]["tampered"] = True
        (tampered / "events.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        with self.assertRaises(ShadowError) as error:
            verify_shadow_events(tampered)
        self.assertEqual(error.exception.code, "EVENT_HASH_MISMATCH")
        deleted = Path(self.temp.name) / "deleted"
        shutil.copytree(self.shadow, deleted)
        rows = (deleted / "events.jsonl").read_text(encoding="utf-8").splitlines()
        (deleted / "events.jsonl").write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
        with self.assertRaises(ShadowError) as error:
            verify_shadow_events(deleted)
        self.assertEqual(error.exception.code, "EVENT_LEDGER_TRUNCATED")


if __name__ == "__main__":
    unittest.main()
