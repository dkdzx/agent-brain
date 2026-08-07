from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_RUNTIME_ROOT = Path.home() / ".agent-brain" / "runtime"
DEFAULT_CODEX_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"
DEFAULT_TITLE_MAP = DEFAULT_RUNTIME_ROOT / "CODEX_THREAD_TITLE_MAP.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
TERMINAL_GROUP_STATES = {
    "ARCHIVED",
    "CLOSED",
    "DELETED",
    "EXPIRED",
    "EXPIRED_OR_ARCHIVED",
    "MEMBERS_REVOKED",
}
REVOKED_MEMBER_STATES = {"EXPIRED", "REMOVED", "REVOKED"}
ROLE_LABELS = {
    "controller": "总控",
    "worker": "施工",
    "reviewer": "审查",
    "observer": "观察",
}
INVALID_TASK_TITLE_MARKERS = (
    "<codex_delegation",
    "<source_thread_id",
    "<input>",
    "userMessage",
    "assistantMessage",
)
UNRESOLVED_TASK_TITLE = "任务名称待同步"


def now_local() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is not None else parsed.astimezone()


def group_is_active(group: dict[str, Any]) -> bool:
    state = str(group.get("state") or group.get("status") or "").upper()
    if state in TERMINAL_GROUP_STATES or group.get("closed_at"):
        return False
    expires_at = parse_datetime(group.get("expires_at"))
    return not expires_at or expires_at > now_local()


def member_is_active(member: dict[str, Any]) -> bool:
    if member.get("active") is not True:
        return False
    state = str(member.get("status") or "").upper()
    if state in REVOKED_MEMBER_STATES or member.get("revoked_at"):
        return False
    expires_at = parse_datetime(member.get("lease_expires_at"))
    return not expires_at or expires_at > now_local()


def contains_chinese(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def is_valid_codex_task_title(value: Any) -> bool:
    title = str(value or "").strip()
    if not title or len(title) > 160 or "\n" in title or "\r" in title:
        return False
    lowered = title.lower()
    if any(marker.lower() in lowered for marker in INVALID_TASK_TITLE_MARKERS):
        return False
    if title.startswith("<") or title.endswith(">"):
        return False
    return True


def group_display_title(group: dict[str, Any], group_id: str) -> str:
    public_name = str(group.get("public_display_name") or "").strip()
    if public_name:
        return public_name
    display_name = str(group.get("display_name") or "").strip()
    if display_name:
        return display_name
    return "工作组"


def group_instance_label(group_id: str) -> str:
    if contains_chinese(group_id):
        return group_id
    return "工作组实例"


def load_codex_thread_titles(
    database_path: Path,
    thread_ids: list[str],
    title_map_path: Path,
) -> dict[str, str]:
    wanted = sorted({thread_id for thread_id in thread_ids if thread_id})
    if not wanted:
        return {}
    overrides: dict[str, str] = {}
    if title_map_path.is_file():
        try:
            payload = read_json_object(title_map_path)
            rows = payload.get("threads")
            if isinstance(rows, dict):
                overrides = {
                    str(thread_id): str(title).strip()
                    for thread_id, title in rows.items()
                    if is_valid_codex_task_title(title)
                }
        except (OSError, ValueError, json.JSONDecodeError):
            overrides = {}
    if not database_path.is_file():
        return {
            thread_id: overrides[thread_id]
            for thread_id in wanted
            if thread_id in overrides
        }
    placeholders = ",".join("?" for _ in wanted)
    uri = database_path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        rows = connection.execute(
            (
                "SELECT id, "
                "COALESCE(NULLIF(TRIM(title), ''), NULLIF(TRIM(name), ''), id) "
                f"FROM threads WHERE id IN ({placeholders})"
            ),
            wanted,
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass
    database_titles = {
        str(thread_id): str(title).strip()
        for thread_id, title in rows
        if thread_id and is_valid_codex_task_title(title)
    }
    database_titles.update(
        {
            thread_id: overrides[thread_id]
            for thread_id in wanted
            if thread_id in overrides
        }
    )
    return database_titles


def safe_group_row(
    group_dir: Path,
    *,
    codex_state_db: Path,
    title_map_path: Path,
) -> dict[str, Any] | None:
    group_path = group_dir / "group.json"
    members_path = group_dir / "members.json"
    if not group_path.is_file() or not members_path.is_file():
        return None
    try:
        group = read_json_object(group_path)
        member_payload = read_json_object(members_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    members = member_payload.get("members")
    if not isinstance(members, dict):
        members = {}
    member_rows = [
        member
        for member in members.values()
        if isinstance(member, dict)
    ]
    title_map = load_codex_thread_titles(
        codex_state_db,
        [
            str(member.get("thread_id") or "")
            for member in member_rows
        ],
        title_map_path,
    )
    controller_member_id = str(group.get("controller_member_id") or "")
    safe_members: list[dict[str, Any]] = []
    role_counts: dict[str, int] = {}
    for member in member_rows:
        role = str(member.get("role") or "unknown")
        active = member_is_active(member)
        if not active:
            continue
        role_counts[role] = role_counts.get(role, 0) + 1
        member_id = str(member.get("member_id") or "")
        thread_id = str(member.get("thread_id") or "")
        member_title = str(member.get("codex_task_title") or "").strip()
        if not is_valid_codex_task_title(member_title):
            member_title = ""
        resolved_title = member_title or title_map.get(thread_id)
        if not is_valid_codex_task_title(resolved_title):
            resolved_title = UNRESOLVED_TASK_TITLE
        safe_members.append(
            {
                "member_id": member_id,
                "conversation_title": resolved_title,
                "conversation_title_source": (
                    "member_verified_codex_task_title"
                    if member_title
                    else (
                        "verified_thread_title_map_or_database"
                        if resolved_title != UNRESOLVED_TASK_TITLE
                        else "unresolved_fail_closed"
                    )
                ),
                "role": role,
                "role_label": ROLE_LABELS.get(role, role),
                "active": True,
                "status": "活跃",
                "is_controller": (
                    member_id == controller_member_id or role == "controller"
                ),
                "joined_at": member.get("joined_at") or member.get("added_at"),
                "lease_expires_at": member.get("lease_expires_at"),
            }
        )
    safe_members.sort(
        key=lambda row: (
            not row["is_controller"],
            row["role"],
            row["conversation_title"],
        )
    )

    return {
        "group_id": str(group.get("group_id") or group_dir.name),
        "task_id": str(group.get("task_id") or ""),
        "display_title": group_display_title(
            group,
            str(group.get("group_id") or group_dir.name),
        ),
        "display_instance": group_instance_label(
            str(group.get("group_id") or group_dir.name)
        ),
        "objective": str(group.get("objective") or ""),
        "state": str(
            group.get("state") or group.get("status") or "UNKNOWN"
        ).upper(),
        "active": group_is_active(group),
        "active_member_count": len(safe_members),
        "total_member_count": len(safe_members),
        "role_counts": role_counts,
        "members": safe_members,
        "created_at": group.get("created_at"),
        "closed_at": group.get("closed_at"),
        "expires_at": group.get("expires_at"),
    }


def build_workgroup_status(
    runtime_root: Path,
    codex_state_db: Path = DEFAULT_CODEX_STATE_DB,
    title_map_path: Path | None = None,
) -> dict[str, Any]:
    resolved_title_map = title_map_path or (runtime_root / "CODEX_THREAD_TITLE_MAP.json")
    groups: list[dict[str, Any]] = []
    if runtime_root.is_dir():
        for group_dir in sorted(runtime_root.iterdir(), key=lambda path: path.name):
            if not group_dir.is_dir() or group_dir.name == "canary_support":
                continue
            row = safe_group_row(
                group_dir,
                codex_state_db=codex_state_db,
                title_map_path=resolved_title_map,
            )
            if row is not None:
                groups.append(row)

    active_groups = [row for row in groups if row["active"]]
    recent_groups = sorted(
        [row for row in groups if not row["active"]],
        key=lambda row: str(
            row.get("closed_at") or row.get("created_at") or ""
        ),
        reverse=True,
    )[:4]
    active_members = sum(row["active_member_count"] for row in active_groups)
    role_counts: dict[str, int] = {}
    for group in active_groups:
        for role, count in group["role_counts"].items():
            role_counts[role] = role_counts.get(role, 0) + int(count)

    return {
        "schema_version": "agent_brain_workgroup_frontend_status_v1",
        "generated_at": now_iso(),
        "display_name": "工作组",
        "active_group_count": len(active_groups),
        "active_member_count": active_members,
        "role_counts": role_counts,
        "active_groups": active_groups,
        "recent_groups": recent_groups,
        "archived_group_count": len(groups) - len(active_groups),
        "runtime_available": runtime_root.is_dir(),
        "privacy": {
            "internal_working_content_exposed": False,
            "host_or_thread_identity_exposed": False,
            "lease_token_exposed": False,
        },
    }


def build_loopx_projection(status: dict[str, Any]) -> dict[str, Any]:
    agents: list[dict[str, Any]] = []
    for group in status["active_groups"]:
        used_names: dict[str, int] = {}
        for member in group["members"]:
            if not member["active"]:
                continue
            title = member["conversation_title"]
            used_names[title] = used_names.get(title, 0) + 1
            suffix = (
                f"（{used_names[title]}）"
                if used_names[title] > 1
                else ""
            )
            agents.append(
                {
                    "agent_id": f"{title}{suffix}",
                    "role": member["role"],
                    "state": "active",
                    "next_action": "参与当前工作组",
                    "last_activity_at": status["generated_at"],
                    "goal_ids": [group["task_id"]] if group["task_id"] else [],
                    "workgroup_id": group["group_id"],
                    "is_controller": member["is_controller"],
                }
            )
    return {
        "schema_version": "loopx_status_v2",
        "ok": True,
        "registry": "",
        "runtime_root": "",
        "goal_count": 0,
        "run_count": 0,
        "status_contract": {
            "schema_version": 2,
            "minimum_dashboard_schema_version": 2,
            "producer": "agent_brain_workgroup_status_frontend",
            "reload_hint": "poll",
        },
        "contract": {
            "ok": True,
            "summary": {"errors": 0, "warnings": 0, "checks": 1},
            "errors": [],
            "warnings": [],
            "checks": ["workgroup_count_projection_only"],
        },
        "attention_queue": {
            "available": True,
            "item_count": 0,
            "needs_user_or_controller": 0,
            "needs_controller": 0,
            "needs_codex": 0,
            "watching_external_evidence": 0,
            "items": [],
        },
        "agent_management_projection": {
            "schema_version": "agent_management_projection_v0",
            "mode": "read-only",
            "goal_id": None,
            "generated_at": status["generated_at"],
            "truth_contract": {
                "todo_is_runtime_work_item": False,
                "projection_is_writable": False,
                "introduces_task_runtime": False,
                "write_api": False,
            },
            "source_summary": {
                "registered_agent_count": status["active_member_count"],
                "projected_agent_count": status["active_member_count"],
                "todo_source": "active workgroup runtime",
            },
            "workgroup_summary": {
                "display_name": "工作组",
                "active_group_count": status["active_group_count"],
                "active_member_count": status["active_member_count"],
                "role_counts": status["role_counts"],
            },
            "agents": agents,
        },
    }


def render_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>工作组</title>
  <style>
    :root { color-scheme: dark; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; background: #071614; color: #fff7e8; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: radial-gradient(circle at 75% 0%, rgba(31,178,145,.16), transparent 38%), linear-gradient(145deg,#061210,#0a211d 55%,#071614); }
    main { width: min(760px,100%); border: 1px solid rgba(209,250,229,.17); border-radius: 24px; overflow: hidden; background: rgba(5,31,28,.91); box-shadow: 0 28px 90px rgba(0,0,0,.4); }
    header { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 26px 28px; border-bottom: 1px solid rgba(209,250,229,.13); }
    h1 { margin: 0; font-size: 24px; letter-spacing: .04em; }
    .live { display: inline-flex; align-items: center; gap: 8px; color: #a7f3d0; font-size: 13px; font-weight: 700; }
    .live::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #34d399; box-shadow: 0 0 14px #34d399; }
    .metrics { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px; padding: 24px 28px 14px; }
    .metric { padding: 20px; border: 1px solid rgba(209,250,229,.13); border-radius: 18px; background: rgba(209,250,229,.045); }
    .metric span { display: block; color: rgba(209,250,229,.6); font-size: 13px; font-weight: 700; }
    .metric strong { display: block; margin-top: 8px; font-size: 42px; font-variant-numeric: tabular-nums; }
    .group-section { padding: 0 28px 22px; }
    .section-title { margin: 6px 0 10px; color: rgba(236,253,245,.72); font-size: 13px; letter-spacing: .08em; }
    .group { margin-top: 10px; padding: 16px; border: 1px solid rgba(209,250,229,.12); border-radius: 16px; background: rgba(0,0,0,.15); }
    .group-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
    .group-title { font-weight: 800; line-height: 1.5; }
    .group-meta { margin-top: 5px; color: rgba(236,253,245,.55); font-size: 12px; }
    .state { flex: 0 0 auto; padding: 5px 9px; border-radius: 999px; border: 1px solid rgba(52,211,153,.2); background: rgba(52,211,153,.08); color: #a7f3d0; font-size: 11px; font-weight: 800; }
    .state.closed { border-color: rgba(148,163,184,.18); background: rgba(148,163,184,.07); color: #cbd5e1; }
    .members { display: grid; gap: 8px; margin-top: 14px; }
    .member { display: grid; grid-template-columns: 36px minmax(0,1fr) auto; align-items: center; gap: 10px; padding: 10px 11px; border: 1px solid rgba(209,250,229,.09); border-radius: 12px; background: rgba(209,250,229,.025); }
    .avatar { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 11px; background: linear-gradient(145deg,#1d8d78,#155e55); color: #ecfdf5; font-size: 13px; font-weight: 900; }
    .member-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 750; }
    .member-meta { margin-top: 3px; color: rgba(236,253,245,.48); font-size: 11px; }
    .member-role { display: inline-flex; align-items: center; gap: 5px; color: #bae6fd; font-size: 12px; font-weight: 700; }
    .inactive { opacity: .58; }
    .empty { padding: 18px 0 6px; color: rgba(236,253,245,.55); font-size: 14px; }
    footer { padding: 14px 28px; border-top: 1px solid rgba(209,250,229,.1); color: rgba(236,253,245,.43); font-size: 12px; }
    @media (max-width: 540px) { .metrics { grid-template-columns: 1fr; } header { align-items: flex-start; flex-direction: column; } }
  </style>
</head>
<body>
  <main>
    <header><h1>工作组</h1><span class="live" id="connection">实时</span></header>
    <section class="metrics">
      <div class="metric"><span>活动工作组</span><strong id="group-count">—</strong></div>
      <div class="metric"><span>活跃成员</span><strong id="member-count">—</strong></div>
    </section>
    <section class="group-section">
      <h2 class="section-title">运行中的工作组</h2>
      <div id="active-groups"></div>
    </section>
    <section class="group-section">
      <h2 class="section-title">已归档的工作组</h2>
      <div id="recent-groups"></div>
    </section>
    <footer id="updated">等待运行态……</footer>
  </main>
  <script>
    const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
    const memberHtml = (member) => {
      const title = member.conversation_title || "未绑定任务";
      const initial = (title.trim()[0] || "组").toUpperCase();
      const role = member.is_controller ? `<div class="member-role">总控</div>` : "";
      const memberLabel = member.is_controller ? "工作组总控" : "工作组成员";
      return `<div class="member">
        <div class="avatar">${escapeHtml(initial)}</div>
        <div>
          <div class="member-title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
          <div class="member-meta">${memberLabel} · ${escapeHtml(member.status)}</div>
        </div>
        ${role}
      </div>`;
    };
    const groupHtml = (group, closed) => {
      const title = group.display_title || "工作组任务";
      const groupName = group.display_instance || "工作组实例";
      const stateLabels = {
        ACTIVE: "协作中",
        HANDOFF_READY: "待交接",
        RECONCILED: "已完成待关闭",
        FREEZING: "冻结中"
      };
        const stateLabel = closed ? "已归档" : (stateLabels[group.state] || "进行中");
      return `<article class="group">
        <div class="group-head">
          <div>
            <div class="group-title">${escapeHtml(title)}</div>
            <div class="group-meta">${escapeHtml(groupName)} · ${group.active_member_count}/${group.total_member_count} 人</div>
          </div>
          <span class="state ${closed ? "closed" : ""}">${stateLabel}</span>
        </div>
        <div class="members">${(group.members || []).map(memberHtml).join("")}</div>
      </article>`;
    };
    async function refresh() {
      try {
        const response = await fetch("/api/status", {cache:"no-store"});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        document.querySelector("#group-count").textContent = data.active_group_count;
        document.querySelector("#member-count").textContent = data.active_member_count;
        document.querySelector("#connection").textContent = "实时";
        const activeGroups = data.active_groups || [];
        document.querySelector("#active-groups").innerHTML = activeGroups.length
          ? activeGroups.map((group) => groupHtml(group, false)).join("")
          : `<div class="empty">当前没有运行中的工作组。</div>`;
        const recentGroups = data.recent_groups || [];
        document.querySelector("#recent-groups").innerHTML = recentGroups.length
          ? recentGroups.map((group) => groupHtml(group, true)).join("")
          : `<div class="empty">暂无已归档的工作组。</div>`;
        document.querySelector("#updated").textContent = `更新于 ${data.generated_at}；仅显示工作组与成员，不显示内部工作内容。`;
      } catch (error) {
        document.querySelector("#connection").textContent = "连接中断";
        document.querySelector("#updated").textContent = String(error);
      }
    }
    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""


class WorkgroupStatusHandler(BaseHTTPRequestHandler):
    runtime_root = DEFAULT_RUNTIME_ROOT
    codex_state_db = DEFAULT_CODEX_STATE_DB
    title_map_path = DEFAULT_TITLE_MAP

    def send_payload(
        self,
        payload: bytes,
        *,
        content_type: str,
        status_code: int = 200,
    ) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        status = build_workgroup_status(
            self.runtime_root,
            codex_state_db=self.codex_state_db,
            title_map_path=self.title_map_path,
        )
        if path in {"/", "/index.html"}:
            self.send_payload(
                render_html().encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            return
        if path == "/api/status":
            self.send_payload(
                (json.dumps(status, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
                content_type="application/json; charset=utf-8",
            )
            return
        if path == "/loopx/status.json":
            self.send_payload(
                (
                    json.dumps(
                        build_loopx_projection(status),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
                content_type="application/json; charset=utf-8",
            )
            return
        if path == "/health":
            self.send_payload(
                b'{"ok":true}\n',
                content_type="application/json; charset=utf-8",
            )
            return
        self.send_payload(
            b'{"ok":false,"error":"not_found"}\n',
            content_type="application/json; charset=utf-8",
            status_code=404,
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读展示当前工作组和活跃成员数量。"
    )
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    parser.add_argument("--codex-state-db", default=str(DEFAULT_CODEX_STATE_DB))
    parser.add_argument("--title-map", default=str(DEFAULT_TITLE_MAP))
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--output")
    snapshot.add_argument("--loopx-output")

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    args = parser.parse_args()
    runtime_root = Path(args.runtime_root).resolve()
    codex_state_db = Path(args.codex_state_db).resolve()
    title_map_path = Path(args.title_map).resolve()

    if args.command == "snapshot":
        status = build_workgroup_status(
            runtime_root,
            codex_state_db=codex_state_db,
            title_map_path=title_map_path,
        )
        if args.output:
            atomic_write_json(Path(args.output).resolve(), status)
        if args.loopx_output:
            atomic_write_json(
                Path(args.loopx_output).resolve(),
                build_loopx_projection(status),
            )
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    WorkgroupStatusHandler.runtime_root = runtime_root
    WorkgroupStatusHandler.codex_state_db = codex_state_db
    WorkgroupStatusHandler.title_map_path = title_map_path
    server = ThreadingHTTPServer((args.host, args.port), WorkgroupStatusHandler)
    print(
        json.dumps(
            {
                "ok": True,
                "display": f"http://{args.host}:{args.port}/",
                "api": f"http://{args.host}:{args.port}/api/status",
                "loopx_status": f"http://{args.host}:{args.port}/loopx/status.json",
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
