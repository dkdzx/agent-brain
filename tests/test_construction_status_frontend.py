import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent_brain"
    / "construction_status_frontend.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location(
    "construction_status_frontend", MODULE_PATH
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class ConstructionBundleStoreTests(unittest.TestCase):
    def make_bundle(self, root: Path) -> tuple[Path, dict]:
        status = root / "status.json"
        events = root / "events.jsonl"
        tasks = root / "task_bindings.json"
        observations = root / "controller_task_observations.jsonl"
        receipt = root / "receipt.json"
        status.write_text(
            json.dumps(
                {
                    "flow_graph": {
                        "node_count": 16,
                        "edge_count": 33,
                        "nodes": [{"node_id": f"S{i:02d}"} for i in range(16)],
                        "edges": [
                            {
                                "source": f"S{i % 16:02d}",
                                "target": f"S{(i + 1) % 16:02d}",
                            }
                            for i in range(33)
                        ],
                    },
                    "stages": [],
                    "summary": {},
                }
            ),
            encoding="utf-8",
        )
        events.write_text(
            json.dumps({"seq": 1, "event_type": "TEST"}) + "\n",
            encoding="utf-8",
        )
        tasks.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        observations.write_text(
            json.dumps({"task_id": "T1"}) + "\n", encoding="utf-8"
        )
        receipt_payload = {
            "verdict": "PASS",
            "checks": {"flow_graph_edge_count": 33},
            "artifact_hashes": {
                "status": digest(status),
                "events": digest(events),
                "task_bindings": digest(tasks),
                "controller_task_observations": digest(observations),
            },
        }
        receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
        pointer = root / "pointer.json"
        pointer.write_text(
            json.dumps(
                {
                    "dynamic_runtime": {
                        "status": str(status),
                        "events": str(events),
                        "task_bindings": str(tasks),
                        "controller_task_observations": str(observations),
                        "receipt": str(receipt),
                    }
                }
            ),
            encoding="utf-8",
        )
        return pointer, receipt_payload

    def test_verified_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            pointer, _ = self.make_bundle(Path(directory))
            payload = MODULE.ConstructionBundleStore(pointer).read()
            self.assertTrue(payload["verified"])
            self.assertEqual(len(payload["status"]["flow_graph"]["nodes"]), 16)
            self.assertEqual(payload["events"][0]["seq"], 1)
            self.assertIn("task_projection", payload)
            self.assertEqual(payload["task_projection"]["task_count"], 0)

    def test_hash_failure_keeps_last_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pointer, _ = self.make_bundle(root)
            store = MODULE.ConstructionBundleStore(pointer)
            first = store.read()
            self.assertTrue(first["verified"])
            (root / "status.json").write_text("{}", encoding="utf-8")
            second = store.read()
            self.assertFalse(second["verified"])
            self.assertTrue(second["stale_cache"])
            self.assertEqual(
                second["status"]["flow_graph"]["node_count"], 16
            )

    def test_first_read_failure_has_no_unverified_status(self):
        with tempfile.TemporaryDirectory() as directory:
            pointer = Path(directory) / "missing-pointer.json"
            payload = MODULE.ConstructionBundleStore(pointer).read()
            self.assertFalse(payload["ok"])
            self.assertIsNone(payload["status"])

    def test_edge_count_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pointer, _ = self.make_bundle(root)
            status = root / "status.json"
            payload = json.loads(status.read_text(encoding="utf-8"))
            payload["flow_graph"]["edge_count"] = 32
            payload["flow_graph"]["edges"] = payload["flow_graph"]["edges"][:32]
            status.write_text(json.dumps(payload), encoding="utf-8")
            receipt = root / "receipt.json"
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_payload["artifact_hashes"]["status"] = digest(status)
            receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
            result = MODULE.ConstructionBundleStore(pointer).read()
            self.assertFalse(result["ok"])
            self.assertIsNone(result["status"])
            self.assertIn("edge_count must be 33", result["error"])

    def test_html_contains_three_views_and_read_only_notice(self):
        html = MODULE.CONSTRUCTION_HTML
        self.assertIn("项目状态机", html)
        self.assertIn("施工列表", html)
        self.assertIn("项目变更流", html)
        self.assertIn("project-list", html)
        self.assertIn("project-header", html)
        self.assertIn("前端无状态写权限", html)
        self.assertIn("类型化侧状态", html)
        self.assertIn("typed_side_states", html)
        self.assertIn("Object.entries", html)
        self.assertIn("project_acceptance_state", html)
        self.assertIn("阶段内子门", html)
        self.assertIn("单一全项目百分比", html)
        self.assertIn("computeLayeredLayout", html)
        self.assertIn("computeGraphLayout", html)
        self.assertIn("elk.bundled.js", html)
        self.assertIn("elk.algorithm", html)
        self.assertIn("ORTHOGONAL", html)
        self.assertIn("elkSectionsToPath", html)
        self.assertIn("@dagrejs/dagre", html)
        self.assertIn("d3@7.9.0/dist/d3.min.js", html)
        self.assertIn("structure-select", html)
        for profile in (
            "elk-right", "elk-down", "dagre-right", "dagre-down",
            "tree-right", "tree-down", "radial", "radial-tree", "concentric",
            "circular", "force", "fruchterman", "forceatlas2",
            "mds", "random", "grid", "compact-box", "combo", "snake",
            "fishbone", "mindmap", "dendrogram", "indented", "manual",
        ):
            self.assertIn(profile, html)
        self.assertIn("webgl3d", html)
        self.assertIn("webglProgram", html)
        self.assertIn("webglM4Multiply", html)
        self.assertIn("getContext('webgl2'", html)
        self.assertIn("webglGraphData", html)
        self.assertIn("webglReadOnly", html)
        self.assertIn("addEventListener('wheel'", html)
        self.assertIn("webglFit", html)
        self.assertIn("webglCamera", html)
        self.assertNotIn("pseudo3d-viewport", html)
        self.assertNotIn("pseudo3d-orbit", html)
        self.assertIn("manual-undo", html)
        self.assertIn("manual-redo", html)
        self.assertIn("manual-arrange", html)
        self.assertIn("manual-snap-input", html)
        self.assertIn("manualSnapshot", html)
        self.assertIn("computeAlternativeLayout", html)
        self.assertIn("computeDagreLayout", html)
        self.assertIn("startNodeDrag", html)
        self.assertIn("construction-layout-draft", html)
        self.assertIn("barycenter", html)
        self.assertIn("DAG", html)
        self.assertIn("dagMode", html)
        self.assertIn("dagWarning", html)
        self.assertIn("Soft dependencies remain visible as optional side rails", html)
        self.assertIn("cycleDetected", html)
        self.assertIn("edge.critical", html)
        self.assertIn("arrow-blocking", html)
        self.assertIn("marker-end", html)
        self.assertIn("lane-guides", html)
        self.assertIn("status==='BLOCKED'", html)
        self.assertIn("/api/construction", html)
        self.assertIn("presentation-mode", html)
        self.assertIn("presentation-toggle", html)
        self.assertIn("scrollbar-width:none", html)
        self.assertIn("::-webkit-scrollbar", html)
        self.assertIn(".task-preview{display:none;font-size:9px", html)
        self.assertIn(".presentation-mode .task-preview{display:block;font-size:8px", html)
        self.assertIn("eventTypeLabel", html)
        self.assertIn("eventPayloadHtml", html)
        self.assertIn("requestFullscreen", html)
        self.assertIn("全屏展示", html)
        self.assertIn("language-select", html)
        self.assertIn("I18N", html)
        self.assertIn("Construction State Machine", html)
        self.assertIn("applyLocale", html)
        self.assertIn("task_projection", html)
        self.assertIn("tasksForNode", html)
        self.assertIn("taskDetails", html)
        self.assertIn("claimStatus", html)
        self.assertIn("masterStepOrder", html)

    def test_workgroup_route_injects_back_link(self):
        source = MODULE.ConstructionStatusHandler.do_GET.__code__
        self.assertTrue(
            any(
                isinstance(value, str) and "返回施工状态机" in value
                for value in source.co_consts
            )
        )

    def test_anonymous_demo_uses_formal_state_machine_contract(self):
        payload = MODULE.AnonymousConstructionBundleStore().read()
        graph = payload["status"]["flow_graph"]
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["demo_mode"])
        self.assertEqual(graph["node_count"], 16)
        self.assertEqual(len(graph["nodes"]), 16)
        self.assertEqual(graph["edge_count"], 33)
        self.assertEqual(len(graph["edges"]), 33)
        self.assertEqual(
            sum(edge["edge_type"] == "HARD_DEPENDENCY" for edge in graph["edges"]),
            23,
        )
        self.assertEqual(
            sum(edge["edge_type"] == "SOFT_DEPENDENCY" for edge in graph["edges"]),
            10,
        )
        self.assertEqual(
            [
                f'{edge["source"]}->{edge["target"]}'
                for edge in graph["edges"]
                if edge["edge_status"] == "BLOCKING"
            ],
            ["S02->S04"],
        )
        self.assertEqual(len(payload["events"]), 12)
        self.assertIn("三条分支并行", payload["status"]["scenario_summary"])
        self.assertEqual(
            payload["status"]["branch_groups"][0]["nodes"],
            ["S01", "S02", "S03"],
        )
        self.assertEqual(payload["status"]["summary"]["stale_private_task_count"], 1)
        self.assertEqual(payload["receipt"]["verdict"], "PASS")
        self.assertIn("task_projection", payload)
        self.assertEqual(payload["task_projection"]["task_count"], 27)
        self.assertEqual(payload["task_projection"]["master_step_count"], 0)
        self.assertTrue(payload["task_projection"]["claim_status_field_available"])
        self.assertTrue(
            all(
                node["task_attachments"]
                for node in payload["status"]["flow_graph"]["nodes"]
            )
        )
        self.assertEqual(
            payload["task_projection"]["tasks_by_stage"]["S00"][0][
                "claim_status"
            ],
            "IN_PROGRESS",
        )
        self.assertEqual(
            payload["task_projection"]["tasks_by_stage"]["S00"][-1][
                "claim_status"
            ],
            "UNCLAIMED",
        )
        self.assertEqual(
            payload["task_projection"]["tasks_by_stage"]["S00"][-1][
                "member_binding_state"
            ],
            "NO_MEMBER_BINDING",
        )
        self.assertNotIn("PRIVATE_PROJECT_NAME", json.dumps(payload, ensure_ascii=False))

    def test_task_projection_preserves_missing_claim_state(self):
        result = MODULE._build_task_projection(
            {"flow_graph": {"nodes": [{"node_id": "S00"}]}},
            {
                "tasks": [
                    {
                        "task_id": "T1",
                        "stage_id": "S00",
                        "display_title": "Observed task",
                        "member_id": "member-a",
                        "task_status_raw": "COMPLETED",
                        "controller_observation_is_latest": True,
                    }
                ]
            },
        )
        self.assertEqual(result["task_count"], 1)
        self.assertEqual(result["master_step_count"], 1)
        self.assertEqual(result["master_tasks"][0]["controller_step_index"], 1)
        self.assertFalse(result["claim_status_field_available"])
        self.assertEqual(
            result["tasks_by_stage"]["S00"][0]["member_binding_state"],
            "MEMBER_BOUND",
        )

    def test_demo_html_keeps_formal_navigation_and_anonymous_label(self):
        html = MODULE.render_construction_html(demo_mode=True)
        self.assertIn("动态施工状态机 · 匿名演示", html)
        self.assertIn("/api/construction?demo=1", html)
        self.assertIn("/workgroups?demo=1", html)
        self.assertNotIn("PRIVATE_PROJECT_NAME", html)


if __name__ == "__main__":
    unittest.main()
