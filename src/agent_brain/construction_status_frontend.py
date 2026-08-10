from __future__ import annotations

import argparse
import hashlib
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from .workgroup_status_frontend import (
        DEFAULT_CODEX_STATE_DB,
        DEFAULT_HOST,
        DEFAULT_PORT,
        DEFAULT_RUNTIME_ROOT,
        DEFAULT_TITLE_MAP,
        WorkgroupStatusHandler,
        render_html as render_workgroup_html,
    )
except ImportError:  # pragma: no cover - direct script execution
    from workgroup_status_frontend import (  # type: ignore[no-redef]
        DEFAULT_CODEX_STATE_DB,
        DEFAULT_HOST,
        DEFAULT_PORT,
        DEFAULT_RUNTIME_ROOT,
        DEFAULT_TITLE_MAP,
        WorkgroupStatusHandler,
        render_html as render_workgroup_html,
    )


# Public-safe default.  A real deployment must pass its own pointer explicitly;
# no private project path is embedded in the package.
DEFAULT_CONSTRUCTION_POINTER = Path("construction-status-pointer.json")
EXPECTED_FLOW_GRAPH_NODE_COUNT = 16


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _read_events(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    rows.sort(key=lambda item: int(item.get("seq") or 0), reverse=True)
    return rows


def _build_task_projection(
    status: dict[str, Any], task_payload: dict[str, Any]
) -> dict[str, Any]:
    """Build a read-only stage/task adapter without changing formal status.

    The dashboard status projection owns stage capability/activity state.  The
    task-bindings artifact owns task identity and observed task status.  This
    adapter joins them in memory so the UI can show atomic work under the
    correct stage while keeping both source artifacts and their authority
    boundaries intact.
    """

    raw_tasks = task_payload.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    stage_ids = {
        str(node.get("node_id"))
        for node in (status.get("flow_graph") or {}).get("nodes") or []
        if isinstance(node, dict) and node.get("node_id")
    }
    normalized: list[dict[str, Any]] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        task = dict(item)
        task["task_status_raw"] = (
            task.get("task_status_raw")
            or task.get("task_status")
            or task.get("status")
            or "NOT_PROVIDED"
        )
        task["effective_status_class"] = (
            task.get("effective_status_class")
            or task.get("task_status_class")
            or task["task_status_raw"]
        )
        task["claim_status_provided"] = bool(
            task.get("claim_status") or task.get("claim_state")
        )
        task["member_binding_state"] = (
            "MEMBER_BOUND" if task.get("member_id") else "NO_MEMBER_BINDING"
        )
        task["master_controller_step"] = bool(
            task.get("controller_observation_is_latest") is True
        )
        normalized.append(task)

    by_stage: dict[str, list[dict[str, Any]]] = {stage_id: [] for stage_id in stage_ids}
    unmatched: list[dict[str, Any]] = []
    for task in normalized:
        stage_id = str(task.get("stage_id") or "")
        if stage_id in by_stage:
            by_stage[stage_id].append(task)
        else:
            unmatched.append(task)

    master_tasks = [task for task in normalized if task["master_controller_step"]]
    # The formal source does not provide an explicit numeric step field.  Keep
    # the source ordering as a presentation-only controller observation order;
    # do not claim that this derived index is a project-control sequence.
    for index, task in enumerate(master_tasks, start=1):
        task["controller_step_index"] = index
        task["controller_step_count"] = len(master_tasks)
    return {
        "source": "task_bindings.json joined with status.json#/flow_graph/nodes",
        "authority": "read_only_frontend_adapter",
        "master_step_source": "controller_task_observation_is_latest",
        "master_step_count": len(master_tasks),
        "master_tasks": master_tasks,
        "tasks_by_stage": by_stage,
        "unmatched_tasks": unmatched,
        "claim_status_field_available": any(
            task["claim_status_provided"] for task in normalized
        ),
        "task_count": len(normalized),
    }


class ConstructionBundleStore:
    """Fail-closed reader that retains only the last verified projection."""

    def __init__(self, pointer_path: Path) -> None:
        self.pointer_path = pointer_path
        self._last_verified: dict[str, Any] | None = None
        self._last_error: str | None = None

    def _paths(self) -> dict[str, Path]:
        pointer = _read_json(self.pointer_path)
        dynamic = pointer["dynamic_runtime"]
        return {
            "status": Path(dynamic["status"]),
            "events": Path(dynamic["events"]),
            "task_bindings": Path(dynamic["task_bindings"]),
            "receipt": Path(dynamic["receipt"]),
            "controller_task_observations": Path(
                dynamic["controller_task_observations"]
            ),
        }

    def read(self) -> dict[str, Any]:
        try:
            paths = self._paths()
            receipt = _read_json(paths["receipt"])
            if receipt.get("verdict") != "PASS":
                raise ValueError("construction receipt verdict is not PASS")
            expected = receipt.get("artifact_hashes") or {}
            for name in (
                "status",
                "events",
                "task_bindings",
                "controller_task_observations",
            ):
                expected_hash = expected.get(name)
                path = paths[name]
                if not expected_hash:
                    if name == "controller_task_observations" and not path.exists():
                        continue
                    raise ValueError(f"missing receipt hash for {name}")
                if not path.exists():
                    raise FileNotFoundError(str(path))
                actual_hash = _sha256(path)
                if actual_hash != str(expected_hash).upper():
                    raise ValueError(
                        f"hash mismatch for {name}: "
                        f"{actual_hash} != {expected_hash}"
                    )
            status = _read_json(paths["status"])
            tasks = _read_json(paths["task_bindings"])
            events = _read_events(paths["events"])
            graph = status.get("flow_graph") or {}
            if graph.get("node_count") != EXPECTED_FLOW_GRAPH_NODE_COUNT:
                raise ValueError(
                    "flow_graph node_count must be "
                    f"{EXPECTED_FLOW_GRAPH_NODE_COUNT}"
                )
            if len(graph.get("nodes") or []) != EXPECTED_FLOW_GRAPH_NODE_COUNT:
                raise ValueError(
                    "flow_graph nodes must contain "
                    f"{EXPECTED_FLOW_GRAPH_NODE_COUNT} records"
                )
            receipt_checks = receipt.get("checks") or {}
            expected_edge_count = receipt_checks.get("flow_graph_edge_count")
            if not isinstance(expected_edge_count, int):
                raise ValueError(
                    "receipt checks must pin flow_graph_edge_count"
                )
            if graph.get("edge_count") != expected_edge_count:
                raise ValueError(
                    "flow_graph edge_count must be "
                    f"{expected_edge_count}"
                )
            if len(graph.get("edges") or []) != expected_edge_count:
                raise ValueError(
                    "flow_graph edges must contain "
                    f"{expected_edge_count} records"
                )
            payload = {
                "ok": True,
                "verified": True,
                "stale_cache": False,
                "error": None,
                "pointer_path": str(self.pointer_path),
                "status": status,
                "task_bindings": tasks,
                "task_projection": _build_task_projection(status, tasks),
                "events": events[:500],
                "receipt": receipt,
            }
            self._last_verified = payload
            self._last_error = None
            return payload
        except Exception as exc:  # fail closed and retain verified view
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._last_verified is not None:
                cached = dict(self._last_verified)
                cached.update(
                    {
                        "verified": False,
                        "stale_cache": True,
                        "error": self._last_error,
                    }
                )
                return cached
            return {
                "ok": False,
                "verified": False,
                "stale_cache": False,
                "error": self._last_error,
                "status": None,
                "task_bindings": None,
                "events": [],
                "receipt": None,
            }


DEMO_TASK_TITLES_EN = {
    "DEMO-STATE-001": "Anonymous demo | controller decomposition and dispatch",
    "DEMO-STATE-002": "Anonymous demo | identity and data foundation",
    "DEMO-STATE-003": "Anonymous demo | parallel dependency solving",
    "DEMO-STATE-004": "Anonymous demo | evidence collection and boundary review",
    "DEMO-STATE-005": "Anonymous demo | reproduce an acceptance-gate blocker",
    "DEMO-STATE-006": "Anonymous demo | runtime state projection",
    "DEMO-STATE-007": "Anonymous demo | shared-context slice",
    "DEMO-STATE-008": "Anonymous demo | parallel task-pool scheduling",
    "DEMO-STATE-009": "Anonymous demo | merge and accept parallel results",
    "DEMO-STATE-010": "Anonymous demo | final delivery review",
    "DEMO-STATE-011": "Anonymous demo | isolate stale private projection",
}


def _demo_task(
    task_id: str,
    display_title: str,
    member_id: str | None,
    task_status: str,
    stage_id: str,
    *,
    claim_status: str | None = None,
    display_title_en: str | None = None,
) -> dict[str, Any]:
    default_claim = {
        "ACTIVE": "IN_PROGRESS",
        "QUEUED": "CLAIMED",
        "PENDING_REVIEW": "CLAIMED",
        "STALE_PRIVATE_PROJECTION": "EXPIRED",
    }.get(task_status, "UNCLAIMED")
    return {
        "task_id": task_id,
        "display_title": display_title,
        "display_title_en": (
            display_title_en
            or DEMO_TASK_TITLES_EN.get(task_id)
            or f"Anonymous demo | {stage_id} atomic validation"
        ),
        "member_id": member_id,
        "role": "施工" if member_id != "demo-reviewer" else "审查",
        "thread_id": f"demo-thread-{task_id.lower()}",
        "group_id": "anonymous-construction-demo",
        "stage_id": stage_id,
        "task_status": task_status,
        "task_status_raw": task_status,
        "effective_status_class": task_status,
        "claim_status": claim_status or default_claim,
    }


def anonymous_construction_bundle() -> dict[str, Any]:
    """Return the formal construction-state shape with synthetic public data.

    The demo deliberately follows the same 16-node/33-edge contract as the
    real construction reader, but it is generated locally and contains no
    project pointer, real task title, thread id, or private path.
    """

    # The public fixture deliberately contains a branching/converging DAG.
    # It is still the same 16-node/33-edge contract as production, but the
    # story now demonstrates why a state machine is useful: independent
    # branches may run in parallel, a blocked gate propagates to a merge, and
    # an expired private projection remains isolated from the formal path.
    titles = [
        "任务拆解与派发",
        "数据与身份底座",
        "规则与依赖分支",
        "证据采集分支",
        "边界验收门",
        "运行时投影分支",
        "共享上下文分支",
        "分支结果汇合",
        "任务池与调度",
        "并行结果合并",
        "交接与回写",
        "观察与诊断",
        "回归与重建",
        "发布候选检查",
        "过期投影隔离",
        "最终验收摘要",
    ]
    titles_en = [
        "Task Decomposition & Dispatch",
        "Data & Identity Foundation",
        "Rules & Dependency Branch",
        "Evidence Collection Branch",
        "Boundary Acceptance Gate",
        "Runtime Projection Branch",
        "Shared Context Branch",
        "Branch Result Convergence",
        "Task Pool & Scheduling",
        "Parallel Result Merge",
        "Handoff & Writeback",
        "Observation & Diagnostics",
        "Regression & Reconstruction",
        "Release Candidate Review",
        "Stale Projection Isolation",
        "Final Acceptance Summary",
    ]
    statuses = [
        "ACTIVE",
        "ACTIVE",
        "ACTIVE",
        "COMPONENT_READY",
        "BLOCKED",
        "ACTIVE",
        "QUEUED",
        "READY",
        "ACTIVE",
        "PENDING_REVIEW",
        "READY",
        "DEFERRED",
        "ACCEPTED_NARROW",
        "READY",
        "STALE_PRIVATE_PROJECTION",
        "PENDING_REVIEW",
    ]
    tasks = [
        _demo_task(
            "DEMO-STATE-001",
            "匿名演示｜总控拆解与主线派发",
            "demo-controller",
            "ACTIVE",
            "S00",
        ),
        _demo_task(
            "DEMO-STATE-002",
            "匿名演示｜身份与数据底座",
            "demo-foundation",
            "ACTIVE",
            "S01",
        ),
        _demo_task(
            "DEMO-STATE-003",
            "匿名演示｜依赖规则并行求解",
            "demo-rules",
            "ACTIVE",
            "S02",
        ),
        _demo_task(
            "DEMO-STATE-004",
            "匿名演示｜证据采集与边界复核",
            "demo-reviewer",
            "PENDING_REVIEW",
            "S03",
        ),
        _demo_task(
            "DEMO-STATE-005",
            "匿名演示｜验收门阻塞复现",
            "demo-gatekeeper",
            "PENDING_REVIEW",
            "S04",
        ),
        _demo_task(
            "DEMO-STATE-006",
            "匿名演示｜运行时状态投影",
            "demo-runtime",
            "ACTIVE",
            "S05",
        ),
        _demo_task(
            "DEMO-STATE-007",
            "匿名演示｜共享上下文切片",
            "demo-context",
            "QUEUED",
            "S06",
        ),
        _demo_task(
            "DEMO-STATE-008",
            "匿名演示｜任务池并行调度",
            "demo-scheduler",
            "ACTIVE",
            "S08",
        ),
        _demo_task(
            "DEMO-STATE-009",
            "匿名演示｜并行结果合并验收",
            "demo-integrator",
            "PENDING_REVIEW",
            "S09",
        ),
        _demo_task(
            "DEMO-STATE-010",
            "匿名演示｜最终交付复核",
            "demo-acceptance",
            "PENDING_REVIEW",
            "S15",
        ),
        _demo_task(
            "DEMO-STATE-011",
            "匿名演示｜过期私有投影隔离",
            "demo-observer",
            "STALE_PRIVATE_PROJECTION",
            "S14",
        ),
    ]
    # Every large stage gets at least one small, explicitly unclaimed atomic
    # check so the public demo demonstrates the node -> task relationship even
    # where the primary story task is absent.  These are synthetic fixture
    # records only; they never enter the real project-control runtime.
    for index, title in enumerate(titles):
        stage_id = f"S{index:02d}"
        tasks.append(
            _demo_task(
                f"DEMO-ATOM-{stage_id}-01",
                f"匿名演示｜{title}原子校验",
                None,
                "READY",
                stage_id,
                claim_status="UNCLAIMED",
                display_title_en=(
                    f"Anonymous demo | {titles_en[index]} atomic validation"
                ),
            )
        )
    task_by_stage: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        task_by_stage.setdefault(str(task["stage_id"]), []).append(task)

    hard_pairs = [
        ("S00", "S01"),
        ("S00", "S02"),
        ("S00", "S03"),
        ("S01", "S04"),
        ("S02", "S04"),
        ("S02", "S05"),
        ("S03", "S05"),
        ("S01", "S06"),
        ("S03", "S06"),
        ("S04", "S07"),
        ("S05", "S07"),
        ("S05", "S08"),
        ("S06", "S08"),
        ("S07", "S09"),
        ("S08", "S09"),
        ("S09", "S10"),
        ("S09", "S11"),
        ("S10", "S12"),
        ("S11", "S12"),
        ("S12", "S13"),
        ("S12", "S14"),
        ("S13", "S15"),
        ("S14", "S15"),
    ]
    soft_pairs = [
        ("S00", "S04"),
        ("S01", "S05"),
        ("S02", "S06"),
        ("S03", "S07"),
        ("S04", "S08"),
        ("S05", "S09"),
        ("S06", "S09"),
        ("S07", "S10"),
        ("S08", "S11"),
        ("S10", "S15"),
    ]
    edges = [
        {
            "source": source,
            "target": target,
            "edge_type": "HARD_DEPENDENCY",
            "edge_status": (
                "BLOCKING" if source == "S02" and target == "S04" else "SATISFIED"
            ),
            "blocks_target": source == "S02" and target == "S04",
            "critical_path_highlight": target in {"S02", "S04", "S07", "S09", "S15"},
        }
        for source, target in hard_pairs
    ] + [
        {
            "source": source,
            "target": target,
            "edge_type": "SOFT_DEPENDENCY",
            "edge_status": "SATISFIED",
            "blocks_target": False,
            "critical_path_highlight": False,
        }
        for source, target in soft_pairs
    ]
    lanes = [
        "主线",
        "并行A",
        "并行B",
        "并行C",
        "汇合门",
        "并行D",
        "并行E",
        "汇合节点",
        "调度",
        "合并门",
        "交接",
        "观测",
        "重建",
        "发布",
        "旁路",
        "验收",
    ]
    lanes_en = [
        "Mainline", "Parallel A", "Parallel B", "Parallel C", "Merge Gate", "Parallel D",
        "Parallel E", "Convergence", "Scheduling", "Merge Gate", "Handoff", "Observation",
        "Reconstruction", "Release", "Side Route", "Acceptance",
    ]
    layers = ["L0", "L1", "L1", "L1", "L2", "L2", "L2", "L3", "L3", "L4", "L5", "L5", "L6", "L7", "L7", "L8"]
    capability_states = [
        "READY", "READY", "READY", "COMPONENT_READY", "READY", "READY", "READY", "READY",
        "COMPONENT_READY", "COMPONENT_READY", "READY", "READY", "COMPONENT_READY", "READY", "READY", "COMPONENT_READY",
    ]
    activity_states = [
        "ACTIVE", "ACTIVE", "ACTIVE", "IDLE", "ACTIVE", "ACTIVE", "ACTIVE", "IDLE",
        "ACTIVE", "ACTIVE", "IDLE", "IDLE", "IDLE", "IDLE", "IDLE", "ACTIVE",
    ]
    effect_states = [
        "PARTIAL", "NOT_MEASURED", "PARTIAL", "COMPONENT_ONLY", "NOT_MEASURED", "PARTIAL", "NOT_MEASURED", "NOT_MEASURED",
        "PARTIAL", "PARTIAL", "NOT_MEASURED", "NOT_MEASURED", "COMPONENT_ONLY", "NOT_MEASURED", "NOT_MEASURED", "NOT_MEASURED",
    ]
    acceptance_states = [
        "PENDING", "PENDING", "PENDING", "PENDING", "BLOCKED", "PENDING", "PENDING", "PENDING",
        "PENDING", "PENDING_REVIEW", "PENDING", "DEFERRED", "ACCEPTED_NARROW", "PENDING", "DEFERRED", "PENDING_REVIEW",
    ]
    completion = [18, 42, 58, 66, 35, 54, 28, 22, 48, 62, 44, 38, 72, 28, 16, 24]
    layout_columns = [0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5]
    layout_rows = [1, 0, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2]
    layout_column_titles = [
        {"title": "任务入口", "subtitle": "单一派发", "title_en": "Task Entry", "subtitle_en": "Single dispatch", "node_ids": ["S00"]},
        {"title": "输入分支", "subtitle": "三路并行", "title_en": "Input Branches", "subtitle_en": "Three-way parallel", "node_ids": ["S01", "S02", "S03"]},
        {"title": "施工分支", "subtitle": "阻塞隔离", "title_en": "Work Branches", "subtitle_en": "Blocker isolation", "node_ids": ["S04", "S05", "S06"]},
        {"title": "汇合与调度", "subtitle": "依赖收敛", "title_en": "Convergence & Scheduling", "subtitle_en": "Dependency convergence", "node_ids": ["S07", "S08", "S09"]},
        {"title": "交付与观测", "subtitle": "双轨验收", "title_en": "Delivery & Observation", "subtitle_en": "Dual-track acceptance", "node_ids": ["S10", "S11", "S12"]},
        {"title": "发布与旁路", "subtitle": "正式 / 只读", "title_en": "Release & Side Routes", "subtitle_en": "Formal / read-only", "node_ids": ["S13", "S14", "S15"]},
    ]
    nodes: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    for index, title in enumerate(titles):
        stage_id = f"S{index:02d}"
        stage_tasks = task_by_stage.get(stage_id, [])
        active_tasks = [t for t in stage_tasks if t["task_status"] == "ACTIVE"]
        queued_tasks = [t for t in stage_tasks if t["task_status"] == "QUEUED"]
        pending_tasks = [
            t for t in stage_tasks if t["task_status"] == "PENDING_REVIEW"
        ]
        stale_tasks = [
            t
            for t in stage_tasks
            if t["task_status"] == "STALE_PRIVATE_PROJECTION"
        ]
        blockers = (
            [
                {
                    "blocker_id": "DEMO-BLOCK-BOUNDARY",
                    "title": "证据分支尚未达到边界验收门",
                    "status": "OPEN",
                    "blocks": ["S04", "S07", "S09"],
                }
            ]
            if stage_id == "S04"
            else []
        )
        node = {
            "node_id": stage_id,
            "lane": lanes[index],
            "lane_en": lanes_en[index],
            "layer": layers[index],
            "layout_column": layout_columns[index],
            "layout_row": layout_rows[index],
            "title": title,
            "title_en": titles_en[index],
            "status": statuses[index],
            "capability_state": capability_states[index],
            "activity_state": activity_states[index],
            "effect_state": effect_states[index],
            "project_acceptance_state": acceptance_states[index],
            "gate_state": "OPEN" if not blockers else "BLOCKED",
            "completion_percent": completion[index],
            "task_counts": {
                "active": len(active_tasks),
                "queued": len(queued_tasks),
                "pending_review": len(pending_tasks),
            },
            "blocker_count": len(blockers),
            "blockers": blockers,
            "subgates": [
                {
                    "subgate_id": f"{stage_id}-GATE-01",
                    "status": "OPEN" if not blockers else "BLOCKED",
                }
            ],
            "capability_tracks": [
                {"track_id": f"{stage_id}-TRACK-A", "status": "READ_ONLY"}
            ],
            "task_attachments": stage_tasks,
        }
        nodes.append(node)
        hard_dependencies = [source for source, target in hard_pairs if target == stage_id]
        soft_dependencies = [source for source, target in soft_pairs if target == stage_id]
        stages.append(
            {
                "stage_id": stage_id,
                "title": title,
                "title_en": titles_en[index],
                "layer": node["layer"],
                "status": statuses[index],
                "completion_percent": node["completion_percent"],
                "target": f"完成{title}并留下可复核交接",
                "target_en": f"Complete {titles_en[index]} and leave a reviewable handoff",
                "hard_dependencies": hard_dependencies,
                "soft_dependencies": soft_dependencies,
                "blockers": blockers,
                "blockers_en": (
                    [
                        {
                            "blocker_id": "DEMO-BLOCK-BOUNDARY",
                            "title": "Evidence branch has not reached the boundary acceptance gate",
                            "status": "OPEN",
                            "blocks": ["S04", "S07", "S09"],
                        }
                    ]
                    if stage_id == "S04"
                    else []
                ),
                "active_tasks": active_tasks,
                "queued_tasks": queued_tasks,
                "pending_review_tasks": pending_tasks,
                "stale_private_tasks": stale_tasks,
                "adoption_mode": "匿名演示只读投影",
                "adoption_mode_en": "Anonymous demo read-only projection",
                "mature_wheel_candidates": ["本地append-only事件"],
                "mature_wheel_candidates_en": ["Local append-only events"],
                "legal_next_task": (
                    "补齐证据分支后关闭阻塞，恢复原施工态"
                    if stage_id == "S04"
                    else (
                        "不得重新claim；先重建过期投影"
                        if stage_id == "S14"
                        else "读取最新状态并按门推进"
                    )
                ),
                "legal_next_task_en": (
                    "Complete the evidence branch, close the blocker, and return to the prior work state"
                    if stage_id == "S04"
                    else (
                        "Do not reclaim; rebuild the stale projection first"
                        if stage_id == "S14"
                        else "Read the latest state and advance through the gate"
                    )
                ),
                "direct_consumer": "状态机演示面板",
                "direct_consumer_en": "State-machine presentation panel",
                "output_handoff": f"demo://handoff/{stage_id.lower()}",
                "claim_ceiling": "仅证明匿名演示投影，不证明真实项目效果",
                "claim_ceiling_en": "Proves only the anonymous demo projection, not real project outcomes",
            }
        )

    fresh_tasks = [
        task
        for task in tasks
        if task["task_status"] in {"ACTIVE", "QUEUED", "PENDING_REVIEW"}
    ]
    events = [
        {
            "seq": 1,
            "stage_id": "S00",
            "occurred_at": "2026-08-08T12:00:00+08:00",
            "event_type": "DEMO_INITIALIZED",
            "current": {
                "status": "ACTIVE",
                "source": "anonymous fixture",
                "parallel_branches": ["S01", "S02", "S03"],
            },
        },
        {
            "seq": 2,
            "stage_id": "S01",
            "occurred_at": "2026-08-08T12:00:30+08:00",
            "event_type": "PARALLEL_BRANCH_STARTED",
            "current": {
                "branch": "data-and-identity",
                "task_id": "DEMO-STATE-002",
                "status": "ACTIVE",
            },
        },
        {
            "seq": 3,
            "stage_id": "S02",
            "occurred_at": "2026-08-08T12:00:45+08:00",
            "event_type": "PARALLEL_BRANCH_STARTED",
            "current": {
                "branch": "rules-and-dependencies",
                "task_id": "DEMO-STATE-003",
                "status": "ACTIVE",
            },
        },
        {
            "seq": 4,
            "stage_id": "S03",
            "occurred_at": "2026-08-08T12:01:00+08:00",
            "event_type": "EVIDENCE_ATTACHED",
            "current": {
                "task_id": "DEMO-STATE-004",
                "status": "PENDING_REVIEW",
                "evidence_count": 3,
            },
        },
        {
            "seq": 5,
            "stage_id": "S06",
            "occurred_at": "2026-08-08T12:01:15+08:00",
            "event_type": "TASK_QUEUED",
            "current": {
                "task_id": "DEMO-STATE-007",
                "status": "QUEUED",
                "waits_for": ["S01", "S03"],
            },
        },
        {
            "seq": 6,
            "stage_id": "S04",
            "occurred_at": "2026-08-08T12:02:00+08:00",
            "event_type": "BLOCKER_OPENED",
            "current": {
                "blocker_id": "DEMO-BLOCK-BOUNDARY",
                "status": "BLOCKED",
                "blocks": ["S04", "S07", "S09"],
                "reason": "证据分支尚未达到边界验收门",
            },
        },
        {
            "seq": 7,
            "stage_id": "S05",
            "occurred_at": "2026-08-08T12:02:15+08:00",
            "event_type": "PARALLEL_BRANCH_CONTINUED",
            "current": {
                "task_id": "DEMO-STATE-006",
                "status": "ACTIVE",
                "note": "不依赖S04的运行时分支继续施工",
            },
        },
        {
            "seq": 8,
            "stage_id": "S08",
            "occurred_at": "2026-08-08T12:02:30+08:00",
            "event_type": "TASK_ACTIVE",
            "current": {
                "task_id": "DEMO-STATE-008",
                "status": "ACTIVE",
                "parallel_with": ["S05", "S06"],
            },
        },
        {
            "seq": 9,
            "stage_id": "S07",
            "occurred_at": "2026-08-08T12:02:45+08:00",
            "event_type": "MERGE_WAITING",
            "current": {
                "status": "READY",
                "waits_for": ["S04", "S05"],
                "reason": "阻塞未解除，禁止越过汇合节点",
            },
        },
        {
            "seq": 10,
            "stage_id": "S09",
            "occurred_at": "2026-08-08T12:03:00+08:00",
            "event_type": "ACCEPTANCE_WAITING",
            "current": {
                "task_id": "DEMO-STATE-009",
                "status": "PENDING_REVIEW",
                "requires": ["S07", "S08"],
            },
        },
        {
            "seq": 11,
            "stage_id": "S14",
            "occurred_at": "2026-08-08T12:03:15+08:00",
            "event_type": "PRIVATE_PROJECTION_EXPIRED",
            "current": {
                "task_id": "DEMO-STATE-011",
                "status": "STALE_PRIVATE_PROJECTION",
                "formal_activity_counted": False,
            },
        },
        {
            "seq": 12,
            "stage_id": "S15",
            "occurred_at": "2026-08-08T12:04:00+08:00",
            "event_type": "ACCEPTANCE_BARRIER_WAITING",
            "current": {
                "task_id": "DEMO-STATE-010",
                "status": "PENDING_REVIEW",
                "requires": ["S13", "S14"],
                "note": "S14只能以隔离旁路形式被观察",
            },
        },
    ]
    return {
        "ok": True,
        "verified": True,
        "stale_cache": False,
        "error": None,
        "demo_mode": True,
        "pointer_path": "anonymous-demo://construction-status-pointer",
        "status": {
            "generated_at": "2026-08-08T12:04:00+08:00",
            "project": {
                "project_id": "anonymous-construction-demo",
                "title": "匿名项目 · 施工状态机演示",
                "title_en": "Anonymous Project · Construction State Machine Demo",
                "state": "ACTIVE",
                "updated_at": "2026-08-08T12:04:00+08:00",
                "source": "anonymous fixture",
                "source_en": "Anonymous fixture",
            },
            "summary": {
                "stage_count": 16,
                "active_stage_count": len({task["stage_id"] for task in fresh_tasks}),
                "fresh_active_task_count": sum(task["task_status"] == "ACTIVE" for task in tasks),
                "fresh_queued_task_count": sum(task["task_status"] == "QUEUED" for task in tasks),
                "pending_project_review_task_count": sum(task["task_status"] == "PENDING_REVIEW" for task in tasks),
                "blocked_stage_count": 1,
                "stale_private_task_count": sum(task["task_status"] == "STALE_PRIVATE_PROJECTION" for task in tasks),
                "critical_path_stage_id": "S04",
            },
            "scenario_summary": "S01/S02/S03三条分支并行；S04阻塞会卡住S07与S09的汇合，但不阻止S05/S06/S08继续施工；S14过期私有投影只走隔离旁路，不能占用正式活动位。",
            "scenario_summary_en": "S01/S02/S03 run in parallel; the S04 blocker holds the S07/S09 convergence without stopping S05/S06/S08; the stale S14 private projection stays on an isolated side route and cannot occupy a formal active slot.",
            "branch_groups": [
                {"name": "输入分支", "nodes": ["S01", "S02", "S03"]},
                {"name": "并行施工", "nodes": ["S05", "S06", "S08"]},
                {"name": "汇合验收", "nodes": ["S07", "S09", "S15"]},
                {"name": "只读旁路", "nodes": ["S14"]},
            ],
            "workflow_state_machine": {
                "ordinary_transitions": [
                    {"from": "NOT_STARTED", "to": "READY"},
                    {"from": "READY", "to": "QUEUED"},
                    {"from": "QUEUED", "to": "ACTIVE"},
                    {"from": "ACTIVE", "to": "ACCEPTANCE"},
                    {"from": "ACCEPTANCE", "to": "PASS"},
                ],
                "blocking_transitions": {
                    "event": "BLOCKER_OPENED",
                    "return_event": "BLOCKER_CLOSED",
                },
                "typed_side_states": {
                    "REFERENCE_ONLY": "仅供研究参考",
                    "ACCEPTED_NARROW": "窄范围接纳，不外推完成",
                    "COMPONENT_READY": "组件可用，集成门未关闭",
                    "DEFERRED": "合法后置，不等于失败",
                },
                "typed_side_states_en": {
                    "REFERENCE_ONLY": "Research reference only",
                    "ACCEPTED_NARROW": "Accepted narrowly; no completion extrapolation",
                    "COMPONENT_READY": "Component ready; integration gate remains open",
                    "DEFERRED": "Legally deferred; not a failure",
                },
                "frontend_write_transition_allowed": False,
            },
            "orthogonal_state_model": {
                "capability": "能力",
                "activity": "活动",
                "effect": "效果",
                "project_acceptance": "项目验收",
            },
            "typed_subgates": ["evidence", "acceptance", "handoff"],
            "capability_tracks": ["component", "integration", "observation"],
            "flow_graph": {
                "node_count": 16,
                "edge_count": 33,
                "layout_columns": [
                    {"column": index, **column}
                    for index, column in enumerate(layout_column_titles)
                ],
                "nodes": nodes,
                "edges": edges,
                "critical_path_highlight": ["S00", "S02", "S04", "S07", "S09", "S15"],
            },
            "stages": stages,
        },
        "task_bindings": {
            "source": "anonymous construction fixture",
            "tasks": tasks,
        },
        "task_projection": _build_task_projection(
            {"flow_graph": {"nodes": nodes}}, {"tasks": tasks}
        ),
        "events": events,
        "receipt": {
            "verdict": "PASS",
            "fixture": "anonymous-construction-demo",
            "checks": {"flow_graph_node_count": 16, "flow_graph_edge_count": 33},
        },
    }


class AnonymousConstructionBundleStore:
    """Read-only synthetic store used only when ``?demo=1`` is requested."""

    def read(self) -> dict[str, Any]:
        return anonymous_construction_bundle()


CONSTRUCTION_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>项目施工状态机</title>
  <style>
    :root{
      color-scheme:dark;--bg:#071421;--panel:#0d2131;--panel2:#102a3d;
      --line:#28495f;--line-soft:#19374a;--text:#edf6f5;--muted:#89a9b7;--cyan:#63dfca;
      --gold:#f1c56b;--red:#ff756f;--blue:#70a7ff;--green:#67d99a;
      --node-w:238px;--node-h:168px
    }
    *{box-sizing:border-box}body{margin:0;overflow-x:hidden;background:linear-gradient(145deg,#07131e,#0a1e2c);
      color:var(--text);font-family:"Segoe UI","Microsoft YaHei",sans-serif}
    button,select,input{font:inherit}.topbar{height:72px;display:flex;align-items:center;
      justify-content:space-between;padding:0 24px;border-bottom:1px solid var(--line);
      background:#081927;position:sticky;top:0;z-index:20}
    h1{font-size:22px;margin:0}.sub{font-size:12px;color:var(--muted);margin-top:5px}
    .health{display:flex;gap:8px;align-items:center;font-size:13px}.pill{padding:6px 10px;
      border:1px solid var(--line);border-radius:999px;background:#0b2231}
    .pill.ok{color:var(--green);border-color:#276b58}.pill.bad{color:var(--red);border-color:#7a3738}
    .tabs{display:flex;gap:8px;padding:12px 20px;border-bottom:1px solid var(--line);
      background:#0a1a28;position:sticky;top:72px;z-index:19}
    .tab,.tool{border:1px solid var(--line);background:#0d2638;color:var(--text);
      padding:8px 13px;border-radius:8px;cursor:pointer}.tab.active{background:#145064;border-color:var(--cyan)}
    .workspace{display:grid;grid-template-columns:276px minmax(0,1fr);min-height:calc(100vh - 72px)}
    .project-column{border-right:1px solid var(--line);background:#081a29;padding:16px 14px;position:sticky;top:72px;height:calc(100vh - 72px);overflow:auto}
    .project-heading{font-size:18px;font-weight:800;margin-bottom:4px}.project-caption{font-size:11px;color:var(--muted);line-height:1.5;margin-bottom:14px}
    .project-card{display:block;width:100%;text-align:left;border:1px solid var(--line);border-radius:12px;background:#0d2638;color:var(--text);padding:12px;margin:8px 0;cursor:default}
    .project-card.active{border-color:var(--blue);box-shadow:0 0 0 2px #70a7ff33;background:#122e49}.project-card .project-state{float:right;font-size:10px;color:var(--green);border:1px solid #276b58;border-radius:999px;padding:3px 6px}
    .project-card .project-title{font-weight:750;font-size:14px;margin-bottom:6px}.project-card .project-meta{color:var(--muted);font-size:11px;line-height:1.45}
    .project-panel{min-width:0;width:100%;overflow:hidden}.project-header{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:16px 20px 8px;border-bottom:1px solid var(--line-soft);background:#091c2c}
    .project-header h2{margin:0;font-size:20px}.project-header p{margin:5px 0 0;color:var(--muted);font-size:12px}.project-header .project-proof{font-size:11px;color:var(--muted);text-align:right;white-space:nowrap}
    .summary{display:grid;grid-template-columns:repeat(8,minmax(120px,1fr));gap:10px;padding:14px 20px}
    .metric{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
    .metric b{display:block;font-size:25px;color:#fff}.metric span{font-size:12px;color:var(--muted)}
    .view{display:none}.view.active{display:block}.toolbar{display:flex;gap:8px;align-items:center;
      padding:0 20px 10px;flex-wrap:wrap}.toolbar .spacer{flex:1}
    .graph-shell{width:calc(100% - 40px);margin:0 20px 20px;border:1px solid var(--line);border-radius:14px;
      background:#071723;display:grid;grid-template-columns:minmax(0,1fr) 320px;height:720px;overflow:hidden}
    .viewport{min-width:0;position:relative;overflow:auto;cursor:grab;background-color:#071723;
      background-image:radial-gradient(#1a415244 1px,transparent 1px);background-size:22px 22px}.viewport.dragging{cursor:grabbing}
    /* Keep pan/zoom behavior without competing visible scrollbars. */
    .viewport,.inspector,.project-column,.table-wrap,.events-wrap{scrollbar-width:none;overscroll-behavior:contain}
    .viewport::-webkit-scrollbar,.inspector::-webkit-scrollbar,.project-column::-webkit-scrollbar,
    .table-wrap::-webkit-scrollbar,.events-wrap::-webkit-scrollbar{width:0;height:0;display:none}
    .canvas{position:relative;transform-origin:0 0;min-width:100%;min-height:100%}
    .edges{position:absolute;inset:0;overflow:visible;pointer-events:none;z-index:1}.edge{fill:none;stroke:#4c8d83;stroke-width:2.5}
    .edge.soft{stroke-dasharray:8 8;stroke:#66808e;stroke-opacity:.2;stroke-width:1.8}
    .edge.soft.satisfied{stroke:#4b8d81;stroke-opacity:.2}.edge.blocking{stroke:var(--red);stroke-width:3.5;stroke-opacity:1}
    #lane-guides{position:absolute;inset:0;z-index:0;pointer-events:none}.lane-guide{position:absolute;top:0;
      border-left:1px solid #2a596a55;border-right:1px solid #1b435722;border-radius:0;
      background:linear-gradient(180deg,#0c2a3b22,#07172308 72%);box-shadow:inset 0 1px #ffffff08}
    .lane-guide strong{display:block;padding:12px 10px 2px;color:#b8d9df;font-size:11px;letter-spacing:.04em}
    .lane-guide span{display:block;padding:0 10px;color:#628596;font-size:10px}
    #nodes{position:absolute;inset:0;z-index:2}
    .edge.satisfied{stroke:#3b8c78}.edge.critical{stroke:var(--gold);stroke-width:3}.edge.blocking.critical{stroke:var(--red)}
    .node{position:absolute;width:var(--node-w);height:var(--node-h);
      border:2px solid var(--line);border-radius:13px;background:linear-gradient(150deg,#102b3d,#0b202f);
      padding:13px;box-shadow:0 10px 28px #0008;cursor:pointer;overflow:hidden;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}
    .node:hover{transform:translateY(-3px);box-shadow:0 14px 30px #000b}
    .node.critical{box-shadow:0 0 0 2px #d3a94d55,0 10px 28px #0008}.node.active{border-color:var(--cyan)}
    .node.blocked{border-color:var(--red)}.node.stale{background:repeating-linear-gradient(135deg,#172431,#172431 10px,#111e29 10px,#111e29 20px)}
    .node-head{display:flex;gap:8px;align-items:start;justify-content:space-between}.node-id{font:12px ui-monospace;color:var(--gold)}
    .node-title{font-weight:750;font-size:14px;margin:4px 0}.badge{padding:3px 7px;border-radius:999px;font-size:10px;
      border:1px solid var(--line);white-space:nowrap}.badge.ACTIVE{color:var(--cyan)}.badge.BLOCKED{color:var(--red)}
    .progress{height:7px;background:#06121b;border-radius:9px;overflow:hidden;margin:8px 0}
    .progress i{height:100%;display:block;background:linear-gradient(90deg,var(--blue),var(--cyan))}
    .counts{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;font-size:10px;color:var(--muted)}
    .counts b{display:block;color:var(--text);font-size:13px}.task-chip{font-size:10px;background:#0b3140;border-left:3px solid var(--cyan);
      margin-top:7px;padding:5px 7px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .task-summary{font-size:10px;color:#b7d1d7;margin-top:6px;padding:4px 6px;border:1px solid #284f63;border-radius:5px;background:#0a2535;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.task-preview{display:none;font-size:9px;background:#0b3140;border-left:3px solid var(--cyan);margin-top:4px;padding:4px 6px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.task-preview.terminal{border-left-color:#9bb0b8;color:#b8c3c8;background:#172a34}.task-preview.stale{border-left-color:#8c969b;color:#a9b4ba;background:#16242d}
    .axes{display:grid;grid-template-columns:1fr 1fr;gap:4px 7px;font-size:10px;color:var(--muted);line-height:1.35}
    .axes b{color:#d9ecec;font-weight:650}.subgate{font-size:11px;padding:5px 7px;margin:5px 0;border:1px solid #325269;border-radius:6px;background:#0a2232}
    .node-subline{margin-top:5px;color:#7898a7;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .task-chip.stale{border-color:#7c8790;color:#a9b4ba;background:#16242d}
    .inspector{min-width:0;border-left:1px solid var(--line);background:linear-gradient(180deg,#0b1f2e,#091a28);padding:14px;overflow:auto}
    .inspector h2{margin:0 0 5px;font-size:17px}.inspector h3{font-size:12px;color:var(--gold);margin:15px 0 6px}
    .kv{font-size:12px;color:var(--muted);line-height:1.6}.kv b{color:var(--text)}
    .task-card{border:1px solid var(--line);border-radius:9px;padding:9px;margin:7px 0;background:#0d2738}
    .task-card .title{font-weight:700;font-size:13px}.task-card code{display:block;color:var(--muted);font-size:10px;word-break:break-all;margin-top:5px}
    .machine{margin:0 20px 20px;padding:12px 14px;background:var(--panel);border:1px solid var(--line);border-radius:12px;
      font-size:13px;color:var(--muted)}.machine b{color:var(--text)}.arrow{color:var(--gold);padding:0 5px}
    .table-wrap,.events-wrap{margin:0 20px 24px;border:1px solid var(--line);border-radius:13px;overflow:auto;background:var(--panel)}
    table{border-collapse:collapse;width:100%;min-width:1400px;font-size:12px}th,td{padding:10px;border-bottom:1px solid #1b3a4e;text-align:left;vertical-align:top}
    th{position:sticky;top:0;background:#102b3d;z-index:2;color:#b9d5dc}.row-detail{max-width:380px;color:var(--muted)}
    .event{padding:12px 14px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:75px 200px 1fr;gap:10px}.event-pair{display:inline-block;margin:0 12px 3px 0}.event-pair b{color:#d5e9e9}
    .event code{font-size:11px;color:var(--gold)}.warning{margin:0 20px 12px;padding:10px;border:1px solid #84403e;
      background:#3a2022;color:#ffc1bd;border-radius:9px;display:none}.warning.show{display:block}
    .empty{padding:30px;color:var(--muted);text-align:center}.legend{font-size:11px;color:var(--muted)}
    .edge-toggle{display:inline-flex;align-items:center;gap:5px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:#0b2232;color:#b7d1d7;font-size:11px;white-space:nowrap}.edge-toggle input{accent-color:var(--cyan)}
    .graph-mode{display:inline-flex;align-items:center;padding:7px 10px;border:1px solid #3d6476;border-radius:8px;background:#102d3d;color:#d5edf0;font-size:11px;font-weight:700;white-space:nowrap}.graph-mode.warning{border-color:var(--red);background:#3a2022;color:#ffc1bd}
    .layout-control{display:inline-flex;align-items:center;gap:5px;padding:4px 7px;border:1px solid var(--line);border-radius:8px;background:#0b2232;color:#b7d1d7;font-size:11px;white-space:nowrap}.layout-control select{border:0;background:transparent;color:var(--text);font-size:11px;outline:0;cursor:pointer}.layout-control select option{background:#0b2232;color:var(--text)}
    .manual-tools{display:flex;align-items:center;gap:7px;min-height:0;padding:0 20px 10px;flex-wrap:wrap}.manual-action{display:none}.editing-view .manual-action{display:inline-flex}.manual-snap{align-items:center;gap:4px;padding:6px 9px;border:1px solid var(--line);border-radius:8px;background:#0b2232;color:#b7d1d7;font-size:11px;white-space:nowrap}.manual-snap input{accent-color:var(--cyan)}.manual-help{display:none;color:#b9d5da;font-size:11px;padding:6px 9px;border-left:2px solid var(--cyan);background:#0c2938}.editing-view .manual-help{display:inline-flex}.manual-action:disabled{opacity:.4;cursor:not-allowed}
    body.editing-view .node{cursor:grab;border-style:dashed}.node.dragging{cursor:grabbing!important;z-index:5;transform:translateY(-3px) scale(1.02);box-shadow:0 16px 34px #000d}.view-draft-note{display:none;color:#f1c56b;font-size:10px;white-space:nowrap}.editing-view .view-draft-note{display:inline}
    .webgl3d{display:none;position:absolute;inset:0;z-index:6;overflow:hidden;cursor:grab;background:radial-gradient(circle at 50% 34%,#123c52 0,#071b2a 44%,#04111b 100%)}.webgl3d.active{display:block}.webgl3d:active{cursor:grabbing}.webgl3d canvas{display:block;width:100%;height:100%}.webgl3d-status{position:absolute;top:12px;left:12px;z-index:10;max-width:min(520px,calc(100% - 24px));padding:7px 10px;border:1px solid #3c7182;border-radius:8px;background:#061722dd;color:#d9f4f2;font-size:11px;pointer-events:none;box-shadow:0 6px 20px #0008}.webgl3d-status:empty{display:none}.webgl3d-overlay{position:absolute;inset:0;z-index:7;width:100%;height:100%;pointer-events:none;overflow:visible;mix-blend-mode:screen}.webgl3d-overlay line{fill:none;stroke:#55d7c3;stroke-width:3;stroke-opacity:.82}.webgl3d-overlay line.soft{stroke:#9aabb8;stroke-dasharray:6 7;stroke-opacity:.58}.webgl3d-overlay line.blocking{stroke:#ff756f;stroke-width:4;stroke-opacity:1}.webgl3d-overlay line.critical{stroke:#f1c56b;stroke-width:3.6;stroke-opacity:1}.webgl3d-labels{position:absolute;inset:0;z-index:8;pointer-events:none}.webgl3d-label{position:absolute;min-width:112px;max-width:170px;padding:6px 8px;transform:translate(-50%,-50%);border:1px solid #4e8291;border-radius:8px;background:#092334ee;color:#dff4f2;box-shadow:0 7px 18px #0009;text-align:left;line-height:1.25;transition:opacity .12s ease,border-color .12s ease,box-shadow .12s ease}.webgl3d-label b{display:block;color:#f1c56b;font:10px ui-monospace}.webgl3d-label strong{display:block;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.webgl3d-label span{display:block;color:#9fc1c9;font-size:9px;margin-top:2px}.webgl3d-label.active{border-color:#55e6cf}.webgl3d-label.blocked{border-color:#ff756f}.webgl3d-label.selected{box-shadow:0 0 0 2px #f1c56b,0 8px 20px #000b;z-index:2000}
    .screen-button{border:1px solid #3d7e7b;border-radius:9px;background:#103b43;color:#dffbf3;padding:8px 12px;cursor:pointer;font-weight:700;white-space:nowrap}.screen-button:hover{background:#15545b}.screen-button:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
    .language-switch{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:11px;white-space:nowrap}.language-switch select{border:1px solid var(--line);border-radius:8px;background:#0b2231;color:var(--text);padding:6px 8px;cursor:pointer}.language-switch select:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
    .presentation-mode{--node-w:210px;--node-h:150px;background:#06121d}
    .presentation-mode .topbar{height:66px;padding:0 28px;background:#061522;border-bottom-color:#203d4d}
    .presentation-mode .topbar h1{font-size:20px}.presentation-mode .topbar .sub{font-size:11px}
    .presentation-mode .workspace{display:block;min-height:calc(100vh - 66px)}
    .presentation-mode .project-column,.presentation-mode .project-header,.presentation-mode .tabs,.presentation-mode .machine{display:none}
    .presentation-mode .project-panel{width:100%;overflow:hidden}
    .presentation-mode #summary{display:flex;gap:18px;align-items:center;padding:8px 28px;border-bottom:1px solid #203d4d;background:#071a29;overflow:hidden}
    .presentation-mode #summary .metric{display:flex;align-items:baseline;gap:5px;padding:0;border:0;background:transparent;white-space:nowrap}
    .presentation-mode #summary .metric b{font-size:15px;color:#d9f0ef}.presentation-mode #summary .metric span{font-size:10px;color:#84a8b4}
    .presentation-mode .toolbar{padding:9px 28px 8px;background:#071a29;border-bottom:1px solid #203d4d}.presentation-mode .toolbar .spacer{display:none}.presentation-mode #status-filter{display:none}
    .presentation-mode .graph-shell{width:100%;height:calc(100vh - 150px);margin:0;border:0;border-radius:0;display:block;background:#06121d}
    .presentation-mode .viewport{height:100%;background-image:radial-gradient(#1b405044 1px,transparent 1px);background-size:20px 20px}
    .presentation-mode .inspector{display:none}.presentation-mode .node{padding:9px;border-radius:10px;box-shadow:0 7px 18px #0009}.presentation-mode .node:hover{transform:none}
    .presentation-mode .node-title{font-size:12px;margin:3px 0}.presentation-mode .node-id{font-size:10px}.presentation-mode .badge{padding:2px 5px;font-size:8px}
    .presentation-mode .progress{height:5px;margin:5px 0}.presentation-mode .axes{gap:2px 5px;font-size:8px}.presentation-mode .counts{gap:3px;font-size:8px}.presentation-mode .counts b{font-size:11px}
    .presentation-mode .node-subline{font-size:8px;margin-top:3px}.presentation-mode .task-chip{font-size:8px;padding:3px 5px;margin-top:4px}
    .presentation-mode .task-preview{display:block;font-size:8px;padding:3px 5px;margin-top:3px}.presentation-mode .task-summary{margin-top:4px;font-size:9px;padding:3px 5px}
    .presentation-mode .lane-guide{top:12px;border-radius:11px;background:linear-gradient(180deg,#0d2a3b33,#06121d11)}
    .presentation-mode .lane-guide strong{padding:7px 9px 1px;font-size:9px}.presentation-mode .lane-guide span{padding:0 9px;font-size:8px}
    .presentation-mode .edge{stroke-width:2}.presentation-mode .edge.blocking{stroke-width:3}.presentation-mode .edge.soft{stroke-opacity:.35}
    @media(max-width:1100px){.workspace{grid-template-columns:220px minmax(0,1fr)}.summary{grid-template-columns:repeat(4,1fr)}.graph-shell{grid-template-columns:1fr;height:760px}
      .inspector{display:none}}@media(max-width:650px){.summary{grid-template-columns:repeat(2,1fr)}.topbar{padding:0 12px}
      .workspace{display:block}.project-column{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line);padding:10px 8px}.project-column .project-caption{display:none}.project-card{margin:6px 0}.project-header{padding-left:10px;padding-right:10px}.project-header .project-proof{display:none}
      .graph-shell,.table-wrap,.events-wrap,.machine{margin-left:8px;margin-right:8px}.tabs{overflow:auto}}
  </style>
</head>
<body>
  <header class="topbar"><div><h1 id="app-title">项目施工状态机</h1><div id="app-subtitle" class="sub">阶段是节点 · 依赖是边 · 任务与成员挂在施工位置上</div></div>
    <div class="health"><button id="presentation-toggle" class="screen-button" type="button" aria-pressed="false">⛶ 全屏展示</button><label class="language-switch"><span id="language-label">语言</span><select id="language-select" aria-label="语言"><option value="zh">中文</option><option value="en">English</option></select></label><span id="updated" class="pill">等待数据</span><span id="verify" class="pill">校验中</span></div></header>
  <div class="workspace">
    <aside class="project-column"><div id="project-heading" class="project-heading">项目</div><div id="project-caption" class="project-caption">状态机、施工列表与变更流都属于当前选中的项目。项目标识在三种视图之间保持一致。</div><div id="project-list"></div></aside>
    <section class="project-panel">
      <div id="project-header" class="project-header"><div><h2 id="project-loading-title">正在读取项目……</h2><p id="project-loading-subtitle">等待已验证施工状态</p></div><div class="project-proof">project_id：—</div></div>
      <nav class="tabs"><button id="tab-graph" class="tab active" data-view="graph">项目状态机</button><button id="tab-table" class="tab" data-view="table">施工列表</button>
        <button id="tab-events" class="tab" data-view="events">项目变更流</button><button id="tab-workgroups" class="tab" onclick="location.href=(new URLSearchParams(location.search).get('demo')==='1'?'/workgroups?demo=1':'/workgroups')">工作组</button></nav>
      <div id="warning" class="warning"></div><section id="summary" class="summary"></section>
  <main>
    <section id="view-graph" class="view active">
      <div class="toolbar"><button class="tool" id="zoom-out" aria-label="缩小">－</button><button class="tool" id="zoom-in" aria-label="放大">＋</button>
        <button class="tool" id="fit">适应窗口</button><span id="graph-mode" class="graph-mode">DAG · 依赖驱动</span><label class="layout-control"><span id="structure-label">结构</span><select id="structure-select" aria-label="结构"><option value="elk-right">左→右 DAG</option><option value="elk-down">上→下 DAG</option><option value="elk-spacious">宽松 DAG</option><option value="elk-compact">紧凑 DAG</option><option value="dagre-right">Dagre 左→右</option><option value="dagre-down">Dagre 上→下</option><option value="tree-right">树形投影 左→右</option><option value="tree-down">树形投影 上→下</option><option value="radial">径向布局</option><option value="radial-tree">径向树</option><option value="concentric">同心圆布局</option><option value="circular">环形布局</option><option value="force">力导向布局</option><option value="fruchterman">Fruchterman 力导向</option><option value="forceatlas2">ForceAtlas2</option><option value="mds">MDS 距离布局</option><option value="random">随机探索</option><option value="grid">网格总览</option><option value="compact-box">紧凑盒状布局</option><option value="combo">组合分区布局</option><option value="snake">蛇形流程</option><option value="fishbone">鱼骨流程</option><option value="mindmap">思维导图</option><option value="dendrogram">树状图</option><option value="indented">缩进树</option><option value="manual">手动布局</option><option value="webgl3d">真实 3D · WebGL</option></select></label><button class="tool" id="layout-edit" type="button">编辑视图</button><button class="tool" id="reset-layout" type="button">重置视图</button><span class="view-draft-note" id="view-draft-note">仅保存本机布局</span><label class="edge-toggle"><input id="show-soft" type="checkbox"> <span id="show-soft-label">显示软依赖</span> <span id="soft-count" class="legend"></span></label><span id="graph-legend" class="legend">实线＝硬依赖　虚线＝软依赖　红边＝当前阻塞　金边＝关键路径</span><span class="spacer"></span>
        <select id="status-filter"><option value="">全部状态</option></select></div>
      <div class="manual-tools" id="manual-tools"><button class="tool manual-action" id="manual-undo" type="button">撤销</button><button class="tool manual-action" id="manual-redo" type="button">重做</button><button class="tool manual-action" id="manual-arrange" type="button">自动整理</button><label class="manual-snap manual-action"><input id="manual-snap-input" type="checkbox" checked> <span id="manual-snap-label">网格吸附</span></label><span id="manual-help" class="manual-help">进入编辑后可拖动节点；位置只保存到本机</span></div>
      <div class="graph-shell"><div id="viewport" class="viewport"><div id="webgl3d" class="webgl3d" aria-label="真实三维流程图"><div id="webgl3d-status" class="webgl3d-status"></div></div><div id="canvas" class="canvas"><svg id="edges" class="edges"></svg><div id="lane-guides"></div><div id="nodes"></div></div></div>
        <aside id="inspector" class="inspector"><div id="inspector-empty" class="empty">点击阶段节点查看施工任务和成员。</div></aside></div>
      <div id="machine" class="machine"></div>
    </section>
    <section id="view-table" class="view"><div class="toolbar"><input id="search" class="tool" placeholder="筛选阶段、任务或成员">
      <label class="legend"><input id="only-tasks" type="checkbox"> <span id="only-tasks-label">只看有任务</span></label><label class="legend"><input id="only-blocked" type="checkbox"> <span id="only-blocked-label">只看阻塞</span></label></div>
      <div class="table-wrap"><table><thead><tr><th id="th-stage">阶段</th><th id="th-status">状态</th><th id="th-completion">完成度</th><th id="th-tasks">施工任务／成员</th><th id="th-blockers">依赖阻塞</th><th id="th-wheels">成熟轮子</th><th id="th-next">合法下一步</th><th id="th-consumer">直接消费者</th></tr></thead><tbody id="stage-rows"></tbody></table></div></section>
    <section id="view-events" class="view"><div id="events" class="events-wrap"></div></section>
  </main>
    </section>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/@dagrejs/dagre@1.1.4/dist/dagre.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/elkjs@0.11.1/lib/elk.bundled.js"></script>
  <script>
    function preferredLanguage(){const query=new URLSearchParams(location.search).get('lang');if(query==='en'||query==='zh')return query;try{const stored=localStorage.getItem('construction-language');if(stored==='en'||stored==='zh')return stored}catch(_error){}return 'zh'}
    const S={bundle:null,scale:.82,selected:null,view:'graph',projectId:null,lang:preferredLanguage(),elk:null,layoutToken:0,editMode:false,manualPositions:new Map(),manualHistory:[],manualHistoryIndex:-1,manualSnap:true,draftProjectId:null,currentLayout:null,webgl:null};
    const I18N={
       zh:{documentTitle:'项目施工状态机',demoDocumentTitle:'动态施工状态机 · 匿名演示',appTitle:'项目施工状态机',appSubtitle:'阶段是节点 · 依赖是边 · 任务与成员挂在施工位置上',language:'语言',chinese:'中文',english:'English',presentation:'⛶ 全屏展示',exitPresentation:'⛶ 退出展示',waitingData:'等待数据',checking:'校验中',project:'项目',projectCaption:'状态机、施工列表与变更流都属于当前选中的项目。项目标识在三种视图之间保持一致。',loadingProject:'正在读取项目……',waitingVerified:'等待已验证施工状态',projectStateMachine:'项目状态机',constructionList:'施工列表',projectChangeStream:'项目变更流',workgroup:'工作组',zoomOut:'缩小',zoomIn:'放大',fit:'适应窗口',structure:'结构',structureRight:'左→右 DAG',structureDown:'上→下 DAG',structureSpacious:'宽松 DAG',structureCompact:'紧凑 DAG',dagreRight:'Dagre 左→右',dagreDown:'Dagre 上→下',treeRight:'树形投影 左→右',treeDown:'树形投影 上→下',radial:'径向布局',radialTree:'径向树',concentric:'同心圆布局',circular:'环形布局',force:'力导向布局',fruchterman:'Fruchterman 力导向',forceatlas2:'ForceAtlas2',force3d:'D3 Force 3D 投影',mds:'MDS 距离布局',random:'随机探索',grid:'网格总览',compactBox:'紧凑盒状布局',combo:'组合分区布局',snake:'蛇形流程',fishbone:'鱼骨流程',mindmap:'思维导图',dendrogram:'树状图',indented:'缩进树',structureManual:'手动布局',editView:'编辑视图',editingView:'完成编辑',resetView:'重置视图',viewDraftNote:'仅保存本机布局',dagMode:'DAG · 依赖驱动',elkMode:'ELK · 分层正交布局',dagWarning:'依赖图存在环 · 降级展示',dagLayer:'依赖层',showSoft:'显示软依赖',softCount:'（{n} 条可选）',graphLegend:'实线＝硬依赖　虚线＝软依赖　红边＝当前阻塞　金边＝关键路径',allStatuses:'全部状态',clickNode:'点击阶段节点查看施工任务和成员。',nodeClickHint:'点击节点查看任务详情',searchPlaceholder:'筛选阶段、任务或成员',onlyTasks:'只看有任务',onlyBlocked:'只看阻塞',stage:'阶段',status:'状态',completion:'完成度',tasksMembers:'施工任务／成员',blockers:'依赖阻塞',wheels:'成熟轮子',nextStep:'合法下一步',consumer:'直接消费者',currentProject:'当前施工项目',projectIdPending:'项目标识待同步',selectedProject:'当前选中项目',nodes:'节点',edges:'边',updated:'更新时间',projectViews:'项目内视图：状态机、施工列表、项目变更流与工作组入口',verifiedSource:'已验证施工状态',metricStage:'阶段',metricActiveStages:'有任务活动阶段',metricActiveTasks:'活动任务',metricQueuedTasks:'排队任务',metricPendingReview:'待项目验收',metricBlockedStages:'阻塞阶段',metricStaleTasks:'过期私有任务',metricCriticalNode:'当前关键节点',effect:'效果',subgates:'子门',active:'活动',queued:'排队',pendingReview:'送审',blocked:'阻塞',laneDefault:'施工分区',capability:'能力',activity:'活动',gate:'门',acceptance:'验收',estimateNotice:'阶段完成度仅为历史估算 {n}%，禁止合成为单一项目百分比。',target:'目标',currentTasksMembers:'当前施工任务与成员',noTasks:'当前没有挂载任务。',dependenciesBlockers:'依赖与阻塞',hardDependencies:'硬依赖',softDependencies:'软依赖',blocker:'阻塞',capabilityTracks:'能力轨',legalNext:'合法下一步',deliveryConsumer:'交付／消费者',claimCeiling:'主张上限',none:'无',readOnlyProjection:'验收维度只读投影',legalTransitions:'合法状态迁移',orthogonalState:'正交状态',typedSideStates:'类型化侧状态',phaseSubgates:'阶段内子门',overallPercentDisabled:'单一全项目百分比：禁用',blocking:'阻塞',enters:'进入',after:'后恢复进入阻塞前状态',frontendNoWrite:'前端无状态写权限。',scenarioRule:'演示施工规则',noMatchingStages:'没有符合筛选条件的阶段。',noEvents:'暂无变更事件。',updatePrefix:'更新 ',dataVerified:'数据已验证',lastVerifiedView:'使用最后验证视图',readError:'施工表读取失败：',noConstructionStatus:'无施工状态',dataInvalid:'施工表数据不可验证：',optional:'可选',unbound:'未绑定',noMountedTasks:'当前没有挂载任务。',readOnly:'只读',verified:'已验证',stateNotProvided:'未提供',atomicTasks:'原子任务',masterSteps:'主控九步',masterStepOrder:'主控观测顺序',taskDetails:'原子任务列表',taskStatus:'任务状态',rawStatus:'原始状态',effectiveStatus:'有效投影状态',claimStatus:'领取状态',claimNotProvided:'状态源未单独提供',memberBinding:'成员绑定',memberBound:'已绑定成员',noMemberBinding:'未绑定成员',taskGroup:'工作组',projectAcceptance:'项目验收',observedAt:'观测时间',verifiedTitle:'名称已验证',masterSource:'主控观测任务',noRegisteredTasks:'当前大块没有登记的原子任务。',taskSource:'来源'},
       en:{documentTitle:'Project Construction State Machine',demoDocumentTitle:'Dynamic Construction State Machine · Anonymous Demo',appTitle:'Project Construction State Machine',appSubtitle:'Stages are nodes · dependencies are edges · tasks and members attach to work positions',language:'Language',chinese:'Chinese',english:'English',presentation:'⛶ Fullscreen',exitPresentation:'⛶ Exit fullscreen',waitingData:'Waiting for data',checking:'Checking',project:'Project',projectCaption:'The state machine, work list, and change stream belong to the selected project. The project identity stays consistent across views.',loadingProject:'Loading project…',waitingVerified:'Waiting for verified construction status',projectStateMachine:'State machine',constructionList:'Work list',projectChangeStream:'Change stream',workgroup:'Workgroups',zoomOut:'Zoom out',zoomIn:'Zoom in',fit:'Fit to window',structure:'Structure',structureRight:'Left → right DAG',structureDown:'Top → bottom DAG',structureSpacious:'Spacious DAG',structureCompact:'Compact DAG',dagreRight:'Dagre left → right',dagreDown:'Dagre top → bottom',treeRight:'Tree projection left → right',treeDown:'Tree projection top → bottom',radial:'Radial layout',radialTree:'Radial tree',concentric:'Concentric layout',circular:'Circular layout',force:'Force-directed layout',fruchterman:'Fruchterman force',forceatlas2:'ForceAtlas2',force3d:'D3 Force 3D projection',mds:'MDS distance layout',random:'Random exploration',grid:'Grid overview',compactBox:'Compact box layout',combo:'Combo partition layout',snake:'Snake flow',fishbone:'Fishbone flow',mindmap:'Mind map',dendrogram:'Dendrogram',indented:'Indented tree',structureManual:'Manual layout',editView:'Edit view',editingView:'Finish editing',resetView:'Reset view',viewDraftNote:'Local layout only',dagMode:'DAG · dependency-driven',elkMode:'ELK · layered orthogonal layout',dagWarning:'Dependency cycle · degraded layout',dagLayer:'Dependency layer',showSoft:'Show soft dependencies',softCount:'({n} optional)',graphLegend:'Solid = hard dependency　dashed = soft dependency　red edge = blocker　gold edge = critical path',allStatuses:'All statuses',clickNode:'Select a stage to inspect its tasks and members.',nodeClickHint:'Select a node to inspect task details',searchPlaceholder:'Filter stages, tasks, or members',onlyTasks:'Tasks only',onlyBlocked:'Blocked only',stage:'Stage',status:'Status',completion:'Completion',tasksMembers:'Tasks / members',blockers:'Dependency blockers',wheels:'Mature wheels',nextStep:'Legal next step',consumer:'Direct consumer',currentProject:'Current construction project',projectIdPending:'Project ID pending synchronization',selectedProject:'Selected project',nodes:'nodes',edges:'edges',updated:'Updated',projectViews:'Project views: state machine, work list, change stream, and workgroup entry',verifiedSource:'Verified construction status',metricStage:'Stages',metricActiveStages:'Stages with active work',metricActiveTasks:'Active tasks',metricQueuedTasks:'Queued tasks',metricPendingReview:'Pending project review',metricBlockedStages:'Blocked stages',metricStaleTasks:'Stale private tasks',metricCriticalNode:'Critical node',effect:'Effect',subgates:'subgates',active:'active',queued:'queued',pendingReview:'in review',blocked:'blocked',laneDefault:'Work lane',capability:'Capability',activity:'Activity',gate:'Gate',acceptance:'Acceptance',estimateNotice:'Stage completion is a historical estimate of {n}%; it must not be collapsed into a single project percentage.',target:'Target',currentTasksMembers:'Current tasks and members',noTasks:'No tasks are attached to this stage.',dependenciesBlockers:'Dependencies and blockers',hardDependencies:'Hard dependencies',softDependencies:'Soft dependencies',blocker:'Blocker',capabilityTracks:'Capability tracks',legalNext:'Legal next step',deliveryConsumer:'Delivery / consumer',claimCeiling:'Claim ceiling',none:'None',readOnlyProjection:'Acceptance dimensions are read-only projections',legalTransitions:'Legal transitions',orthogonalState:'Orthogonal state',typedSideStates:'Typed side states',phaseSubgates:'Phase subgates',overallPercentDisabled:'Single project-wide percentage: disabled',blocking:'Blocking',enters:' enters ',after:' then returns to the previous construction state',frontendNoWrite:'The frontend has no state-write permission.',scenarioRule:'Demo construction rule',noMatchingStages:'No stages match the current filters.',noEvents:'No change events.',updatePrefix:'Updated ',dataVerified:'Data verified',lastVerifiedView:'Showing the last verified view',readError:'Construction status read failed: ',noConstructionStatus:'No construction status',dataInvalid:'Construction data cannot be verified: ',optional:'optional',unbound:'unbound',noMountedTasks:'No tasks are attached.',readOnly:'read-only',verified:'verified',stateNotProvided:'not provided',atomicTasks:'Atomic tasks',masterSteps:'Controller nine steps',masterStepOrder:'Controller observation order',taskDetails:'Atomic task list',taskStatus:'Task status',rawStatus:'Raw status',effectiveStatus:'Effective projection',claimStatus:'Claim status',claimNotProvided:'Not provided as a separate field',memberBinding:'Member binding',memberBound:'Member bound',noMemberBinding:'No member binding',taskGroup:'Workgroup',projectAcceptance:'Project acceptance',observedAt:'Observed at',verifiedTitle:'Title verified',masterSource:'Controller observation',noRegisteredTasks:'No atomic tasks are registered under this block.',taskSource:'Source'}
    };
    Object.assign(I18N.zh,{webgl3d:'真实 3D · WebGL',webglReadOnly:'真实三维只读观察：滚轮缩放，拖动旋转，右键平移',webglFallback:'WebGL 不可用，已回退到普通 DAG',webglLoading:'正在加载真实三维图层……',undo:'撤销',redo:'重做',autoArrange:'自动整理',snapGrid:'网格吸附',manualHelp:'进入编辑后拖动节点；支持网格吸附、撤销/重做；位置只保存到本机'});
    Object.assign(I18N.en,{webgl3d:'True 3D · WebGL',webglReadOnly:'True 3D read-only view: wheel to zoom, drag to orbit, right-drag to pan',webglFallback:'WebGL unavailable; fell back to the ordinary DAG',webglLoading:'Loading the true 3D layer…',undo:'Undo',redo:'Redo',autoArrange:'Auto arrange',snapGrid:'Snap to grid',manualHelp:'Drag nodes in edit mode; grid snapping and undo/redo are available; positions stay local'});
    const VALUE_LABELS={
      ACTIVE:{zh:'进行中',en:'ACTIVE'},READY:{zh:'就绪',en:'READY'},QUEUED:{zh:'排队',en:'QUEUED'},ACCEPTANCE:{zh:'待验收',en:'ACCEPTANCE'},PASS:{zh:'已通过',en:'PASS'},BLOCKED:{zh:'已阻塞',en:'BLOCKED'},NOT_STARTED:{zh:'未开始',en:'NOT STARTED'},IDLE:{zh:'空闲',en:'IDLE'},ACCEPTED_NARROW:{zh:'窄范围接纳',en:'ACCEPTED NARROW'},COMPONENT_READY:{zh:'组件就绪',en:'COMPONENT READY'},REFERENCE_ONLY:{zh:'仅供参考',en:'REFERENCE ONLY'},DEFERRED:{zh:'已后置',en:'DEFERRED'},PENDING:{zh:'待定',en:'PENDING'},PENDING_REVIEW:{zh:'待审查',en:'PENDING REVIEW'},STALE_PRIVATE_PROJECTION:{zh:'过期私有投影',en:'STALE PRIVATE PROJECTION'},OPEN:{zh:'开放',en:'OPEN'},READ_ONLY:{zh:'只读',en:'READ-ONLY'},PARTIAL:{zh:'部分完成',en:'PARTIAL'},NOT_MEASURED:{zh:'未测量',en:'NOT MEASURED'},COMPONENT_ONLY:{zh:'仅组件',en:'COMPONENT ONLY'},TERMINAL:{zh:'终态',en:'TERMINAL'},COMPLETED:{zh:'已完成',en:'COMPLETED'},DISPATCHED_ACTIVE:{zh:'已派发／进行中',en:'DISPATCHED ACTIVE'},NOT_INFERRED:{zh:'未推断',en:'NOT INFERRED'},CONTROLLER_OBSERVED:{zh:'总控观测',en:'CONTROLLER OBSERVED'},MEMBER_BOUND:{zh:'已绑定成员',en:'MEMBER BOUND'},NO_MEMBER_BINDING:{zh:'未绑定成员',en:'NO MEMBER BINDING'},CLAIMED:{zh:'已领取',en:'CLAIMED'},IN_PROGRESS:{zh:'执行中',en:'IN PROGRESS'},UNCLAIMED:{zh:'待领取',en:'UNCLAIMED'},EXPIRED:{zh:'已过期',en:'EXPIRED'},NOT_PROVIDED:{zh:'未提供',en:'NOT PROVIDED'}};
    const EVENT_TYPE_LABELS={
      DEMO_INITIALIZED:{zh:'演示初始化',en:'Demo initialized'},
      PARALLEL_BRANCH_STARTED:{zh:'并行分支启动',en:'Parallel branch started'},
      EVIDENCE_ATTACHED:{zh:'证据已附加',en:'Evidence attached'},
      TASK_QUEUED:{zh:'任务进入队列',en:'Task queued'},
      BLOCKER_OPENED:{zh:'阻塞已打开',en:'Blocker opened'},
      PARALLEL_BRANCH_CONTINUED:{zh:'并行分支继续',en:'Parallel branch continued'},
      TASK_ACTIVE:{zh:'任务开始执行',en:'Task became active'},
      MERGE_WAITING:{zh:'等待汇合',en:'Waiting for convergence'},
      ACCEPTANCE_WAITING:{zh:'等待验收',en:'Waiting for acceptance'},
      PRIVATE_PROJECTION_EXPIRED:{zh:'私有投影已过期',en:'Private projection expired'},
      ACCEPTANCE_BARRIER_WAITING:{zh:'等待验收屏障',en:'Waiting at acceptance barrier'}
    };
    const EVENT_KEY_LABELS={
      status:{zh:'状态',en:'Status'},source:{zh:'来源',en:'Source'},parallel_branches:{zh:'并行分支',en:'Parallel branches'},branch:{zh:'分支',en:'Branch'},task_id:{zh:'任务ID',en:'Task ID'},evidence_count:{zh:'证据数量',en:'Evidence count'},waits_for:{zh:'等待',en:'Waits for'},blocker_id:{zh:'阻塞ID',en:'Blocker ID'},blocks:{zh:'阻塞目标',en:'Blocks'},reason:{zh:'原因',en:'Reason'},note:{zh:'说明',en:'Note'},parallel_with:{zh:'并行于',en:'Runs in parallel with'},requires:{zh:'依赖',en:'Requires'},formal_activity_counted:{zh:'计入正式活动',en:'Counted as formal activity'}
    };
    const EVENT_TEXT_LABELS={
      'data-and-identity':{zh:'数据与身份',en:'Data & identity'},
      'rules-and-dependencies':{zh:'规则与依赖',en:'Rules & dependencies'},
      '证据分支尚未达到边界验收门':{zh:'证据分支尚未达到边界验收门',en:'The evidence branch has not reached the boundary acceptance gate'},
      '不依赖S04的运行时分支继续施工':{zh:'不依赖 S04 的运行时分支继续施工',en:'The runtime branch independent of S04 continues construction'},
      '阻塞未解除，禁止越过汇合节点':{zh:'阻塞未解除，禁止越过汇合节点',en:'The blocker remains open; the convergence node cannot be bypassed'},
      'S14只能以隔离旁路形式被观察':{zh:'S14 只能以隔离旁路形式被观察',en:'S14 can only be observed through an isolated side route'}
    };
    const eventTypeLabel=(value)=>EVENT_TYPE_LABELS[String(value)]?.[S.lang]||String(value??'—').replaceAll('_',' ');
    const eventKeyLabel=(value)=>EVENT_KEY_LABELS[String(value)]?.[S.lang]||String(value??'—').replaceAll('_',' ');
    const eventTextLabel=(value)=>EVENT_TEXT_LABELS[String(value)]?.[S.lang]||String(value??'—');
    const eventValueLabel=(key,value)=>{if(Array.isArray(value))return value.map(item=>eventValueLabel('',item)).join(S.lang==='en'?', ': '、');if(value&&typeof value==='object')return JSON.stringify(value);if(typeof value==='boolean')return value?(S.lang==='en'?'Yes':'是'):(S.lang==='en'?'No':'否');if(key==='status')return valueLabel(value);if(key==='source')return sourceLabel(value);return eventTextLabel(value)};
    const eventPayloadHtml=(payload)=>{if(!payload||typeof payload!=='object')return `<span class="event-pair">${esc(eventTextLabel(payload))}</span>`;return Object.entries(payload).map(([key,value])=>`<span class="event-pair"><b>${esc(eventKeyLabel(key))}：</b>${esc(eventValueLabel(key,value))}</span>`).join(' · ')};
    const tr=(key,vars={})=>{let value=(I18N[S.lang]||I18N.zh)[key]??I18N.zh[key]??key;return Object.entries(vars).reduce((text,[name,replacement])=>text.replaceAll(`{${name}}`,String(replacement??'')),value)};
    const AXIS_LABELS={capability:{zh:'能力',en:'Capability'},activity:{zh:'活动',en:'Activity'},effect:{zh:'效果',en:'Effect'},project_acceptance:{zh:'项目验收',en:'Project acceptance'}};
    const valueLabel=(value)=>{const key=String(value??'');return VALUE_LABELS[key]?.[S.lang]||AXIS_LABELS[key]?.[S.lang]||key.replaceAll('_',' ')};
    const sourceLabel=(value)=>{const key=String(value??'');if(key==='anonymous fixture')return S.lang==='en'?'Anonymous fixture':'匿名演示数据';return key||tr('verifiedSource')};
    const localized=(object,field,fallback='—')=>{if(!object)return fallback;const translated=S.lang==='en'?object[`${field}_en`]:null;return translated??object[field]??fallback};
    function mergeTasks(...lists){const byId=new Map();lists.flat().forEach(task=>{if(!task||typeof task!=='object')return;const key=String(task.task_id||`${task.display_title||task.title||'task'}-${byId.size}`);if(!byId.has(key))byId.set(key,task)});return [...byId.values()]}
    function tasksForNode(node){const projection=S.bundle?.task_projection?.tasks_by_stage?.[node.node_id]||[];return mergeTasks(projection,node.task_attachments||[])}
    function tasksForStage(stage){const projection=S.bundle?.task_projection?.tasks_by_stage?.[stage.stage_id]||[];return mergeTasks(projection,stage.active_tasks||[],stage.queued_tasks||[],stage.pending_review_tasks||[],stage.stale_private_tasks||[])}
    const taskRawStatus=(task)=>task?.task_status_raw||task?.task_status||task?.status||'NOT_PROVIDED';
    const taskEffectiveStatus=(task)=>task?.effective_status_class||task?.task_status_class||taskRawStatus(task);
    const taskClaimLabel=(task)=>{const explicit=task?.claim_status||task?.claim_state;return explicit?valueLabel(explicit):tr('claimNotProvided')};
    const taskTitle=(task)=>localized(task,'display_title',task?.title||task?.task_id||'—');
    function taskPreviewHtml(task){const effective=taskEffectiveStatus(task),className=effective==='STALE_PRIVATE_PROJECTION'?'stale':effective==='TERMINAL'?'terminal':'';return `<div class="task-preview ${className}">${esc(short(taskTitle(task),56))} · ${esc(valueLabel(taskRawStatus(task)))} · ${esc(taskClaimLabel(task))}</div>`}
    function taskDetailHtml(task){const raw=taskRawStatus(task),effective=taskEffectiveStatus(task),master=task.master_controller_step===true;const step=master&&task.controller_step_index?`<span class="badge">${esc(tr('masterStepOrder'))} ${task.controller_step_index}/${task.controller_step_count||'?'}</span>`:'';const member=task.member_id?`${tr('memberBound')} · ${task.member_id}`:tr('noMemberBinding');return `<div class="task-card"><div class="title">${esc(taskTitle(task))}</div><div class="task-meta">${step}${master?` <span class="badge">${esc(tr('masterSource'))}</span>`:''}<br><b>${esc(tr('taskStatus'))}：</b>${esc(valueLabel(raw))} · <b>${esc(tr('effectiveStatus'))}：</b>${esc(valueLabel(effective))}<br><b>${esc(tr('claimStatus'))}：</b>${esc(taskClaimLabel(task))} · <b>${esc(tr('memberBinding'))}：</b>${esc(member)}<br><b>${esc(tr('taskGroup'))}：</b>${esc(task.group_id||'—')} · <b>${esc(tr('projectAcceptance'))}：</b>${esc(valueLabel(task.project_acceptance||'NOT_PROVIDED'))}<br><b>${esc(tr('observedAt'))}：</b>${esc(task.observed_at||'—')} · <b>${esc(tr('verifiedTitle'))}：</b>${esc(task.title_verified_from_map===true?tr('verified'):tr('stateNotProvided'))}</div><code>${esc(task.task_id||'—')}</code></div>`}
    function applyLocale(){document.documentElement.lang=S.lang==='en'?'en':'zh-CN';document.title=new URLSearchParams(location.search).get('demo')==='1'?tr('demoDocumentTitle'):tr('documentTitle');const textMap={'#app-title':'appTitle','#app-subtitle':'appSubtitle','#language-label':'language','#project-heading':'project','#project-caption':'projectCaption','#project-loading-title':'loadingProject','#project-loading-subtitle':'waitingVerified','#tab-graph':'projectStateMachine','#tab-table':'constructionList','#tab-events':'projectChangeStream','#tab-workgroups':'workgroup','#fit':'fit','#structure-label':'structure','#layout-edit':S.editMode?'editingView':'editView','#reset-layout':'resetView','#graph-mode':'dagMode','#show-soft-label':'showSoft','#graph-legend':'graphLegend','#only-tasks-label':'onlyTasks','#only-blocked-label':'onlyBlocked','#th-stage':'stage','#th-status':'status','#th-completion':'completion','#th-tasks':'tasksMembers','#th-blockers':'blockers','#th-wheels':'wheels','#th-next':'nextStep','#th-consumer':'consumer','#inspector-empty':'clickNode'};Object.entries(textMap).forEach(([selector,key])=>{const element=document.querySelector(selector);if(element)element.textContent=tr(key)});const structure=document.querySelector('#structure-select');if(structure){structure.setAttribute('aria-label',tr('structure'));const labels={"elk-right":'structureRight',"elk-down":'structureDown',"elk-spacious":'structureSpacious',"elk-compact":'structureCompact',"dagre-right":'dagreRight',"dagre-down":'dagreDown',"tree-right":'treeRight',"tree-down":'treeDown',radial:'radial',concentric:'concentric',force:'force',grid:'grid',snake:'snake',fishbone:'fishbone',mindmap:'mindmap',manual:'structureManual',webgl3d:'webgl3d'};Array.from(structure.options).forEach(option=>{if(labels[option.value])option.textContent=tr(labels[option.value])})}const draftNote=document.querySelector('#view-draft-note');if(draftNote)draftNote.textContent=tr('viewDraftNote');const presentationButton=document.querySelector('#presentation-toggle');if(presentationButton)presentationButton.textContent=document.body.classList.contains('presentation-mode')?tr('exitPresentation'):tr('presentation');const search=document.querySelector('#search');if(search)search.placeholder=tr('searchPlaceholder');const zoomOut=document.querySelector('#zoom-out');if(zoomOut)zoomOut.setAttribute('aria-label',tr('zoomOut'));const zoomIn=document.querySelector('#zoom-in');if(zoomIn)zoomIn.setAttribute('aria-label',tr('zoomIn'));const languageSelect=document.querySelector('#language-select');if(languageSelect){languageSelect.value=S.lang;languageSelect.setAttribute('aria-label',tr('language'));languageSelect.options[0].text=tr('chinese');languageSelect.options[1].text=tr('english')}}
    function applyStructureOptionLabels(){const structure=document.querySelector('#structure-select');if(!structure)return;const labels={"elk-right":'structureRight',"elk-down":'structureDown',"elk-spacious":'structureSpacious',"elk-compact":'structureCompact',"dagre-right":'dagreRight',"dagre-down":'dagreDown',"tree-right":'treeRight',"tree-down":'treeDown',radial:'radial',"radial-tree":'radialTree',concentric:'concentric',circular:'circular',force:'force',fruchterman:'fruchterman',forceatlas2:'forceatlas2',force3d:'force3d',mds:'mds',random:'random',grid:'grid',"compact-box":'compactBox',combo:'combo',snake:'snake',fishbone:'fishbone',mindmap:'mindmap',dendrogram:'dendrogram',indented:'indented',manual:'structureManual',webgl3d:'webgl3d'};Array.from(structure.options).forEach(option=>{if(labels[option.value])option.textContent=tr(labels[option.value])})}
    function ensureStructureOptions(){const structure=document.querySelector('#structure-select');if(!structure)return;const extras=[['webgl3d',tr('webgl3d')]];extras.forEach(([value,label])=>{if(!structure.querySelector(`option[value="${value}"]`)){const option=document.createElement('option');option.value=value;option.textContent=label;structure.appendChild(option)}})}
    function applyManualLocale(){const map={'#manual-undo':'undo','#manual-redo':'redo','#manual-arrange':'autoArrange','#manual-snap-label':'snapGrid','#manual-help':'manualHelp'};Object.entries(map).forEach(([selector,key])=>{const element=document.querySelector(selector);if(element)element.textContent=tr(key)});const snap=document.querySelector('#manual-snap-input');if(snap)snap.setAttribute('aria-label',tr('snapGrid'))}
    const esc=(v)=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const short=(v,n=86)=>{v=String(v??'');return v.length>n?v.slice(0,n-1)+'…':v};
    function metric(label,value){return `<div class="metric"><b>${esc(value)}</b><span>${esc(label)}</span></div>`}
    function setView(name){S.view=name;document.querySelectorAll('.tab[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
      document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));document.querySelector('#view-'+name).classList.add('active');
      if(name==='graph')requestAnimationFrame(renderGraph)}
    document.querySelectorAll('.tab[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view));
    function stageMap(){return new Map((S.bundle?.status?.stages||[]).map(x=>[x.stage_id,x]))}
    const STRUCTURE_PROFILES=new Set(['elk-right','elk-down','elk-spacious','elk-compact','dagre-right','dagre-down','tree-right','tree-down','radial','radial-tree','concentric','circular','force','fruchterman','forceatlas2','mds','random','grid','compact-box','combo','snake','fishbone','mindmap','dendrogram','indented','manual','webgl3d']);
    function preferredStructure(){const query=new URLSearchParams(location.search).get('layout');if(STRUCTURE_PROFILES.has(query))return query;try{const stored=localStorage.getItem('construction-layout');if(STRUCTURE_PROFILES.has(stored))return stored}catch(_error){}return 'elk-right'}
    function selectedStructure(){const value=document.querySelector('#structure-select')?.value||preferredStructure();return STRUCTURE_PROFILES.has(value)?value:'elk-right'}
    function layoutModeLabel(layout){if(layout.cycleDetected)return tr('dagWarning');const keys={"elk-right":'structureRight',"elk-down":'structureDown',"elk-spacious":'structureSpacious',"elk-compact":'structureCompact',"dagre-right":'dagreRight',"dagre-down":'dagreDown',"tree-right":'treeRight',"tree-down":'treeDown',radial:'radial',"radial-tree":'radialTree',concentric:'concentric',circular:'circular',force:'force',fruchterman:'fruchterman',forceatlas2:'forceatlas2',mds:'mds',random:'random',grid:'grid',"compact-box":'compactBox',combo:'combo',snake:'snake',fishbone:'fishbone',mindmap:'mindmap',dendrogram:'dendrogram',indented:'indented',manual:'structureManual',webgl3d:'webgl3d'};return keys[layout.layoutProfile]?tr(keys[layout.layoutProfile]):layout.engine==='elk'?tr('elkMode'):tr('dagMode')}
    function layoutDraftKey(projectId){return `construction-layout-draft:${String(projectId||'unknown')}`}
    function loadLayoutDraft(projectId){S.manualPositions=new Map();S.manualHistory=[];S.manualHistoryIndex=-1;S.draftProjectId=String(projectId||'unknown');try{const raw=localStorage.getItem(layoutDraftKey(S.draftProjectId)),value=raw?JSON.parse(raw):{};Object.entries(value&&value.positions||{}).forEach(([id,p])=>{if(Number.isFinite(Number(p?.x))&&Number.isFinite(Number(p?.y)))S.manualPositions.set(id,{x:Number(p.x),y:Number(p.y)})})}catch(_error){}}
    function saveLayoutDraft(){const positions={};S.manualPositions.forEach((p,id)=>{positions[id]={x:Math.round(p.x),y:Math.round(p.y)}});try{localStorage.setItem(layoutDraftKey(S.draftProjectId||S.projectId||'unknown'),JSON.stringify({version:1,project_id:S.projectId,positions}))}catch(_error){} }
    function clearLayoutDraft(){try{localStorage.removeItem(layoutDraftKey(S.draftProjectId||S.projectId||'unknown'))}catch(_error){}S.manualPositions=new Map();S.manualHistory=[];S.manualHistoryIndex=-1;S.currentLayout=null;updateManualButtons()}
    function manualSnapshot(){return new Map([...S.manualPositions.entries()].map(([id,p])=>[id,{x:Number(p.x),y:Number(p.y)}]))}
    function manualSnapshotKey(snapshot){return JSON.stringify([...snapshot.entries()].sort((a,b)=>a[0].localeCompare(b[0])).map(([id,p])=>[id,Math.round(p.x),Math.round(p.y)]))}
    function ensureManualHistory(){if(!S.manualHistory.length){S.manualHistory=[manualSnapshot()];S.manualHistoryIndex=0}updateManualButtons()}
    function commitManualHistory(){ensureManualHistory();const next=manualSnapshot(),last=S.manualHistory[S.manualHistoryIndex];if(last&&manualSnapshotKey(last)===manualSnapshotKey(next))return;S.manualHistory=S.manualHistory.slice(0,S.manualHistoryIndex+1);S.manualHistory.push(next);S.manualHistoryIndex=S.manualHistory.length-1;if(S.manualHistory.length>50){S.manualHistory.shift();S.manualHistoryIndex-=1}updateManualButtons()}
    function restoreManualSnapshot(snapshot){S.manualPositions=new Map([...snapshot.entries()].map(([id,p])=>[id,{x:p.x,y:p.y}]));S.currentLayout=null;saveLayoutDraft();updateManualButtons();renderGraph()}
    function updateManualButtons(){const undo=document.querySelector('#manual-undo'),redo=document.querySelector('#manual-redo');if(undo)undo.disabled=!S.editMode||S.manualHistoryIndex<=0;if(redo)redo.disabled=!S.editMode||S.manualHistoryIndex<0||S.manualHistoryIndex>=S.manualHistory.length-1}
    function undoManual(){if(!S.editMode||S.manualHistoryIndex<=0)return;S.manualHistoryIndex-=1;restoreManualSnapshot(S.manualHistory[S.manualHistoryIndex])}
    function redoManual(){if(!S.editMode||S.manualHistoryIndex<0||S.manualHistoryIndex>=S.manualHistory.length-1)return;S.manualHistoryIndex+=1;restoreManualSnapshot(S.manualHistory[S.manualHistoryIndex])}
    function autoArrangeManual(){if(!S.editMode)return;ensureManualHistory();S.manualPositions=new Map();S.currentLayout=null;commitManualHistory();saveLayoutDraft();renderGraph()}
    function renderProjectChrome(){
      const status=S.bundle.status||{};
      const fallback={project_id:status.project_id||null,title:status.project_title||tr('currentProject'),state:'ACTIVE',updated_at:status.generated_at||'—',source:tr('verifiedSource')};
      const project=status.project||fallback;
      const projectId=project.project_id?String(project.project_id):tr('projectIdPending');
      S.projectId=projectId;
      if(S.draftProjectId!==projectId)loadLayoutDraft(projectId);
      document.querySelector('#project-list').innerHTML=`<div class="project-card active"><span class="project-state">${esc(valueLabel(project.state||'ACTIVE'))}</span><div class="project-title">${esc(localized(project,'title',fallback.title))}</div><div class="project-meta">${esc(tr('selectedProject'))}<br>${esc(tr('nodes'))} ${esc(status.summary?.stage_count??'—')} · ${esc(tr('edges'))} ${esc(status.flow_graph?.edge_count??'—')}<br>${esc(tr('updated'))} ${esc(project.updated_at||status.generated_at||'—')}</div></div>`;
      document.querySelector('#project-header').innerHTML=`<div><h2>${esc(localized(project,'title',fallback.title))}</h2><p>${esc(tr('projectViews'))}</p></div><div class="project-proof">project_id：${esc(projectId)}<br><span>${esc(sourceLabel(localized(project,'source',tr('verifiedSource'))))}</span></div>`;
    }
    function renderSummary(){
      renderProjectChrome();
      const s=S.bundle.status.summary;document.querySelector('#summary').innerHTML=[
        metric(tr('metricStage'),s.stage_count),metric(tr('metricActiveStages'),s.active_stage_count),metric(tr('metricActiveTasks'),s.fresh_active_task_count),
        metric(tr('metricQueuedTasks'),s.fresh_queued_task_count),metric(tr('metricPendingReview'),s.pending_project_review_task_count),
        metric(tr('metricBlockedStages'),s.blocked_stage_count),metric(tr('metricStaleTasks'),s.stale_private_task_count),
        metric(tr('metricCriticalNode'),String(s.critical_path_stage_id||'—').split('_')[0])
      ].join('');
    }
    function computeLayeredLayout(nodes,edges){
      // Sugiyama-style lightweight layout: the dependency graph is the only
      // source of rank. Backend coordinates may break ties, but never decide
      // which dependency layer a node belongs to.
      const ids=new Set(nodes.map(n=>n.node_id)),incoming=new Map(),outgoing=new Map(),indegree=new Map();
      nodes.forEach(n=>{incoming.set(n.node_id,[]);outgoing.set(n.node_id,[]);indegree.set(n.node_id,0)});
      edges.forEach(e=>{if(ids.has(e.source)&&ids.has(e.target)&&e.source!==e.target){
        // Soft dependencies remain visible as optional side rails, but they
        // must not move a node into a later primary execution layer.
        if(e.edge_type!=='HARD_DEPENDENCY')return;
        incoming.get(e.target).push({id:e.source,weight:2});outgoing.get(e.source).push({id:e.target,weight:2});
        indegree.set(e.target,(indegree.get(e.target)||0)+1);
      }});
      const queue=nodes.filter(n=>indegree.get(n.node_id)===0).map(n=>n.node_id),topological=[];
      while(queue.length){const id=queue.shift();topological.push(id);for(const link of outgoing.get(id)||[]){
        indegree.set(link.id,indegree.get(link.id)-1);if(indegree.get(link.id)===0)queue.push(link.id);
      }}
      const cycleDetected=topological.length<nodes.length;
      nodes.forEach(n=>{if(!topological.includes(n.node_id))topological.push(n.node_id)});
      const rank=new Map(nodes.map(n=>[n.node_id,0]));
      for(const id of topological){for(const link of outgoing.get(id)||[]){
        rank.set(link.id,Math.max(rank.get(link.id)||0,(rank.get(id)||0)+1));
      }}
      const layers=new Map();for(const n of nodes){const r=rank.get(n.node_id)||0;(layers.get(r)||layers.set(r,[]).get(r)).push(n)}
      for(const list of layers.values())list.sort((a,b)=>{
        const ar=Number.isFinite(Number(a.layout_row))?Number(a.layout_row):0,br=Number.isFinite(Number(b.layout_row))?Number(b.layout_row):0;
        return ar-br||a.node_id.localeCompare(b.node_id);
      });
      const positionInLayer=()=>{const positions=new Map();for(const list of layers.values())list.forEach((n,i)=>positions.set(n.node_id,i));return positions};
      const barycenter=(links,positions)=>{const values=(links||[]).filter(link=>positions.has(link.id)).map(link=>positions.get(link.id));return values.length?values.reduce((sum,v)=>sum+v,0)/values.length:null};
      for(let sweep=0;sweep<6;sweep++){
        const positions=positionInLayer();
        [...layers.keys()].sort((a,b)=>a-b).slice(1).forEach(r=>{const list=layers.get(r);list.sort((a,b)=>{
          const av=barycenter(incoming.get(a.node_id),positions),bv=barycenter(incoming.get(b.node_id),positions);
          return (av==null?Number.MAX_SAFE_INTEGER:av)-(bv==null?Number.MAX_SAFE_INTEGER:bv)||a.node_id.localeCompare(b.node_id);
        })});
        const nextPositions=positionInLayer();
        [...layers.keys()].sort((a,b)=>b-a).slice(1).forEach(r=>{const list=layers.get(r);list.sort((a,b)=>{
          const av=barycenter(outgoing.get(a.node_id),nextPositions),bv=barycenter(outgoing.get(b.node_id),nextPositions);
          return (av==null?Number.MAX_SAFE_INTEGER:av)-(bv==null?Number.MAX_SAFE_INTEGER:bv)||a.node_id.localeCompare(b.node_id);
        })});
      }
      const presentation=document.body.classList.contains('presentation-mode');
      const NODE_W=presentation?210:238,NODE_H=presentation?150:168,COL_STEP=presentation?246:282,ROW_STEP=presentation?176:188,LEFT=presentation?24:30,TOP=presentation?34:46;
      const maxLayerLength=Math.max(1,...Array.from(layers.values(),list=>list.length));
      const positions=new Map();let maxX=0,maxY=0;
      [...layers.keys()].sort((a,b)=>a-b).forEach(r=>{const list=layers.get(r),offset=(maxLayerLength-list.length)*ROW_STEP/2;
        list.forEach((n,i)=>{
        const x=LEFT+r*COL_STEP,y=TOP+offset+i*ROW_STEP;positions.set(n.node_id,{x,y});
        maxX=Math.max(maxX,x+NODE_W+32);maxY=Math.max(maxY,y+NODE_H+34);
        });
      });
      return {positions,ranks:rank,layers,width:Math.max(maxX,720),height:Math.max(maxY,TOP+maxLayerLength*ROW_STEP+NODE_H+34),nodeWidth:NODE_W,nodeHeight:NODE_H,columnStep:COL_STEP,rowStep:ROW_STEP,left:LEFT,top:TOP,cycleDetected};
    }
    function layersFromPositions(nodes,positions,nodeWidth,direction='RIGHT',nodeHeight=nodeWidth){
      const axis=direction==='DOWN'?'y':'x',size=direction==='DOWN'?nodeHeight:nodeWidth;
      const ordered=nodes.filter(n=>positions.has(n.node_id)).slice().sort((a,b)=>positions.get(a.node_id)[axis]-positions.get(b.node_id)[axis]||positions.get(a.node_id).y-positions.get(b.node_id).y||a.node_id.localeCompare(b.node_id));
      const layers=new Map();let lastAxis=null,rank=-1;
      ordered.forEach(n=>{const value=positions.get(n.node_id)[axis];if(lastAxis===null||Math.abs(value-lastAxis)>Math.max(6,size*.08)){rank++;lastAxis=value}(layers.get(rank)||layers.set(rank,[]).get(rank)).push(n)});
      return layers;
    }
    function elkSectionsToPath(sections,dx=0,dy=0){
      const section=(sections||[])[0];if(!section)return '';
      const points=[section.startPoint,...(section.bendPoints||[]),section.endPoint].filter(Boolean).map(p=>({x:Number(p.x)+dx,y:Number(p.y)+dy}));if(points.length<2)return '';
      return `M${points[0].x},${points[0].y} `+points.slice(1).map(p=>`L${p.x},${p.y}`).join(' ');
    }
    function pointsToPath(points){const usable=(points||[]).filter(p=>Number.isFinite(Number(p?.x))&&Number.isFinite(Number(p?.y)));if(usable.length<2)return '';return `M${usable[0].x},${usable[0].y} `+usable.slice(1).map(p=>`L${p.x},${p.y}`).join(' ')}
    function layoutSignature(profile,direction,nodes,edges){return `${profile}|${direction}|${nodes.map(n=>n.node_id).sort().join(',')}|${edges.filter(e=>e.edge_type==='HARD_DEPENDENCY').map(e=>`${e.source}>${e.target}`).sort().join(',')}`}
    function normalizePositions(nodes,positions,nodeWidth,nodeHeight,padding=46){
      const values=[...positions.values()];if(!values.length)return {positions:new Map(),width:720,height:650};
      const minX=Math.min(...values.map(p=>Number(p.x)||0)),minY=Math.min(...values.map(p=>Number(p.y)||0));
      const normalized=new Map([...positions.entries()].map(([id,p])=>[id,{x:Math.round((Number(p.x)||0)-minX+padding),y:Math.round((Number(p.y)||0)-minY+padding)}]));
      const maxX=Math.max(...[...normalized.values()].map(p=>p.x+nodeWidth),nodeWidth)+padding,maxY=Math.max(...[...normalized.values()].map(p=>p.y+nodeHeight),nodeHeight)+padding;
      return {positions:normalized,width:Math.max(720,Math.round(maxX)),height:Math.max(650,Math.round(maxY)),shiftX:padding-minX,shiftY:padding-minY};
    }
    function offsetPath(path,dx,dy){return String(path||'').replace(/([ML])(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/g,(_,command,x,y)=>`${command}${Number(x)+dx},${Number(y)+dy}`)}
    function layoutRecord(profile,direction,engine,nodes,edges,base,positions,signature,options={}){
      const nodeWidth=options.nodeWidth||base.nodeWidth,nodeHeight=options.nodeHeight||base.nodeHeight,padding=options.padding||46;
      const normalized=normalizePositions(nodes,positions,nodeWidth,nodeHeight,padding),layers=layersFromPositions(nodes,normalized.positions,nodeWidth,direction,nodeHeight),hardEdges=edges.filter(e=>e.edge_type==='HARD_DEPENDENCY'),edgePaths=new Map([...((options.edgePaths)||new Map()).entries()].map(([key,path])=>[key,offsetPath(path,normalized.shiftX,normalized.shiftY)]));
      return {positions:normalized.positions,ranks:base.ranks,layers,width:normalized.width,height:normalized.height,nodeWidth,nodeHeight,columnStep:options.columnStep||0,rowStep:options.rowStep||0,left:padding,top:padding,cycleDetected:base.cycleDetected,engine,layoutProfile:profile,direction,signature,projection:options.projection||null,depths:options.depths||new Map(),basePositions:new Map(normalized.positions),edgeRoutes:new Map(),edgePaths,edgeOffset:{x:normalized.shiftX,y:normalized.shiftY},hardEdgeIndex:new Map(hardEdges.map((e,i)=>[`${e.source}→${e.target}`,i])),showGuides:options.showGuides!==false};
    }
    function hardEdgesFor(nodes,edges){const ids=new Set(nodes.map(n=>n.node_id));return edges.filter(e=>e.edge_type==='HARD_DEPENDENCY'&&ids.has(e.source)&&ids.has(e.target)&&e.source!==e.target)}
    function computeDagreLayout(profile,direction,nodes,edges,base,signature,presentation){
      const dagre=window.dagre;if(!dagre?.graphlib?.Graph||typeof dagre.layout!=='function')return null;
      const nodeWidth=presentation?210:238,nodeHeight=presentation?150:168,g=new dagre.graphlib.Graph().setGraph({rankdir:direction==='DOWN'?'TB':'LR',ranker:'network-simplex',nodesep:presentation?34:58,ranksep:presentation?72:118,marginx:24,marginy:24}).setDefaultEdgeLabel(()=>({}));
      nodes.forEach(n=>g.setNode(n.node_id,{width:nodeWidth,height:nodeHeight}));
      hardEdgesFor(nodes,edges).forEach((e,i)=>g.setEdge(e.source,e.target,{id:`hard-${i}`}));
      try{dagre.layout(g);const positions=new Map(nodes.map(n=>{const p=g.node(n.node_id);return [n.node_id,{x:(p?.x||0)-nodeWidth/2,y:(p?.y||0)-nodeHeight/2}]}));const edgePaths=new Map();g.edges().forEach(edge=>{const points=g.edge(edge)?.points;if(points?.length)edgePaths.set(`${edge.v}→${edge.w}`,pointsToPath(points))});return layoutRecord(profile,direction,'dagre',nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding:presentation?34:46,edgePaths})}catch(error){console.warn('Dagre layout failed; using alternative fallback',error);return null}
    }
    function groupedByRank(base){const groups=new Map();[...base.layers.entries()].sort((a,b)=>a[0]-b[0]).forEach(([rank,list])=>groups.set(rank,list.slice()));return groups}
    function computeAlternativeLayout(profile,direction,nodes,edges,base,signature,presentation){
      if(profile==='dagre-right'||profile==='dagre-down')return computeDagreLayout(profile,direction,nodes,edges,base,signature,presentation);
      const nodeWidth=presentation?210:238,nodeHeight=presentation?150:168,padding=presentation?34:46,groups=groupedByRank(base),positions=new Map();
      const maxCount=Math.max(1,...[...groups.values()].map(list=>list.length)),rowStep=presentation?176:188,columnStep=presentation?246:282;
      if(profile==='tree-right'||profile==='tree-down'||profile==='dendrogram'){
        const treeRow=profile==='dendrogram'?Math.round(rowStep*1.25):rowStep;[...groups.entries()].forEach(([rank,list])=>list.forEach((n,i)=>{if(direction==='DOWN'){positions.set(n.node_id,{x:padding+(i-(list.length-1)/2)*columnStep,y:padding+rank*(treeRow+nodeHeight)})}else{positions.set(n.node_id,{x:padding+rank*(columnStep+nodeWidth),y:padding+(i-(list.length-1)/2)*treeRow})}}));
        return layoutRecord(profile,direction,profile==='dendrogram'?'dendrogram':'tree',nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding});
      }
      if(profile==='indented'){
        const ordered=[...groups.entries()].sort((a,b)=>a[0]-b[0]).flatMap(([,list])=>list),indentStep=profile==='indented'?Math.round(nodeWidth*.42):0;ordered.forEach((n,i)=>positions.set(n.node_id,{x:(base.ranks.get(n.node_id)||0)*indentStep,y:i*(nodeHeight+12)}));
        return layoutRecord(profile,'RIGHT','indented',nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding,showGuides:false});
      }
      if(profile==='radial'||profile==='radial-tree'||profile==='concentric'||profile==='circular'){
        const cx=0,cy=0,ringGroups=profile==='circular'?new Map([[0,nodes.slice()]]):groups;ringGroups.forEach((list,rank)=>{const radius=profile==='circular'?260:(rank===0&&list.length===1?0:Math.max(130,rank*190));list.forEach((n,i)=>{const angle=(Math.PI*2*i/Math.max(1,list.length))-(Math.PI/2)+(rank%2)*0.18;positions.set(n.node_id,{x:cx+Math.cos(angle)*radius,y:cy+Math.sin(angle)*radius})})});
        return layoutRecord(profile,'RIGHT',profile,nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding,showGuides:false});
      }
      if(profile==='grid'){
        const columns=Math.max(3,Math.ceil(Math.sqrt(nodes.length)));nodes.slice().sort((a,b)=>a.node_id.localeCompare(b.node_id)).forEach((n,i)=>positions.set(n.node_id,{x:(i%columns)*(nodeWidth+32),y:Math.floor(i/columns)*(nodeHeight+32)}));
        return layoutRecord(profile,'RIGHT','grid',nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding,showGuides:false});
      }
      if(profile==='random'){
        const hash=value=>{let h=2166136261;for(const ch of String(value)){h^=ch.charCodeAt(0);h=Math.imul(h,16777619)}return (h>>>0)/4294967296};const columns=Math.max(3,Math.ceil(Math.sqrt(nodes.length)));nodes.slice().sort((a,b)=>a.node_id.localeCompare(b.node_id)).forEach((n,i)=>{const jitterX=(hash(n.node_id)-.5)*nodeWidth*1.8,jitterY=(hash(n.node_id+'y')-.5)*nodeHeight*1.8;positions.set(n.node_id,{x:(i%columns)*(nodeWidth+70)+jitterX,y:Math.floor(i/columns)*(nodeHeight+70)+jitterY})});
        return layoutRecord(profile,'RIGHT','random-seeded',nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding,showGuides:false});
      }
      if(profile==='compact-box'||profile==='combo'){
        const laneGroups=new Map();nodes.forEach(n=>{const key=profile==='combo'?String(n.lane||n.layer||'default'):String(base.ranks.get(n.node_id)||0);(laneGroups.get(key)||laneGroups.set(key,[]).get(key)).push(n)});let groupIndex=0;laneGroups.forEach(list=>{const columns=Math.max(1,Math.ceil(Math.sqrt(list.length)));list.forEach((n,i)=>positions.set(n.node_id,{x:groupIndex*(columns*(nodeWidth+18)+80)+(i%columns)*(nodeWidth+18),y:Math.floor(i/columns)*(nodeHeight+18)}));groupIndex++});
        return layoutRecord(profile,'RIGHT',profile,nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding,showGuides:false});
      }
      if(profile==='snake'||profile==='fishbone'||profile==='mindmap'){
        [...groups.entries()].forEach(([rank,list])=>{
          const ordered=profile==='snake'&&rank%2?list.slice().reverse():list.slice();
          ordered.forEach((n,i)=>{let x=rank*(columnStep+nodeWidth),y=(i-(ordered.length-1)/2)*(profile==='fishbone'?nodeHeight+42:rowStep);if(profile==='mindmap'){if(rank===0){x=0;y=0}else{const upper=i%2===0;y=(Math.ceil(i/2))*(nodeHeight+38)*(upper?-1:1);x=rank*(columnStep+nodeWidth)}}positions.set(n.node_id,{x,y})});
        });
        return layoutRecord(profile,'RIGHT',profile,nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding,showGuides:false});
      }
      if(['force','fruchterman','forceatlas2','force3d','mds'].includes(profile)){
        const seed=new Map(base.positions),items=nodes.map(n=>({id:n.node_id,x:seed.get(n.node_id)?.x||0,y:seed.get(n.node_id)?.y||0})),links=hardEdgesFor(nodes,edges).map(e=>({source:e.source,target:e.target})),distance=profile==='mds'?(presentation?150:190):profile==='fruchterman'?(presentation?210:270):profile==='forceatlas2'?(presentation?250:320):(presentation?180:230),charge=profile==='forceatlas2'?(presentation?-520:-760):profile==='fruchterman'?(presentation?-300:-420):(presentation?-360:-520);
        const d3=window.d3;if(d3?.forceSimulation&&d3.forceLink){const sim=d3.forceSimulation(items).force('link',d3.forceLink(links).id(d=>d.id).distance(distance).strength(profile==='mds'?0.9:0.72)).force('charge',d3.forceManyBody().strength(charge)).force('collide',d3.forceCollide(Math.max(nodeWidth,nodeHeight)*.62)).force('center',d3.forceCenter(0,0)).stop();for(let i=0;i<(profile==='force3d'?160:120);i++)sim.tick();items.forEach(n=>positions.set(n.id,{x:n.x,y:n.y}))}else{items.forEach(n=>positions.set(n.id,{x:n.x,y:n.y}))}
        return layoutRecord(profile,'RIGHT',d3?.forceSimulation?`d3-${profile}`:`${profile}-fallback`,nodes,edges,base,positions,signature,{nodeWidth,nodeHeight,padding,showGuides:false});
      }
      return null;
    }
    async function computeGraphLayout(nodes,edges,profileOverride){
      const profile=profileOverride||selectedStructure(),fallback=computeLayeredLayout(nodes,edges),presentation=document.body.classList.contains('presentation-mode');
      const direction=profile.endsWith('-down')||profile==='tree-down'?'DOWN':'RIGHT',signature=layoutSignature(profile,direction,nodes,edges);
      if(profile==='webgl3d')return {...fallback,engine:'webgl',layoutProfile:'webgl3d',direction:'RIGHT',signature,showGuides:false};
      if(profile==='manual'&&S.currentLayout&&S.currentLayout.signature===signature&&S.currentLayout.basePositions){
        const positions=new Map(S.currentLayout.basePositions);S.manualPositions.forEach((p,id)=>{if(positions.has(id))positions.set(id,{x:p.x,y:p.y})});
        return {...S.currentLayout,positions,layers:layersFromPositions(nodes,positions,S.currentLayout.nodeWidth,direction,S.currentLayout.nodeHeight),engine:'manual',layoutProfile:'manual'};
      }
      const ElkConstructor=window.ELK;if(typeof ElkConstructor!=='function')return {...fallback,engine:'fallback',layoutProfile:profile,signature};
      if(profile==='manual'){
        const base=await computeGraphLayout(nodes,edges,'elk-right'),positions=new Map(base.positions);S.manualPositions.forEach((p,id)=>{if(positions.has(id))positions.set(id,{x:p.x,y:p.y})});
        return {...base,positions,layers:layersFromPositions(nodes,positions,base.nodeWidth,'RIGHT',base.nodeHeight),engine:'manual',layoutProfile:'manual',signature,basePositions:new Map(base.positions)};
      }
      const alternative=computeAlternativeLayout(profile,direction,nodes,edges,fallback,signature,presentation);if(alternative)return alternative;
      if(profile.startsWith('dagre-'))return {...fallback,engine:'fallback',layoutProfile:profile,direction,signature,showGuides:true};
      const NODE_W=presentation?210:238,NODE_H=presentation?150:168,padding=presentation?34:46;
      const hardEdges=edges.filter(e=>e.edge_type==='HARD_DEPENDENCY'&&nodes.some(n=>n.node_id===e.source)&&nodes.some(n=>n.node_id===e.target));
      const edgeIds=hardEdges.map((e,i)=>({edge:e,id:`hard-${i}-${e.source}-${e.target}`}));
      const spacious=profile==='elk-spacious',compact=profile==='elk-compact';
      const graph={id:'construction-dag',layoutOptions:{'elk.algorithm':'layered','elk.direction':direction,'elk.edgeRouting':compact?'POLYLINE':'ORTHOGONAL','elk.layered.spacing.nodeNodeBetweenLayers':presentation?(spacious?'72':'42'):(spacious?'118':compact?'42':'72'),'elk.spacing.nodeNode':presentation?(spacious?'34':'18'):(spacious?'58':compact?'18':'34'),'elk.layered.nodePlacement.strategy':'NETWORK_SIMPLEX','elk.layered.crossingMinimization.strategy':'LAYER_SWEEP','elk.layered.considerModelOrder.strategy':'NODES_AND_EDGES','elk.layered.nodePlacement.bk.fixedAlignment':'BALANCED'},children:nodes.map(n=>({id:n.node_id,width:NODE_W,height:NODE_H})),edges:edgeIds.map(x=>({id:x.id,sources:[x.edge.source],targets:[x.edge.target]}))};
      try{
        S.elk=S.elk||new ElkConstructor();
        const result=await S.elk.layout(graph),positions=new Map((result.children||[]).map(n=>[n.id,{x:Number(n.x||0)+padding,y:Number(n.y||0)+padding}]));
        if(positions.size!==nodes.length)throw new Error('ELK returned an incomplete node layout');
        const edgeRoutes=new Map();(result.edges||[]).forEach(re=>{const match=edgeIds.find(x=>x.id===re.id);if(match)edgeRoutes.set(hardEdges.indexOf(match.edge),re.sections||[])});
        const layers=layersFromPositions(nodes,positions,NODE_W,direction,NODE_H),maxX=Math.max(...[...positions.values()].map(p=>p.x+NODE_W),NODE_W)+padding,maxY=Math.max(...[...positions.values()].map(p=>p.y+NODE_H),NODE_H)+padding;
        return {positions,ranks:new Map(),layers,width:Math.max(maxX,720),height:Math.max(maxY,650),nodeWidth:NODE_W,nodeHeight:NODE_H,columnStep:0,rowStep:0,left:padding,top:padding,cycleDetected:fallback.cycleDetected,engine:'elk',layoutProfile:profile,direction,signature,basePositions:new Map(positions),edgeRoutes,edgeOffset:{x:padding,y:padding},hardEdgeIndex:new Map(hardEdges.map((e,i)=>[`${e.source}→${e.target}`,i]))};
      }catch(error){
        console.warn('ELK layout failed; using deterministic fallback',error);return {...fallback,engine:'fallback',layoutProfile:profile,signature,layoutError:String(error&&error.message||error)};
      }
    }
    function webglNodeColor(node){const status=String(node.status||'').toUpperCase();if(status==='BLOCKED')return '#ff756f';if(status==='ACTIVE')return '#55e6cf';if(status==='ACCEPTANCE'||status==='PENDING_REVIEW')return '#f1c56b';if(status==='STALE_PRIVATE_PROJECTION')return '#8d9aa3';if(node.critical)return '#f1c56b';return '#5f9ee5'}
    function webglLinkColor(link){if(link.blocking)return '#ff756f';if(link.critical)return '#f1c56b';if(link.soft)return '#718797';if(link.satisfied)return '#4b9b88';return '#4ba7a2'}
    function webglHexColor(value){const raw=String(value||'#5f9ee5').replace('#','');const hex=raw.length===3?raw.split('').map(x=>x+x).join(''):raw;return [parseInt(hex.slice(0,2),16)/255,parseInt(hex.slice(2,4),16)/255,parseInt(hex.slice(4,6),16)/255]}
    function webglGraphData(nodes,edges,visibleIds,showSoft,critical){
      const base=computeLayeredLayout(nodes,edges),groups=base.layers,maxRank=Math.max(0,...base.ranks.values());
      const dataNodes=nodes.filter(n=>visibleIds.has(n.node_id)).map(n=>{const rank=Number(base.ranks.get(n.node_id)||0),group=groups.get(rank)||[],index=Math.max(0,group.findIndex(item=>item.node_id===n.node_id)),z=Math.sin(rank*1.25+index*.85)*118+((index%3)-1)*72;return {id:n.node_id,name:localized(n,'title',n.node_id),lane:localized(n,'lane',n.lane||tr('laneDefault')),rawStatus:n.status,status:valueLabel(n.status),completion:Number(n.completion_percent)||0,critical:critical.has(n.node_id),color:webglNodeColor({...n,critical:critical.has(n.node_id)}),x:(rank-maxRank/2)*420,y:(index-(group.length-1)/2)*210,z,val:Math.max(1,4+(Number(n.completion_percent)||0)/18)}});
      const dataLinks=edges.filter(e=>visibleIds.has(e.source)&&visibleIds.has(e.target)&&(showSoft||e.edge_type!=='SOFT_DEPENDENCY')).map(e=>({source:e.source,target:e.target,soft:e.edge_type==='SOFT_DEPENDENCY',blocking:e.edge_status==='BLOCKING',satisfied:e.edge_status==='SATISFIED',critical:e.edge_type==='HARD_DEPENDENCY'&&critical.has(e.source)&&critical.has(e.target),color:webglLinkColor({blocking:e.edge_status==='BLOCKING',critical:e.edge_type==='HARD_DEPENDENCY'&&critical.has(e.source)&&critical.has(e.target),soft:e.edge_type==='SOFT_DEPENDENCY',satisfied:e.edge_status==='SATISFIED'})}));return {nodes:dataNodes,links:dataLinks}
    }
    function webglProgram(gl,vertexSource,fragmentSource){const compile=(type,source)=>{const shader=gl.createShader(type);gl.shaderSource(shader,source);gl.compileShader(shader);if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(shader)||'WebGL shader compile failed');return shader};const program=gl.createProgram();gl.attachShader(program,compile(gl.VERTEX_SHADER,vertexSource));gl.attachShader(program,compile(gl.FRAGMENT_SHADER,fragmentSource));gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(program)||'WebGL program link failed');return program}
    function webglBuffer(gl,data){const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(data),gl.STATIC_DRAW);return buffer}
    function webglM4Multiply(a,b){const out=new Float32Array(16);for(let column=0;column<4;column++)for(let row=0;row<4;row++)out[column*4+row]=a[row]*b[column*4]+a[4+row]*b[column*4+1]+a[8+row]*b[column*4+2]+a[12+row]*b[column*4+3];return out}
    function webglPerspective(fov,aspect,near,far){const f=1/Math.tan(fov/2),nf=1/(near-far);return new Float32Array([f/aspect,0,0,0,0,f,0,0,0,0,(far+near)*nf,-1,0,0,2*far*near*nf,0])}
    function webglNormalize(v){const length=Math.hypot(v[0],v[1],v[2])||1;return [v[0]/length,v[1]/length,v[2]/length]}
    function webglCross(a,b){return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]}
    function webglDot(a,b){return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]}
    function webglLookAt(eye,target){const z=webglNormalize([eye[0]-target[0],eye[1]-target[1],eye[2]-target[2]]),x=webglNormalize(webglCross([0,1,0],z)),y=webglCross(z,x);return new Float32Array([x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,-webglDot(x,eye),-webglDot(y,eye),-webglDot(z,eye),1])}
    function webglCamera(wgl){const cosPitch=Math.cos(wgl.pitch),eye=[wgl.target[0]+wgl.distance*cosPitch*Math.sin(wgl.yaw),wgl.target[1]+wgl.distance*Math.sin(wgl.pitch),wgl.target[2]+wgl.distance*cosPitch*Math.cos(wgl.yaw)],view=webglLookAt(eye,wgl.target),projection=webglPerspective(Math.PI/3,Math.max(.1,wgl.width/Math.max(1,wgl.height)),1,6500);return {eye,matrix:webglM4Multiply(projection,view)}}
    function webglProject(wgl,point){const m=wgl.matrix,x=m[0]*point.x+m[4]*point.y+m[8]*point.z+m[12],y=m[1]*point.x+m[5]*point.y+m[9]*point.z+m[13],clipZ=m[2]*point.x+m[6]*point.y+m[10]*point.z+m[14],clipW=m[3]*point.x+m[7]*point.y+m[11]*point.z+m[15];if(clipW<=.01)return null;const nx=x/clipW,ny=y/clipW,nz=clipZ/clipW;return {x:(nx*.5+.5)*wgl.width,y:(1-(ny*.5+.5))*wgl.height,z:nz,visible:nx>-1.2&&nx<1.2&&ny>-1.2&&ny<1.2&&nz>-1&&nz<1}}
    function webglResize(wgl){const rect=wgl.canvas.getBoundingClientRect(),dpr=Math.min(2,window.devicePixelRatio||1),width=Math.max(1,Math.floor(rect.width*dpr)),height=Math.max(1,Math.floor(rect.height*dpr));if(wgl.canvas.width!==width||wgl.canvas.height!==height){wgl.canvas.width=width;wgl.canvas.height=height;wgl.gl.viewport(0,0,width,height)}wgl.width=Math.max(1,rect.width);wgl.height=Math.max(1,rect.height)}
    function webglBuildBuffers(wgl){const nodePositions=[],nodeNormals=[],nodeColors=[],edgePositions=[],edgeColors=[],faces=[{normal:[0,0,1],verts:[[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]]},{normal:[0,0,-1],verts:[[1,-1,-1],[-1,-1,-1],[-1,1,-1],[1,1,-1]]},{normal:[1,0,0],verts:[[1,-1,1],[1,-1,-1],[1,1,-1],[1,1,1]]},{normal:[-1,0,0],verts:[[-1,-1,-1],[-1,-1,1],[-1,1,1],[-1,1,-1]]},{normal:[0,1,0],verts:[[-1,1,1],[1,1,1],[1,1,-1],[-1,1,-1]]},{normal:[0,-1,0],verts:[[-1,-1,-1],[1,-1,-1],[1,-1,1],[-1,-1,1]]}],triangles=[0,1,2,0,2,3],hx=78,hy=48,hz=28;wgl.data.nodes.forEach(node=>{const color=webglHexColor(node.color);faces.forEach(face=>triangles.forEach(index=>{const vertex=face.verts[index];nodePositions.push(node.x+vertex[0]*hx,node.y+vertex[1]*hy,node.z+vertex[2]*hz);nodeNormals.push(...face.normal);nodeColors.push(...color)}))});wgl.data.links.forEach(link=>{const a=wgl.data.nodes.find(node=>node.id===link.source),b=wgl.data.nodes.find(node=>node.id===link.target);if(!a||!b)return;const color=webglHexColor(link.color);edgePositions.push(a.x,a.y,a.z,b.x,b.y,b.z);edgeColors.push(...color,...color)});const gl=wgl.gl;wgl.nodeBuffers={position:webglBuffer(gl,nodePositions),normal:webglBuffer(gl,nodeNormals),color:webglBuffer(gl,nodeColors)};wgl.edgeBuffers={position:webglBuffer(gl,edgePositions),color:webglBuffer(gl,edgeColors)};wgl.nodeVertexCount=nodePositions.length/3;wgl.edgeVertexCount=edgePositions.length/3}
    function webglUpdateLabels(wgl){const labels=wgl.labels;labels.innerHTML=wgl.data.nodes.map(node=>`<div class="webgl3d-label ${String(node.rawStatus||'').toLowerCase()}" data-id="${esc(node.id)}"><b>${esc(node.id)}</b><strong>${esc(node.name)}</strong><span>${esc(node.status)} · ${esc(node.completion)}%</span></div>`).join('')}
    function webglUpdateOverlay(wgl){const pointById=new Map(wgl.data.nodes.map(node=>[node.id,webglProject(wgl,node)]));wgl.overlay.setAttribute('viewBox',`0 0 ${wgl.width} ${wgl.height}`);wgl.overlay.innerHTML=wgl.data.links.map(link=>{const a=pointById.get(link.source),b=pointById.get(link.target);if(!a||!b||!a.visible||!b.visible)return '';const cls=`${link.soft?'soft ':''}${link.blocking?'blocking ':''}${link.critical?'critical':''}`;return `<line class="${cls}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>`}).join('')}
    function webglFit(wgl){if(!wgl.data?.nodes.length)return;const xs=wgl.data.nodes.map(node=>node.x),ys=wgl.data.nodes.map(node=>node.y),zs=wgl.data.nodes.map(node=>node.z),center=[(Math.min(...xs)+Math.max(...xs))/2,(Math.min(...ys)+Math.max(...ys))/2,(Math.min(...zs)+Math.max(...zs))/2],radius=Math.max(160,Math.hypot(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys),Math.max(...zs)-Math.min(...zs))/2);wgl.target=center;wgl.distance=Math.max(560,radius*2.55);wgl.yaw=-.58;wgl.pitch=.24;wgl.viewInitialized=true}
    function webglPickNode(wgl,event){const rect=wgl.canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;let picked=null,best=34*34;wgl.data.nodes.forEach(node=>{const point=webglProject(wgl,node);if(!point||!point.visible)return;const distance=(point.x-x)**2+(point.y-y)**2;if(distance<best){best=distance;picked=node.id}});if(picked)(window.__CONSTRUCTION_HOOKS__?.selectNode||selectNode)(picked)}
    function webglAttachControls(wgl){const canvas=wgl.canvas;canvas.addEventListener('contextmenu',event=>event.preventDefault());canvas.addEventListener('pointerdown',event=>{if(![0,1,2].includes(event.button))return;event.preventDefault();wgl.drag={x:event.clientX,y:event.clientY,yaw:wgl.yaw,pitch:wgl.pitch,target:wgl.target.slice(),button:event.button,moved:false};canvas.setPointerCapture?.(event.pointerId)});canvas.addEventListener('pointermove',event=>{const drag=wgl.drag;if(!drag)return;const dx=event.clientX-drag.x,dy=event.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3)drag.moved=true;if(drag.button===0){wgl.yaw=drag.yaw+dx*.008;wgl.pitch=Math.max(-1.35,Math.min(1.35,drag.pitch+dy*.008))}else{const scale=wgl.distance*.0022;wgl.target=[drag.target[0]-dx*scale,drag.target[1]+dy*scale,drag.target[2]]}});const finish=event=>{const drag=wgl.drag;wgl.drag=null;canvas.releasePointerCapture?.(event.pointerId);if(drag&&!drag.moved)webglPickNode(wgl,event)};canvas.addEventListener('pointerup',finish);canvas.addEventListener('pointercancel',()=>{wgl.drag=null});canvas.addEventListener('wheel',event=>{event.preventDefault();wgl.distance=Math.max(240,Math.min(7000,wgl.distance*Math.exp(event.deltaY*.001)));},{passive:false})}
    function webglFrame(wgl){if(S.webgl!==wgl)return;webglResize(wgl);const camera=webglCamera(wgl);wgl.matrix=camera.matrix;const gl=wgl.gl;gl.enable(gl.DEPTH_TEST);gl.clearColor(.015,.06,.09,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.useProgram(wgl.edgeProgram);gl.uniformMatrix4fv(wgl.edgeMatrix,false,wgl.matrix);gl.bindBuffer(gl.ARRAY_BUFFER,wgl.edgeBuffers.position);gl.enableVertexAttribArray(wgl.edgePosition);gl.vertexAttribPointer(wgl.edgePosition,3,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,wgl.edgeBuffers.color);gl.enableVertexAttribArray(wgl.edgeColor);gl.vertexAttribPointer(wgl.edgeColor,3,gl.FLOAT,false,0,0);gl.drawArrays(gl.LINES,0,wgl.edgeVertexCount);gl.useProgram(wgl.nodeProgram);gl.uniformMatrix4fv(wgl.nodeMatrix,false,wgl.matrix);gl.bindBuffer(gl.ARRAY_BUFFER,wgl.nodeBuffers.position);gl.enableVertexAttribArray(wgl.nodePosition);gl.vertexAttribPointer(wgl.nodePosition,3,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,wgl.nodeBuffers.normal);gl.enableVertexAttribArray(wgl.nodeNormal);gl.vertexAttribPointer(wgl.nodeNormal,3,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,wgl.nodeBuffers.color);gl.enableVertexAttribArray(wgl.nodeColor);gl.vertexAttribPointer(wgl.nodeColor,3,gl.FLOAT,false,0,0);gl.drawArrays(gl.TRIANGLES,0,wgl.nodeVertexCount);wgl.lastError=gl.getError();webglUpdateOverlay(wgl);wgl.labels.querySelectorAll('.webgl3d-label').forEach(element=>{const node=wgl.data.nodes.find(item=>item.id===element.dataset.id),point=node&&webglProject(wgl,node);if(!point||!point.visible){element.style.opacity='0';return}element.style.opacity='1';element.style.left=point.x+'px';element.style.top=point.y+'px';element.style.transform=`translate(-50%,-50%) scale(${Math.max(.72,Math.min(1.18,1.08-point.z*.18))})`;element.style.zIndex=String(1000-Math.round(point.z*100));element.classList.toggle('selected',node.id===S.selected)});wgl.raf=requestAnimationFrame(()=>webglFrame(wgl))}
    function destroyWebgl3d(){const host=document.querySelector('#webgl3d');if(S.webgl?.raf)cancelAnimationFrame(S.webgl.raf);S.webgl=null;if(host){host.classList.remove('active');host.innerHTML='<div id="webgl3d-status" class="webgl3d-status"></div>'}}
    function renderWebgl3d(nodes,edges,visibleIds,showSoft,critical){const host=document.querySelector('#webgl3d'),status=document.querySelector('#webgl3d-status');if(!host)return false;host.classList.add('active');if(!S.webgl){const canvas=document.createElement('canvas');canvas.id='webgl3d-canvas';canvas.setAttribute('aria-label',tr('webgl3d'));host.insertBefore(canvas,status||null);const overlay=document.createElementNS('http://www.w3.org/2000/svg','svg');overlay.id='webgl3d-overlay';overlay.className.baseVal='webgl3d-overlay';host.appendChild(overlay);const labels=document.createElement('div');labels.id='webgl3d-labels';labels.className='webgl3d-labels';host.appendChild(labels);const gl=canvas.getContext('webgl2',{antialias:true,alpha:false})||canvas.getContext('webgl',{antialias:true,alpha:false});if(!gl){if(status)status.textContent=tr('webglFallback');host.classList.remove('active');return false}try{const nodeProgram=webglProgram(gl,`attribute vec3 a_position;attribute vec3 a_normal;attribute vec3 a_color;uniform mat4 u_matrix;varying vec3 v_normal;varying vec3 v_color;void main(){gl_Position=u_matrix*vec4(a_position,1.0);v_normal=a_normal;v_color=a_color;}`,`precision mediump float;varying vec3 v_normal;varying vec3 v_color;void main(){vec3 light=normalize(vec3(-.35,.55,1.0));float shade=.52+.48*max(dot(normalize(v_normal),light),0.0);gl_FragColor=vec4(v_color*shade,1.0);}`),edgeProgram=webglProgram(gl,`attribute vec3 a_position;attribute vec3 a_color;uniform mat4 u_matrix;varying vec3 v_color;void main(){gl_Position=u_matrix*vec4(a_position,1.0);v_color=a_color;}`,`precision mediump float;varying vec3 v_color;void main(){gl_FragColor=vec4(v_color,1.0);}`);S.webgl={native:true,host,canvas,overlay,labels,gl,nodeProgram,edgeProgram,yaw:-.58,pitch:.24,distance:900,target:[0,0,0],viewInitialized:false,fitRequested:false,raf:0};S.webgl.nodePosition=gl.getAttribLocation(nodeProgram,'a_position');S.webgl.nodeNormal=gl.getAttribLocation(nodeProgram,'a_normal');S.webgl.nodeColor=gl.getAttribLocation(nodeProgram,'a_color');S.webgl.nodeMatrix=gl.getUniformLocation(nodeProgram,'u_matrix');S.webgl.edgePosition=gl.getAttribLocation(edgeProgram,'a_position');S.webgl.edgeColor=gl.getAttribLocation(edgeProgram,'a_color');S.webgl.edgeMatrix=gl.getUniformLocation(edgeProgram,'u_matrix');webglAttachControls(S.webgl)}catch(error){if(status)status.textContent=tr('webglFallback');host.classList.remove('active');canvas.remove();labels.remove();overlay.remove();S.webgl=null;return false}}
      const wgl=S.webgl;wgl.data=webglGraphData(nodes,edges,visibleIds,showSoft,critical);webglBuildBuffers(wgl);webglUpdateLabels(wgl);if(!wgl.viewInitialized||wgl.fitRequested){wgl.fitRequested=false;webglFit(wgl)}if(status)status.textContent=`${tr('webglReadOnly')} · ${wgl.data.nodes.length} ${tr('nodes')} · ${wgl.data.links.length} ${tr('edges')} · 3D geometry ${wgl.nodeVertexCount}/${wgl.edgeVertexCount}${wgl.lastError?` · GL ${wgl.lastError}`:''}`;if(!wgl.raf)wgl.raf=requestAnimationFrame(()=>webglFrame(wgl));return true}
    function nodeHtml(n,critical){
      const tasks=tasksForNode(n),masterTasks=tasks.filter(t=>t.master_controller_step===true),masterSummary=masterTasks.length?` · ${tr('masterSteps')} ${masterTasks.length}`:'',stale=tasks.some(t=>taskEffectiveStatus(t)==='STALE_PRIVATE_PROJECTION');
      const capabilityClass=String(n.capability_state||'').toLowerCase(),statusClass=String(n.status||'').toLowerCase();
      return `<article class="node ${capabilityClass} ${statusClass} ${critical?'critical':''} ${stale?'stale':''}" data-id="${esc(n.node_id)}">
        <div class="node-head"><div><div class="node-id">${esc(n.node_id.split('_')[0])} · ${esc(localized(n,'lane',n.lane||tr('laneDefault')))}</div><div class="node-title">${esc(localized(n,'title'))}</div></div><span class="badge ${esc(n.status)}">${esc(valueLabel(n.status))}</span></div>
        <div class="progress"><i style="width:${Number(n.completion_percent)||0}%"></i></div>
        <div class="axes"><span>${esc(tr('capability'))} <b>${esc(valueLabel(n.capability_state))}</b></span><span>${esc(tr('activity'))} <b>${esc(valueLabel(n.activity_state))}</b></span><span>${esc(tr('gate'))} <b>${esc(valueLabel(n.gate_state))}</b></span><span>${esc(tr('acceptance'))} <b>${esc(valueLabel(n.project_acceptance_state))}</b></span></div>
        <div class="node-subline">${esc(tr('effect'))} ${esc(valueLabel(n.effect_state))} · ${esc(tr('subgates'))} ${(n.subgates||[]).length} · ${esc(localized(n,'layer',n.layer||'—'))}</div>
        <div class="counts"><span><b>${n.task_counts.active}</b>${esc(tr('active'))}</span><span><b>${n.task_counts.queued}</b>${esc(tr('queued'))}</span><span><b>${n.task_counts.pending_review}</b>${esc(tr('pendingReview'))}</span><span><b>${n.blocker_count}</b>${esc(tr('blocked'))}</span></div>
        <div class="task-summary">${esc(tr('atomicTasks'))} ${tasks.length}${esc(masterSummary)}</div>
        ${tasks.slice(0,2).map(taskPreviewHtml).join('')}</article>`;
    }
    function startNodeDrag(event,element){
      if(!S.editMode||(event.button!==0&&event.pointerType!=='touch')||selectedStructure()!=='manual')return;
      event.preventDefault();event.stopPropagation();const id=element.dataset.id,p=S.currentLayout?.positions.get(id);if(!p)return;
      ensureManualHistory();const startX=event.clientX,startY=event.clientY,originX=p.x,originY=p.y,grid=8;let moved=false,frame=0;element.classList.add('dragging');element.setPointerCapture?.(event.pointerId);
      const move=moveEvent=>{const rawX=Math.max(8,originX+(moveEvent.clientX-startX)/S.scale),rawY=Math.max(8,originY+(moveEvent.clientY-startY)/S.scale),next={x:S.manualSnap?Math.round(rawX/grid)*grid:rawX,y:S.manualSnap?Math.round(rawY/grid)*grid:rawY};moved=true;S.manualPositions.set(id,next);if(S.currentLayout?.positions)S.currentLayout.positions.set(id,next);element.style.left=next.x+'px';element.style.top=next.y+'px';if(!frame)frame=requestAnimationFrame(()=>{frame=0;renderGraph()})};
      const finish=()=>{if(frame)cancelAnimationFrame(frame);element.classList.remove('dragging');element.releasePointerCapture?.(event.pointerId);window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',finish);window.removeEventListener('pointercancel',finish);if(moved){commitManualHistory();saveLayoutDraft();renderGraph()}};
      window.addEventListener('pointermove',move);window.addEventListener('pointerup',finish,{once:true});window.addEventListener('pointercancel',finish,{once:true});
    }
    async function renderGraph(){
      if(!S.bundle)return;
      const token=++S.layoutToken,g=S.bundle.status.flow_graph,nodes=g.nodes||[],edges=g.edges||[],layout=await computeGraphLayout(nodes,edges);
      if(token!==S.layoutToken||!S.bundle)return;
      S.currentLayout=layout;
      const filter=document.querySelector('#status-filter').value,visible=filter?nodes.filter(n=>n.status===filter):nodes;
      const visibleIds=new Set(visible.map(n=>n.node_id)),pos=layout.positions,critical=new Set(g.critical_path_highlight||[]);
      const showSoft=document.querySelector('#show-soft')?.checked||false,softEdges=edges.filter(e=>e.edge_type==='SOFT_DEPENDENCY').length;
      const softCount=document.querySelector('#soft-count');if(softCount)softCount.textContent=tr('softCount',{n:softEdges});
      const isWebgl=selectedStructure()==='webgl3d',webglReady=isWebgl?renderWebgl3d(nodes,edges,visibleIds,showSoft,critical):false,true3d=isWebgl&&webglReady;if(!isWebgl&&S.webgl)destroyWebgl3d();
      const graphMode=document.querySelector('#graph-mode');if(graphMode){graphMode.textContent=true3d?tr('webgl3d'):isWebgl?tr('webglFallback'):layoutModeLabel(layout);graphMode.classList.toggle('warning',layout.cycleDetected||isWebgl&&!true3d)}
      const webglHost=document.querySelector('#webgl3d');if(webglHost)webglHost.classList.toggle('active',true3d);const canvas=document.querySelector('#canvas'),viewport=document.querySelector('#viewport');canvas.style.display=true3d?'none':'block';canvas.style.width=layout.width+'px';canvas.style.height=layout.height+'px';canvas.style.transform=`scale(${S.scale})`;
      const guides=layout.showGuides===false?'':[...layout.layers.entries()].map(([rank,list])=>{
        const visibleLayer=list.filter(n=>visibleIds.has(n.node_id)),members=visibleLayer.map(n=>pos.get(n.node_id)).filter(Boolean);if(!members.length)return '';
        const down=layout.direction==='DOWN',left=down?Math.min(...members.map(p=>p.x))-18:Math.min(...members.map(p=>p.x))-18,right=down?Math.max(...members.map(p=>p.x))+layout.nodeWidth+18:Math.max(...members.map(p=>p.x))+layout.nodeWidth+18;
        const top=down?Math.min(...members.map(p=>p.y))-18:18,height=down?Math.max(...members.map(p=>p.y))+layout.nodeHeight+18-top:layout.height-18;
        return `<div class="lane-guide" style="left:${left}px;top:${top}px;width:${right-left}px;height:${height}px"><strong>${esc(tr('dagLayer'))} ${rank+1}</strong><span>${visibleLayer.length} ${esc(tr('nodes'))}</span></div>`;
      }).join('');
      document.querySelector('#lane-guides').innerHTML=guides;
      document.querySelector('#nodes').innerHTML=visible.map(n=>nodeHtml(n,critical.has(n.node_id))).join('');
      document.querySelectorAll('.node').forEach(el=>{const p=pos.get(el.dataset.id);if(!p)return;const depth=Number(layout.depths?.get(el.dataset.id)||0);el.style.left=p.x+'px';el.style.top=p.y+'px';el.style.setProperty('--depth',`${depth}px`);el.style.zIndex=String(2+Math.round(depth/8));el.onclick=()=>selectNode(el.dataset.id);if(S.editMode)el.onpointerdown=event=>startNodeDrag(event,el)});
      const svg=document.querySelector('#edges');svg.setAttribute('width',layout.width);svg.setAttribute('height',layout.height);
      const defs=`<defs><marker id="arrow-normal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#4c8d83"/></marker><marker id="arrow-blocking" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#ff756f"/></marker><marker id="arrow-critical" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#f1c56b"/></marker></defs>`;
      let softOrdinal=0;
      const paths=edges.filter(e=>visibleIds.has(e.source)&&visibleIds.has(e.target)&&(showSoft||e.edge_type!=='SOFT_DEPENDENCY')).map(e=>{const a=pos.get(e.source),b=pos.get(e.target);if(!a||!b)return '';
        const isSoft=e.edge_type==='SOFT_DEPENDENCY',down=layout.direction==='DOWN',x1=down?a.x+layout.nodeWidth/2:a.x+layout.nodeWidth,y1=down?a.y+layout.nodeHeight:a.y+layout.nodeHeight/2,x2=down?b.x+layout.nodeWidth/2:b.x,y2=down?b.y:b.y+layout.nodeHeight/2,forward=down?y2>y1+8:x2>x1+8;
        const bend=Math.round((down?y1:y1)+(down?y2-y1:x2-x1)/2),channel=down?Math.min(a.y,b.y)-28:Math.min(a.x,b.x)-28;
        const softChannel=isSoft?(down?Math.min(layout.width-18,Math.max(a.x+layout.nodeWidth,b.x+layout.nodeWidth)+18+(softOrdinal++%4)*12):Math.min(layout.height-18,Math.max(a.y+layout.nodeHeight,b.y+layout.nodeHeight)+18+(softOrdinal++%4)*12)):0;
        const fallbackPath=isSoft&&down&&forward?`M${x1},${y1} V${y1+12} H${softChannel} V${y2-12} H${x2} V${y2}`:isSoft&&forward?`M${x1},${y1} H${x1+12} V${softChannel} H${x2-12} V${y2} H${x2}`:isSoft&&down?`M${x1},${y1} V${channel} H${x2} V${y2}`:isSoft?`M${x1},${y1} H${channel} V${y2} H${x2}`:down&&forward?`M${x1},${y1} V${bend} H${x2} V${y2}`:down?`M${x1},${y1} V${channel} H${x2} V${y2}`:Math.abs(y1-y2)<2&&forward?`M${x1},${y1} H${x2}`:forward?`M${x1},${y1} H${bend} V${y2} H${x2}`:`M${x1},${y1} H${channel} V${y2} H${x2}`;
        const hardIndex=layout.hardEdgeIndex?.get(`${e.source}→${e.target}`),elkPath=!isSoft&&hardIndex!==undefined?elkSectionsToPath(layout.edgeRoutes?.get(hardIndex),layout.edgeOffset?.x||0,layout.edgeOffset?.y||0):'';
        const routedPath=!isSoft?layout.edgePaths?.get(`${e.source}→${e.target}`)||'':'';
        const path=isSoft?fallbackPath:(elkPath||routedPath||fallbackPath);
        const isCritical=e.edge_type==='HARD_DEPENDENCY'&&critical.has(e.source)&&critical.has(e.target),isBlocking=e.edge_status==='BLOCKING';
        const cls=`edge ${e.edge_type==='SOFT_DEPENDENCY'?'soft':''} ${isBlocking?'blocking':e.edge_status==='SATISFIED'?'satisfied':''} ${isCritical?'critical':''}`;
        const marker=isBlocking?'arrow-blocking':isCritical?'arrow-critical':'arrow-normal';
        return `<path class="${cls}" d="${path}" marker-end="url(#${marker})"/>`}).join('');
      svg.innerHTML=defs+paths;
      const firstVisible=visible.find(n=>n.status==='BLOCKED')||visible.find(n=>critical.has(n.node_id))||visible[0];
      if(S.selected&&visibleIds.has(S.selected))selectNode(S.selected,false);else if(firstVisible)selectNode(firstVisible.node_id,false);
    }
    function selectNode(id,rerender=true){S.selected=id;const n=(S.bundle.status.flow_graph.nodes||[]).find(x=>x.node_id===id);const st=stageMap().get(id);if(!n||!st)return;const tasks=tasksForNode(n);
      document.querySelector('#inspector').innerHTML=`<h2>${esc(localized(n,'title'))}</h2><div class="kv"><b>${esc(id)}</b><br>${esc(tr('capability'))}=${esc(valueLabel(n.capability_state))} · ${esc(tr('activity'))}=${esc(valueLabel(n.activity_state))} · ${esc(tr('gate'))}=${esc(valueLabel(n.gate_state))}<br>${esc(tr('effect'))}=${esc(valueLabel(n.effect_state))} · ${esc(tr('acceptance'))}=${esc(valueLabel(n.project_acceptance_state))}<br>${esc(tr('estimateNotice',{n:n.completion_percent}))}</div>
        <h3>${esc(tr('target'))}</h3><div class="kv">${esc(localized(st,'target'))}</div><h3>${esc(tr('taskDetails'))} · ${tasks.length}</h3>${tasks.length?tasks.map(taskDetailHtml).join(''):`<div class="kv">${esc(tr('noRegisteredTasks'))}</div>`}
        <h3>${esc(tr('dependenciesBlockers'))}</h3><div class="kv">${esc(tr('hardDependencies'))}：${esc((st.hard_dependencies||[]).join('、')||tr('none'))}<br>${esc(tr('softDependencies'))}：${esc((st.soft_dependencies||[]).join('、')||tr('none'))}<br>${esc(tr('blocker'))}：${esc(JSON.stringify(localized(st,'blockers',[])||[]))}</div>
        <h3>${esc(tr('phaseSubgates'))}</h3>${(n.subgates||[]).length?(n.subgates||[]).map(g=>`<div class="subgate"><b>${esc(g.subgate_id)}</b><br><span class="kv">${esc(JSON.stringify(g))}</span></div>`).join(''):`<div class="kv">${esc(tr('none'))}</div>`}
        <h3>${esc(tr('capabilityTracks'))}</h3>${(n.capability_tracks||[]).length?(n.capability_tracks||[]).map(g=>`<div class="subgate"><b>${esc(g.track_id)}</b><br><span class="kv">${esc(JSON.stringify(g))}</span></div>`).join(''):`<div class="kv">${esc(tr('none'))}</div>`}
        <h3>${esc(tr('legalNext'))}</h3><div class="kv">${esc(localized(st,'legal_next_task'))}</div><h3>${esc(tr('deliveryConsumer'))}</h3><div class="kv">${esc(localized(st,'output_handoff'))}<br>→ ${esc(localized(st,'direct_consumer'))}</div>
        <h3>${esc(tr('claimCeiling'))}</h3><div class="kv">${esc(localized(st,'claim_ceiling'))}</div>`;
      if(rerender)document.querySelectorAll('.node').forEach(el=>el.style.outline=el.dataset.id===id?'3px solid #f1c56b':'none');
    }
    function renderMachine(){const m=S.bundle.status.workflow_state_machine||{},ts=m.ordinary_transitions||[],raw=(S.lang==='en'?(m.typed_side_states_en||m.typed_side_states):(m.typed_side_states||{})),axes=S.bundle.status.orthogonal_state_model||{},subs=S.bundle.status.typed_subgates||[],tracks=S.bundle.status.capability_tracks||[],scenario=localized(S.bundle.status,'scenario_summary','');
      const ss=Array.isArray(raw)?raw.map(x=>[x,'']):Object.entries(raw);
      document.querySelector('#machine').innerHTML=`<b>${esc(tr('legalTransitions'))}：</b> ${ts.map((t,i)=>`${i?'<span class="arrow">→</span>':''}${esc(valueLabel(t.from))} → ${esc(valueLabel(t.to))}`).join(' ')}
      <br><b>${esc(tr('orthogonalState'))}：</b> ${Object.keys(axes).map(x=>`<span class="badge">${esc(valueLabel(x))}</span>`).join(' ')}；${esc(tr('readOnlyProjection'))}。
      <br><b>${esc(tr('typedSideStates'))}：</b> ${ss.length?ss.map(([k,v])=>`<span class="badge ${esc(k)}" title="${esc(v)}">${esc(valueLabel(k))}${v?'：'+esc(v):''}</span>`).join(' '):esc(tr('none'))}
      <br><b>${esc(tr('phaseSubgates'))}：</b>${subs.length}　<b>${esc(tr('capabilityTracks'))}：</b>${tracks.length}　<b>${esc(tr('overallPercentDisabled'))}</b>
      <br><b>${esc(tr('blocking'))}：</b>${esc(m.blocking_transitions?.event||'BLOCKER_OPENED')} ${esc(tr('enters'))} BLOCKED；${esc(m.blocking_transitions?.return_event||'BLOCKER_CLOSED')}${esc(tr('after'))}。${esc(tr('frontendNoWrite'))}
      ${scenario?`<br><b>${esc(tr('scenarioRule'))}：</b>${esc(scenario)}`:''}`}
    function renderTable(){
      const q=document.querySelector('#search').value.toLowerCase(),onlyTasks=document.querySelector('#only-tasks').checked,onlyBlocked=document.querySelector('#only-blocked').checked;
      const stages=S.bundle.status.stages||[];document.querySelector('#stage-rows').innerHTML=stages.filter(s=>{const tasks=tasksForStage(s);
        const hay=[s.stage_id,s.title,s.legal_next_task,...tasks.flatMap(t=>[t.task_id,t.display_title,t.member_id])].join(' ').toLowerCase();return(!q||hay.includes(q))&&(!onlyTasks||tasks.length)&&(!onlyBlocked||(s.blockers||[]).length)}).map(s=>{
        const tasks=tasksForStage(s);
        const wheels=(S.lang==='en'?(s.mature_wheel_candidates_en??s.mature_wheel_candidates):(s.mature_wheel_candidates||[]))||[];
        return `<tr><td><b>${esc(localized(s,'title'))}</b><br><code>${esc(s.stage_id)}</code><br><span class="kv">${esc(localized(s,'layer',s.layer||'—'))}</span></td><td><span class="badge ${esc(s.status)}">${esc(valueLabel(s.status))}</span></td><td>${esc(s.completion_percent)}%<br><span class="kv">${esc(S.lang==='en'?'estimated':'估算')}</span></td>
          <td>${tasks.length?tasks.map(t=>`<div class="task-card"><b>${esc(taskTitle(t))}</b><br><span class="kv">${esc(valueLabel(taskRawStatus(t)))} · ${esc(taskClaimLabel(t))} · ${esc(t.member_id||tr('unbound'))}</span></div>`).join(''):'—'}</td>
          <td>${esc((s.blockers||[]).length)}<div class="row-detail">${esc(JSON.stringify(s.blockers||[]))}</div></td><td><b>${esc(localized(s,'adoption_mode'))}</b><div class="row-detail">${esc(wheels.join('、'))}</div></td>
          <td class="row-detail">${esc(localized(s,'legal_next_task'))}</td><td class="row-detail">${esc(localized(s,'direct_consumer'))}</td></tr>`}).join('')||`<tr><td colspan="8" class="empty">${esc(tr('noMatchingStages'))}</td></tr>`}
    function renderEvents(){const rows=S.bundle.events||[];document.querySelector('#events').innerHTML=rows.length?rows.map(e=>`<div class="event"><code>#${esc(e.seq)}</code><div><b>${esc(e.stage_id)}</b><br><span class="kv">${esc(e.occurred_at)}</span></div><div><b>${esc(eventTypeLabel(e.event_type))}</b><div class="kv">${eventPayloadHtml(e.current)}</div></div></div>`).join(''):`<div class="empty">${esc(tr('noEvents'))}</div>`}
    function populateFilters(){const statuses=[...new Set((S.bundle.status.stages||[]).map(x=>x.status))].sort(),select=document.querySelector('#status-filter'),old=select.value;
      select.innerHTML=`<option value="">${esc(tr('allStatuses'))}</option>`+statuses.map(x=>`<option value="${esc(x)}">${esc(valueLabel(x))}</option>`).join('');select.value=old}
    function renderAll(){renderSummary();populateFilters();(window.__CONSTRUCTION_HOOKS__?.renderGraph||renderGraph)();renderMachine();renderTable();renderEvents();
      document.querySelector('#updated').textContent=tr('updatePrefix')+(S.bundle.status.generated_at||'—');const ver=document.querySelector('#verify');ver.textContent=S.bundle.verified?tr('dataVerified'):tr('lastVerifiedView');ver.className='pill '+(S.bundle.verified?'ok':'bad');
      const warn=document.querySelector('#warning');warn.textContent=S.bundle.error?tr('dataInvalid')+S.bundle.error:'';warn.classList.toggle('show',!!S.bundle.error)}
    const demoMode=new URLSearchParams(location.search).get('demo')==='1';
    async function refresh(){try{if(window.__CONSTRUCTION_BUNDLE__){S.bundle=window.__CONSTRUCTION_BUNDLE__;renderAll();return}
      const r=await fetch(demoMode?'/api/construction?demo=1':'/api/construction',{cache:'no-store'}),p=await r.json();if(!p.status)throw new Error(p.error||tr('noConstructionStatus'));S.bundle=p;renderAll()}catch(e){const w=document.querySelector('#warning');w.textContent=tr('readError')+e.message;w.classList.add('show')}}
    async function enterPresentationMode(){document.body.classList.add('presentation-mode');const button=document.querySelector('#presentation-toggle');if(button){button.textContent=tr('exitPresentation');button.setAttribute('aria-pressed','true')};const soft=document.querySelector('#show-soft');if(soft)soft.checked=true;renderGraph();requestAnimationFrame(()=>fitGraph());try{if(!document.fullscreenElement&&document.documentElement.requestFullscreen)await document.documentElement.requestFullscreen()}catch(_error){}}
    async function exitPresentationMode(){document.body.classList.remove('presentation-mode');const button=document.querySelector('#presentation-toggle');if(button){button.textContent=tr('presentation');button.setAttribute('aria-pressed','false')};const soft=document.querySelector('#show-soft');if(soft)soft.checked=false;if(document.fullscreenElement&&document.exitFullscreen){try{await document.exitFullscreen()}catch(_error){}}renderGraph();requestAnimationFrame(()=>fitGraph())}
    async function togglePresentationMode(){if(document.body.classList.contains('presentation-mode'))await exitPresentationMode();else await enterPresentationMode()}
    document.querySelector('#presentation-toggle').onclick=togglePresentationMode;
    document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement&&document.body.classList.contains('presentation-mode')){document.body.classList.remove('presentation-mode');const button=document.querySelector('#presentation-toggle');if(button){button.textContent=tr('presentation');button.setAttribute('aria-pressed','false')};const soft=document.querySelector('#show-soft');if(soft)soft.checked=false;renderGraph()}});
    async function fitGraph(){if(!S.bundle)return;if(selectedStructure()==='webgl3d'&&S.webgl?.native){S.webgl.fitRequested=true;webglFit(S.webgl);return}const graph=await computeGraphLayout(S.bundle.status.flow_graph.nodes||[],S.bundle.status.flow_graph.edges||[]),viewport=document.querySelector('#viewport');
      const widthScale=(viewport.clientWidth-30)/graph.width,heightScale=(viewport.clientHeight-30)/graph.height;S.scale=Math.max(.42,Math.min(.92,widthScale,heightScale));viewport.scrollTo(0,0);renderGraph()}
    function zoomGraph(delta){if(selectedStructure()==='webgl3d'&&S.webgl?.native){S.webgl.distance=Math.max(240,Math.min(7000,S.webgl.distance*(delta>0?.82:1.22)));return}S.scale=Math.max(.42,Math.min(1.5,S.scale+delta));renderGraph()}
    document.querySelector('#zoom-in').onclick=()=>zoomGraph(.1);document.querySelector('#zoom-out').onclick=()=>zoomGraph(-.1);
    document.querySelector('#fit').onclick=fitGraph;document.querySelector('#status-filter').onchange=renderGraph;document.querySelector('#show-soft').onchange=renderGraph;
    document.querySelector('#structure-select').onchange=event=>{const next=STRUCTURE_PROFILES.has(event.target.value)?event.target.value:'elk-right';try{localStorage.setItem('construction-layout',next)}catch(_error){}S.currentLayout=null;const url=new URL(location.href);url.searchParams.set('layout',next);history.replaceState(null,'',url);renderGraph()};
    document.querySelector('#layout-edit').onclick=()=>{S.editMode=!S.editMode;document.body.classList.toggle('editing-view',S.editMode);const select=document.querySelector('#structure-select');if(S.editMode){select.value='manual';try{localStorage.setItem('construction-layout','manual')}catch(_error){}ensureManualHistory()}else{saveLayoutDraft();S.manualHistory=[];S.manualHistoryIndex=-1}S.currentLayout=null;applyLocale();applyStructureOptionLabels();updateManualButtons();renderGraph();if(S.editMode)requestAnimationFrame(()=>fitGraph())};
    document.querySelector('#reset-layout').onclick=()=>{clearLayoutDraft();S.editMode=false;document.body.classList.remove('editing-view');const select=document.querySelector('#structure-select');select.value='elk-right';try{localStorage.setItem('construction-layout','elk-right')}catch(_error){}const url=new URL(location.href);url.searchParams.set('layout','elk-right');history.replaceState(null,'',url);S.currentLayout=null;applyLocale();applyStructureOptionLabels();updateManualButtons();renderGraph()};
    document.querySelector('#manual-undo').onclick=undoManual;document.querySelector('#manual-redo').onclick=redoManual;document.querySelector('#manual-arrange').onclick=autoArrangeManual;document.querySelector('#manual-snap-input').onchange=event=>{S.manualSnap=Boolean(event.target.checked)};
    document.addEventListener('keydown',event=>{if(!S.editMode||!(event.ctrlKey||event.metaKey))return;const key=event.key.toLowerCase();if(key==='z'&&!event.shiftKey){event.preventDefault();undoManual()}else if(key==='y'||(key==='z'&&event.shiftKey)){event.preventDefault();redoManual()}});
    ['search','only-tasks','only-blocked'].forEach(id=>document.querySelector('#'+id).addEventListener('input',renderTable));
    document.querySelector('#language-select').onchange=event=>{const next=event.target.value==='en'?'en':'zh';S.lang=next;try{localStorage.setItem('construction-language',next)}catch(_error){}const url=new URL(location.href);url.searchParams.set('lang',next);history.replaceState(null,'',url);applyLocale();if(S.bundle)renderAll()};
    document.querySelector('#language-select').addEventListener('change',()=>{applyStructureOptionLabels();applyManualLocale()});
    ensureStructureOptions();applyLocale();applyStructureOptionLabels();applyManualLocale();updateManualButtons();
    const initialStructure=document.querySelector('#structure-select');if(initialStructure)initialStructure.value=preferredStructure();
    const vp=document.querySelector('#viewport');let drag=null;vp.addEventListener('pointerdown',e=>{if(e.target.closest('.node'))return;drag={x:e.clientX,y:e.clientY,l:vp.scrollLeft,t:vp.scrollTop};vp.setPointerCapture(e.pointerId);vp.classList.add('dragging')});
    vp.addEventListener('pointermove',e=>{if(drag){vp.scrollLeft=drag.l-(e.clientX-drag.x);vp.scrollTop=drag.t-(e.clientY-drag.y)}});vp.addEventListener('pointerup',()=>{drag=null;vp.classList.remove('dragging')});
    vp.addEventListener('wheel',event=>{if(selectedStructure()==='webgl3d'&&S.webgl?.native)return;event.preventDefault();if(!S.bundle)return;const rect=vp.getBoundingClientRect(),before=S.scale,factor=Math.exp(-event.deltaY*.001),next=Math.max(.42,Math.min(1.5,before*factor)),anchorX=(event.clientX-rect.left+vp.scrollLeft)/before,anchorY=(event.clientY-rect.top+vp.scrollTop)/before;S.scale=next;renderGraph();requestAnimationFrame(()=>{vp.scrollLeft=Math.max(0,anchorX*next-(event.clientX-rect.left));vp.scrollTop=Math.max(0,anchorY*next-(event.clientY-rect.top))})},{passive:false});
    window.__CONSTRUCTION_HOOKS__={state:S,renderGraph:renderGraph,selectNode:selectNode,useUnifiedNodeHtml:false};
     refresh();setInterval(refresh,3000);
   </script>
 </body></html>"""
def render_construction_html(*, demo_mode: bool = False) -> str:
    """Render the formal state-machine UI, with a safe anonymous label in demo mode."""

    if not demo_mode:
        return CONSTRUCTION_HTML
    return (
        CONSTRUCTION_HTML.replace("项目施工状态机", "动态施工状态机 · 匿名演示")
        .replace("Project Construction State Machine", "Construction State Machine · Anonymous Demo")
    )


class ConstructionStatusHandler(WorkgroupStatusHandler):
    construction_store = ConstructionBundleStore(DEFAULT_CONSTRUCTION_POINTER)
    demo_construction_store = AnonymousConstructionBundleStore()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        demo_mode = (parse_qs(parsed.query).get("demo") or ["0"])[0] == "1"
        if path in {"/", "/index.html", "/construction"}:
            self.send_payload(
                render_construction_html(demo_mode=demo_mode).encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            return
        if path == "/workgroups":
            html = render_workgroup_html()
            back_target = "/?demo=1" if demo_mode else "/"
            back_link = (
                f'<a href="{back_target}" aria-label="返回动态施工状态机" '
                'style="position:fixed;right:22px;top:18px;z-index:9999;'
                'padding:9px 14px;border:1px solid #2f7b70;'
                'border-radius:999px;background:#0b2a28;color:#dffbf3;'
                'font:600 13px Segoe UI,Microsoft YaHei,sans-serif;'
                'text-decoration:none;box-shadow:0 6px 22px #0008">'
                "← 返回施工状态机</a>"
            )
            if "<body>" in html:
                html = html.replace("<body>", "<body>" + back_link, 1)
            else:
                html = back_link + html
            self.send_payload(
                html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            return
        if path == "/api/construction":
            store = (
                self.demo_construction_store
                if demo_mode
                else self.construction_store
            )
            payload = store.read()
            self.send_payload(
                (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
                content_type="application/json; charset=utf-8",
                status_code=200 if payload.get("status") else 503,
            )
            return
        if path == "/api/construction/events":
            store = (
                self.demo_construction_store
                if demo_mode
                else self.construction_store
            )
            payload = store.read()
            compact = {
                "ok": payload.get("ok"),
                "verified": payload.get("verified"),
                "stale_cache": payload.get("stale_cache"),
                "error": payload.get("error"),
                "events": payload.get("events") or [],
            }
            self.send_payload(
                (json.dumps(compact, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
                content_type="application/json; charset=utf-8",
                status_code=200 if payload.get("status") else 503,
            )
            return
        super().do_GET()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="工作组前端＋动态施工状态机只读展示。"
    )
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    parser.add_argument("--codex-state-db", default=str(DEFAULT_CODEX_STATE_DB))
    parser.add_argument("--title-map", default=str(DEFAULT_TITLE_MAP))
    parser.add_argument(
        "--construction-pointer",
        default=str(DEFAULT_CONSTRUCTION_POINTER),
    )
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    ConstructionStatusHandler.runtime_root = Path(args.runtime_root).resolve()
    ConstructionStatusHandler.codex_state_db = Path(
        args.codex_state_db
    ).resolve()
    ConstructionStatusHandler.title_map_path = Path(args.title_map).resolve()
    ConstructionStatusHandler.construction_store = ConstructionBundleStore(
        Path(args.construction_pointer).resolve()
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), ConstructionStatusHandler
    )
    print(
        json.dumps(
            {
                "ok": True,
                "display": f"http://{args.host}:{args.port}/",
                "construction_api": (
                    f"http://{args.host}:{args.port}/api/construction"
                ),
                "workgroups": f"http://{args.host}:{args.port}/workgroups",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
