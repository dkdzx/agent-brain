from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from . import workgroup_brain as _workgroup_brain
except ImportError:  # pragma: no cover - direct ``python src/...`` execution
    import workgroup_brain as _workgroup_brain  # type: ignore[no-redef]

DEFAULT_CONTEXT_BUDGET_BYTES = int(
    getattr(_workgroup_brain, "DEFAULT_CONTEXT_BUDGET_BYTES", 262144)
)
DEFAULT_CONTEXT_MAX_ENTRIES = int(
    getattr(_workgroup_brain, "DEFAULT_CONTEXT_MAX_ENTRIES", 512)
)
DEFAULT_MEMBER_POSITION_CARD_LIMIT = int(
    getattr(_workgroup_brain, "DEFAULT_MEMBER_POSITION_CARD_LIMIT", 15)
)
filtered_context = getattr(_workgroup_brain, "filtered_context", None)
materialize_view = getattr(_workgroup_brain, "materialize_view", None)


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
FRONTEND_STATUS_SCHEMA_VERSION = "agent_brain_workgroup_frontend_status_v2"
EXACT_ENTRY_SCHEMA_VERSION = "agent_brain_workgroup_frontend_exact_entry_v1"
NO_FINAL_SIGNING_AUTHORITY = "无最终签字权"
DIAGNOSTIC_EVIDENCE_VALID = "诊断证据有效"
MODEL_GATE_NONCOMPLIANT = "模型门不合规"


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


def compact_text(value: Any, max_chars: int = 280) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def normalize_scope(group: dict[str, Any], view: dict[str, Any] | None) -> str:
    raw_scope = group.get("scope")
    if isinstance(raw_scope, list) and raw_scope:
        return str(raw_scope[0])
    if isinstance(raw_scope, str) and raw_scope.strip():
        return raw_scope.strip()
    if isinstance(view, dict):
        entries = view.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("scope"):
                    return str(entry["scope"])
    return "task/shared"


def scope_matches(member: dict[str, Any], requested_scope: str) -> bool:
    read_scope = member.get("read_scope") or member.get("scopes") or []
    if not isinstance(read_scope, list):
        return False
    for allowed in read_scope:
        allowed_text = str(allowed)
        if allowed_text == "*":
            return True
        if requested_scope == allowed_text or requested_scope.startswith(
            allowed_text + "/"
        ):
            return True
    return False


def resolved_member_title(
    member: dict[str, Any],
    title_map: dict[str, str],
) -> tuple[str, str]:
    member_title = str(member.get("codex_task_title") or "").strip()
    if is_valid_codex_task_title(member_title):
        return member_title, "member_verified_codex_task_title"
    thread_id = str(member.get("thread_id") or "")
    mapped_title = title_map.get(thread_id)
    if is_valid_codex_task_title(mapped_title):
        return mapped_title, "verified_thread_title_map_or_database"
    return UNRESOLVED_TASK_TITLE, "unresolved_fail_closed"


def context_member_copy(member: dict[str, Any], title: str) -> dict[str, Any]:
    copied = dict(member)
    copied["active"] = member_is_active(member)
    copied["codex_task_title"] = title
    copied["read_scope"] = list(
        member.get("read_scope") or member.get("scopes") or []
    )
    copied["write_scope"] = list(member.get("write_scope") or [])
    return copied


def display_badges(card: dict[str, Any]) -> dict[str, str]:
    evidence_status = str(card.get("evidence_status") or "").strip().lower()
    model_gate_status = str(card.get("model_gate_status") or "").strip().lower()
    signing_authority = str(card.get("signing_authority") or "").strip().lower()
    evidence_is_valid = bool(card.get("evidence_refs")) or evidence_status in {
        "valid",
        "verified",
        "diagnostic_valid",
        "diagnostic evidence valid",
        DIAGNOSTIC_EVIDENCE_VALID.lower(),
    }
    model_is_noncompliant = model_gate_status in {
        "invalid",
        "noncompliant",
        "non-compliant",
        "not_compliant",
        "模型门不合规",
    }
    model_is_compliant = model_gate_status in {
        "valid",
        "verified",
        "compliant",
        "合规",
        "模型门合规",
    }
    has_final_authority = signing_authority in {
        "final",
        "project_final",
        "host_final",
        "最终签字权",
    }
    return {
        "evidence": (
            DIAGNOSTIC_EVIDENCE_VALID
            if evidence_is_valid
            else "诊断证据状态未提供"
        ),
        "model_gate": (
            MODEL_GATE_NONCOMPLIANT
            if model_is_noncompliant
            else ("模型门合规" if model_is_compliant else "模型门状态未提供")
        ),
        "signing": (
            "最终签字权需上游确认"
            if has_final_authority
            else NO_FINAL_SIGNING_AUTHORITY
        ),
    }


def safe_card_for_frontend(
    card: dict[str, Any],
    member_titles: dict[str, str],
) -> dict[str, Any]:
    member_id = str(card.get("member_id") or "")
    related_ids = [
        str(entry_id)
        for entry_id in card.get("related_entry_ids", [])
        if entry_id
    ]
    source_entry_id = str(card.get("source_entry_id") or "") or None
    result = {
        "codex_task_title": (
            str(card.get("codex_task_title") or "").strip()
            or member_titles.get(member_id)
            or UNRESOLVED_TASK_TITLE
        ),
        "thread_id_available": bool(card.get("thread_id")),
        "member_active": bool(card.get("member_active")),
        "participation_status": card.get(
            "participation_status",
            "active_member"
            if card.get("member_active")
            else "historical_diagnostic_evidence_only",
        ),
        "core_claim": card.get("core_claim"),
        "strongest_evidence": card.get("strongest_evidence"),
        "strongest_counterevidence": card.get("strongest_counterevidence"),
        "scope": card.get("scope"),
        "claim_ceiling": card.get("claim_ceiling"),
        "evidence_status": card.get("evidence_status"),
        "model_gate_status": card.get("model_gate_status"),
        "signing_authority": card.get("signing_authority"),
        "source_entry_id": source_entry_id,
        "source_entry": {
            "entry_id": source_entry_id,
            "retrieval": "exact_get_entry_or_anonymous_fixture",
        },
        "related_entry_ids": related_ids,
        "evidence_refs": [
            str(ref) for ref in card.get("evidence_refs", []) if ref
        ],
    }
    result["badges"] = display_badges(result)
    return result


def safe_entry_for_frontend(
    entry: dict[str, Any],
    member_titles: dict[str, str],
    event_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    author_id = str(entry.get("author_member_id") or "")
    content = entry.get("content")
    if content is None:
        content = entry.get("payload")
    event_meta = event_meta or {}
    if isinstance(content, dict):
        preview_value = None
        for key in (
            "core_claim",
            "claim",
            "ruling",
            "conclusion",
            "text",
        ):
            if content.get(key) not in (None, "", [], {}):
                preview_value = content.get(key)
                break
        if preview_value is None:
            preview_value = content
        content_preview = compact_text(preview_value, 220)
    else:
        content_preview = compact_text(content, 220)
    return {
        "entry_id": entry.get("entry_id"),
        "entry_seq": entry.get("entry_seq"),
        "global_event_seq": event_meta.get(
            "global_event_seq", entry.get("entry_seq")
        ),
        "core_event_seq": event_meta.get("core_event_seq"),
        "core_event_count": event_meta.get("core_event_count"),
        "is_core_event": bool(event_meta.get("is_core_event", True)),
        "event_category": event_meta.get("event_category", "裁决相关"),
        "entry_type": entry.get("entry_type"),
        "subject_key": entry.get("subject_key"),
        "scope": entry.get("scope"),
        "status": entry.get("status"),
        "confidence": entry.get("confidence"),
        "created_at": entry.get("created_at"),
        "author_task_title": member_titles.get(author_id, UNRESOLVED_TASK_TITLE),
        "content_preview": content_preview,
        "evidence_refs": [
            str(ref) for ref in entry.get("evidence_refs", []) if ref
        ],
        "has_exact_content": True,
    }


EVENT_CATEGORY_LABELS = {
    "decision": "决策",
    "effect": "真实效果",
    "challenge": "质询",
    "evidence": "证据附加",
    "claim": "认领/完成",
    "accounting": "核算/投影/ABSTAIN",
    "other": "其他流水",
}
CORE_EVENT_CATEGORIES = {"decision", "effect", "challenge", "evidence", "claim"}


def classify_event_type(entry: dict[str, Any]) -> tuple[str, bool]:
    entry_type = str(entry.get("entry_type") or "").upper()
    if entry_type in {
        "LOCAL_DECISION",
        "CURRENT_BEST_MODEL",
        "CONFLICT_RESOLVED",
        "HANDOFF_READY",
        "SCOPE_DECISION",
    }:
        return "decision", True
    if entry_type in {
        "PARTIAL_RESULT",
        "FACT_CONFIRMED",
        "ARTIFACT_PUBLISHED",
        "REAL_EFFECT",
        "K3_EFFECT",
    }:
        return "effect", True
    if entry_type in {
        "QUESTION_OPENED",
        "CONFLICT_RECORDED",
        "SCOPE_WARNING",
        "CHALLENGE",
        "OBJECTION",
    }:
        return "challenge", True
    if entry_type in {"EVIDENCE_ATTACHED", "EVIDENCE_ADDED", "VERIFICATION"}:
        return "evidence", True
    if entry_type in {
        "TASK_CLAIMED",
        "TASK_COMPLETED",
        "TODO_CLAIMED",
        "TODO_COMPLETED",
        "CLAIM",
        "COMPLETION",
    }:
        return "claim", True
    if entry_type in {
        "USAGE_RECORDED",
        "COST_RECORDED",
        "QUOTA_UPDATED",
        "PROJECTION_UPDATED",
        "ACCOUNTING",
        "ABSTAIN",
        "HEARTBEAT",
        "MEMBER_ADDED",
        "MEMBER_REVOKED",
        "SNAPSHOT_CREATED",
    }:
        return "accounting", False
    if entry.get("evidence_refs"):
        return "evidence", True
    return "other", False


def load_event_rows(group_dir: Path) -> list[dict[str, Any]]:
    path = group_dir / "events.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def build_event_summary(
    group_dir: Path,
    group: dict[str, Any],
    view: dict[str, Any] | None,
    entries: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build a display-only event projection without changing global seq."""
    raw_events = load_event_rows(group_dir)
    view_counts = view.get("counts") if isinstance(view, dict) else {}
    if not isinstance(view_counts, dict):
        view_counts = {}
    raw_seq = [
        int(item.get("seq"))
        for item in raw_events
        if str(item.get("seq") or "").isdigit()
    ]
    entry_seqs = [
        int(item.get("entry_seq"))
        for item in entries
        if str(item.get("entry_seq") or "").isdigit()
    ]
    total_candidates = [0, *raw_seq, *entry_seqs]
    for source in (view_counts.get("events"), group.get("event_total_count")):
        try:
            total_candidates.append(int(source or 0))
        except (TypeError, ValueError):
            continue
    total_stream_count = max(total_candidates)
    ordered = sorted(
        [item for item in entries if isinstance(item, dict)],
        key=lambda item: int(item.get("entry_seq") or 0),
    )
    meta_by_entry: dict[str, dict[str, Any]] = {}
    core_index = 0
    category_counts = {label: 0 for label in EVENT_CATEGORY_LABELS.values()}
    core_category_counts = {label: 0 for label in EVENT_CATEGORY_LABELS.values()}
    for entry in ordered:
        category, is_core = classify_event_type(entry)
        label = EVENT_CATEGORY_LABELS[category]
        category_counts[label] += 1
        core_seq = None
        if is_core:
            core_index += 1
            core_seq = core_index
            core_category_counts[label] += 1
        meta_by_entry[str(entry.get("entry_id") or "")] = {
            "global_event_seq": entry.get("entry_seq"),
            "core_event_seq": core_seq,
            "core_event_count": None,  # filled after the pass
            "is_core_event": is_core,
            "event_category": label,
        }
    for meta in meta_by_entry.values():
        meta["core_event_count"] = core_index
    real_effect_count = core_category_counts[EVENT_CATEGORY_LABELS["effect"]]
    evidence_count = sum(
        1
        for entry in ordered
        if entry.get("evidence_refs")
        or classify_event_type(entry)[0] == "evidence"
    )
    summary = {
        "total_stream_count": total_stream_count,
        "current_entry_count": len(ordered),
        "core_event_count": core_index,
        "real_effect_count": real_effect_count,
        "evidence_count": evidence_count,
        "category_counts": category_counts,
        "core_category_counts": core_category_counts,
        "raw_event_rows_available": bool(raw_events),
        "default_filter": "core",
        "core_filter_notice": "核心事件只包含可能影响任务裁决或真实效果/K3的条目；全部流水仍保留可查。",
    }
    return summary, meta_by_entry


def exact_entry_for_frontend(
    entry: dict[str, Any],
    member_titles: dict[str, str],
) -> dict[str, Any]:
    safe = dict(entry)
    author_id = str(safe.pop("author_member_id", "") or "")
    # Exact lookup may read the original event-shaped object.  Keep the
    # projection useful for diagnosis without leaking host/thread identity.
    safe.pop("thread_id", None)
    safe.pop("host_id", None)
    safe.pop("lease_token", None)
    safe.pop("lease_token_hash", None)
    safe.pop("payload_sha256", None)
    safe["author_task_title"] = member_titles.get(
        author_id,
        str(safe.get("author_task_title") or UNRESOLVED_TASK_TITLE),
    )
    safe["exact_content"] = safe.get("content", safe.get("payload"))
    safe.pop("payload", None)
    return safe


def group_display_title(group: dict[str, Any], group_id: str) -> str:
    for key in (
        "public_display_name",
        "display_name",
        "title",
        "task_title",
        "task_name",
    ):
        candidate = str(group.get(key) or "").strip()
        if not is_valid_codex_task_title(candidate):
            continue
        # Do not turn a machine-generated decision/project key into the
        # visible project title when no public name was supplied.
        compact_candidate = compact_text(candidate, 96) or ""
        identifier_like = (
            len(candidate) > 64
            and not contains_chinese(candidate)
            and not any(char.isspace() for char in candidate)
        )
        if compact_candidate and not identifier_like:
            return compact_candidate
    return "工作组（未命名）"


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


def load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = read_json_object(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload


def task_pool_source(
    group_dir: Path,
    group: dict[str, Any],
) -> Any:
    for key in ("task_pool", "tasks", "todo_items", "work_items"):
        if key in group and group[key] not in (None, [], {}):
            return group[key]
    for filename in ("task_pool.json", "tasks.json", "todo_items.json"):
        sidecar = load_optional_json(group_dir / filename)
        if sidecar is None:
            continue
        for key in ("task_pool", "tasks", "todo_items", "work_items"):
            if key in sidecar:
                return sidecar[key]
        return sidecar
    return None


def normalize_task_pool(
    group_dir: Path,
    group: dict[str, Any],
    member_titles: dict[str, str],
) -> dict[str, Any]:
    raw = task_pool_source(group_dir, group)
    if isinstance(raw, dict):
        if isinstance(raw.get("items"), list):
            rows = raw["items"]
        elif isinstance(raw.get("tasks"), list):
            rows = raw["tasks"]
        else:
            rows = [raw]
        policy = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
    elif isinstance(raw, list):
        rows = raw
        policy = {}
    else:
        rows = []
        policy = {}

    waiting: list[dict[str, Any]] = []
    claimed: list[dict[str, Any]] = []
    waiting_states = {
        "pending",
        "available",
        "unclaimed",
        "todo",
        "待领取",
        "待认领",
    }
    claimed_states = {
        "claimed",
        "in_progress",
        "in-progress",
        "active",
        "已领取",
        "已认领",
        "进行中",
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        task_id = str(
            row.get("task_id") or row.get("todo_id") or row.get("id") or ""
        )
        title = str(
            row.get("title")
            or row.get("name")
            or row.get("label")
            or task_id
            or "未命名任务"
        ).strip()
        raw_status = str(
            row.get("status") or row.get("state") or row.get("assignment_status") or ""
        ).strip()
        normalized_status = raw_status.lower()
        claimed_by = row.get("claimed_by") or row.get("assignee") or row.get("owner")
        claimed_by_id = str(claimed_by or "")
        assignee_title = member_titles.get(claimed_by_id)
        if not assignee_title and claimed_by:
            candidate_title = str(claimed_by).strip()
            assignee_title = (
                candidate_title
                if is_valid_codex_task_title(candidate_title)
                else UNRESOLVED_TASK_TITLE
            )
        if normalized_status in claimed_states or claimed_by:
            assignment_status = "已领取"
        elif normalized_status in waiting_states or not normalized_status:
            assignment_status = "待领取"
        else:
            assignment_status = "已领取" if "progress" in normalized_status else "待领取"
        safe_row = {
            "task_id": task_id or None,
            "title": title,
            "assignment_status": assignment_status,
            "raw_status": raw_status or None,
            "claimed_by_task_title": assignee_title,
            "scope": row.get("scope"),
            "priority": row.get("priority"),
            "dependencies": row.get("dependencies") or row.get("depends_on") or [],
            "required_role": row.get("required_role") or row.get("role"),
            "created_at": row.get("created_at"),
            "claimed_at": row.get("claimed_at"),
            "updated_at": row.get("updated_at") or row.get("claimed_at"),
            "evidence_ref": row.get("evidence_ref") or row.get("evidence_entry_id"),
        }
        (claimed if assignment_status == "已领取" else waiting).append(safe_row)

    one_person_one_task = bool(
        policy.get("one_person_one_task")
        or policy.get("single_task_per_member")
        or group.get("one_person_one_task")
        or group.get("single_task_per_member")
        or True
    )
    member_tasks: dict[str, list[str]] = {}
    task_owners: dict[str, list[str]] = {}
    for item in claimed:
        task_id = str(item.get("task_id") or "")
        owner = str(item.get("claimed_by_task_title") or "").strip()
        if owner:
            member_tasks.setdefault(owner, []).append(task_id)
        if task_id:
            task_owners.setdefault(task_id, []).append(owner or UNRESOLVED_TASK_TITLE)
    member_conflicts = [
        {"owner": owner, "task_ids": task_ids}
        for owner, task_ids in member_tasks.items()
        if len(task_ids) > 1
    ]
    task_conflicts = [
        {"task_id": task_id, "owners": owners}
        for task_id, owners in task_owners.items()
        if len(owners) > 1
    ]
    return {
        "available": bool(raw is not None),
        "waiting": waiting,
        "claimed": claimed,
        "waiting_count": len(waiting),
        "claimed_count": len(claimed),
        "one_person_one_task": one_person_one_task,
        "policy_label": "一人一任务",
        "member_conflicts": member_conflicts,
        "task_conflicts": task_conflicts,
        "conflict_count": len(member_conflicts) + len(task_conflicts),
        "source": "anonymous_fixture_or_workgroup_task_pool",
    }


def first_mapping_value(
    mappings: list[dict[str, Any]],
    keys: tuple[str, ...],
) -> Any:
    for mapping in mappings:
        for key in keys:
            if key in mapping and mapping[key] not in (None, "", [], {}):
                return mapping[key]
    return None


def normalize_budget_tiers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, (int, float, str)):
            try:
                budget_bytes = int(item)
            except (TypeError, ValueError):
                continue
            result.append({"budget_bytes": budget_bytes})
            continue
        if not isinstance(item, dict):
            continue
        raw_bytes = first_mapping_value(
            [item],
            (
                "budget_bytes",
                "bytes",
                "budget",
                "context_budget_bytes",
                "selected_budget_bytes",
            ),
        )
        try:
            budget_bytes = int(raw_bytes)
        except (TypeError, ValueError):
            continue
        normalized = dict(item)
        normalized["budget_bytes"] = budget_bytes
        result.append(normalized)
    deduped: dict[int, dict[str, Any]] = {}
    for item in result:
        deduped[item["budget_bytes"]] = item
    return [deduped[key] for key in sorted(deduped)]


def normalize_benefit_curve(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append(dict(item))
    return result


def normalize_context_policy(
    group: dict[str, Any],
    view: dict[str, Any] | None,
) -> dict[str, Any]:
    view_policy = view.get("context_policy") if isinstance(view, dict) else {}
    if not isinstance(view_policy, dict):
        view_policy = {}
    group_policy = group.get("context_policy")
    if not isinstance(group_policy, dict):
        group_policy = {}
    policy_sources = [view_policy, group_policy, group]

    selected_raw = first_mapping_value(
        policy_sources,
        (
            "selected_budget_bytes",
            "current_budget_bytes",
            "effective_budget_bytes",
            "budget_bytes",
            "context_budget_bytes",
        ),
    )
    try:
        selected_budget_bytes = int(selected_raw)
    except (TypeError, ValueError):
        selected_budget_bytes = DEFAULT_CONTEXT_BUDGET_BYTES

    tiers_raw = first_mapping_value(
        policy_sources,
        (
            "budget_tiers",
            "available_budget_tiers",
            "budget_options",
            "budgets",
            "budget_ladder_bytes",
        ),
    )
    budget_tiers = normalize_budget_tiers(tiers_raw)
    if not budget_tiers:
        budget_tiers = [
            {
                "budget_bytes": selected_budget_bytes,
                "label": (
                    "32KB 临时最小档"
                    if selected_budget_bytes == DEFAULT_CONTEXT_BUDGET_BYTES
                    else "当前选中档位"
                ),
                "temporary_minimum": selected_budget_bytes
                == DEFAULT_CONTEXT_BUDGET_BYTES,
            }
        ]

    max_scan_raw = first_mapping_value(
        policy_sources,
        (
            "max_scan_entries",
            "scan_guard_entries",
            "scan_guard_limit",
            "max_entries",
            "context_max_entries",
        ),
    )
    try:
        max_scan_entries = int(max_scan_raw)
    except (TypeError, ValueError):
        max_scan_entries = DEFAULT_CONTEXT_MAX_ENTRIES

    adaptive_raw = first_mapping_value(
        policy_sources,
        (
            "adaptive_budget",
            "adaptive_budget_enabled",
            "is_adaptive",
            "budget_is_adaptive",
        ),
    )
    mode = first_mapping_value(
        policy_sources,
        ("budget_mode", "mode", "selection_mode"),
    )
    adaptive = bool(adaptive_raw) or str(mode or "").lower() in {
        "adaptive",
        "auto",
        "marginal_gain",
        "边际收益",
    }
    curve_raw = first_mapping_value(
        policy_sources,
        (
            "benefit_curve",
            "budget_benefit_curve",
            "budget_comparison",
            "budget_comparisons",
            "sensitivity_curve",
        ),
    )
    benefit_curve = normalize_benefit_curve(curve_raw)
    policy_metrics = {
        "member_opinion_coverage": first_mapping_value(
            policy_sources, ("member_opinion_coverage", "position_card_coverage")
        ),
        "critical_decision_conflict_coverage": first_mapping_value(
            policy_sources,
            ("critical_decision_conflict_coverage", "decision_conflict_coverage", "key_conflict_coverage"),
        ),
        "duplicate_rate": first_mapping_value(
            policy_sources, ("duplicate_rate", "repetition_rate", "repeat_rate")
        ),
        "retrieval_supplement_count": first_mapping_value(
            policy_sources, ("retrieval_supplement_count", "supplement_count", "retrieval_additions")
        ),
        "latency_ms": first_mapping_value(
            policy_sources, ("latency_ms", "retrieval_latency_ms", "latency")
        ),
    }
    selected_reason = first_mapping_value(
        policy_sources,
        ("selected_reason", "selection_reason", "budget_selection_reason"),
    )
    elbow_budget_bytes = first_mapping_value(
        policy_sources,
        ("elbow_budget_bytes", "elbow", "elbow_point"),
    )
    doubling_marginal_gain = first_mapping_value(
        policy_sources,
        ("doubling_marginal_gain", "next_doubling_gain"),
    )
    minimum_raw = first_mapping_value(
        policy_sources,
        ("minimum_budget_bytes", "min_budget_bytes", "context_min_budget_bytes"),
    )
    try:
        minimum_budget_bytes = int(minimum_raw)
    except (TypeError, ValueError):
        minimum_budget_bytes = min(DEFAULT_CONTEXT_BUDGET_BYTES, selected_budget_bytes)
    target_raw = first_mapping_value(
        policy_sources, ("target_coverage", "context_target_coverage")
    )
    marginal_raw = first_mapping_value(
        policy_sources,
        ("minimum_marginal_gain", "min_marginal_gain", "context_min_marginal_gain"),
    )
    return {
        "selected_budget_bytes": selected_budget_bytes,
        "budget_bytes": selected_budget_bytes,
        "budget_tiers": budget_tiers,
        "max_scan_entries": max_scan_entries,
        "adaptive": adaptive,
        "mode": str(mode or ("adaptive" if adaptive else "compatibility_fallback")),
        "curve_available": bool(benefit_curve),
        "curve_source": (
            "bottom_layer_budget_policy"
            if benefit_curve or adaptive_raw is not None or mode
            else "compatibility_fallback"
        ),
        "benefit_curve": benefit_curve,
        "policy_metrics": policy_metrics,
        "selected_reason": selected_reason,
        "elbow_budget_bytes": elbow_budget_bytes,
        "doubling_marginal_gain": doubling_marginal_gain,
        "minimum_budget_bytes": minimum_budget_bytes,
        "target_coverage": target_raw,
        "minimum_marginal_gain": marginal_raw,
        "temporary_minimum_bytes": DEFAULT_CONTEXT_BUDGET_BYTES,
        "temporary_minimum_label": "临时最小档",
        "max_scan_entries_is_guard_only": True,
        "hard_upper_bound_declared": first_mapping_value(
            policy_sources,
            ("hard_upper_bound_declared", "budget_is_hard_cap", "is_hard_cap"),
        ),
    }


def benefit_metrics(
    raw_context: dict[str, Any],
    policy: dict[str, Any],
    retrieval: dict[str, Any],
) -> dict[str, Any]:
    raw_retrieval = raw_context.get("retrieval")
    if not isinstance(raw_retrieval, dict):
        raw_retrieval = {}
    raw_metrics = raw_context.get("benefit_metrics")
    if not isinstance(raw_metrics, dict):
        raw_metrics = {}
    mappings = [
        raw_metrics,
        raw_retrieval,
        policy.get("policy_metrics", {}),
        policy,
    ]

    included = retrieval["included_entry_count"]
    visible = retrieval["full_visible_entry_count"]
    entry_coverage = (included / visible) if visible else None
    curve = raw_context.get("context_budget_curve")
    if not isinstance(curve, list) or not curve:
        curve = policy.get("benefit_curve")
    if not isinstance(curve, list):
        curve = []
    curve_rows = [item for item in curve if isinstance(item, dict)]

    selected_budget = raw_context.get("context_budget", {})
    if not isinstance(selected_budget, dict):
        selected_budget = {}
    try:
        selected_budget_bytes = int(
            selected_budget.get(
                "selected_budget_bytes",
                selected_budget.get(
                    "budget_bytes", policy.get("selected_budget_bytes", 0)
                ),
            )
            or 0
        )
    except (TypeError, ValueError):
        selected_budget_bytes = int(policy.get("selected_budget_bytes") or 0)

    elbow_row = next(
        (item for item in curve_rows if item.get("elbow") is True),
        None,
    )
    if elbow_row is None:
        elbow_row = next(
            (
                item
                for item in curve_rows
                if int(item.get("budget_bytes") or 0) == selected_budget_bytes
            ),
            None,
        )

    # A row's ``marginal_gain_from_previous`` describes the gain into that
    # row.  The UI needs the gain after the elbow, so prefer the next row's
    # value unless the producer supplied an explicit next-doubling field.
    next_gain = None
    if elbow_row is not None:
        next_gain = first_mapping_value(
            [elbow_row], ("doubling_marginal_gain", "next_doubling_gain")
        )
        if next_gain is None:
            elbow_index = curve_rows.index(elbow_row)
            if elbow_index + 1 < len(curve_rows):
                next_gain = first_mapping_value(
                    [curve_rows[elbow_index + 1]],
                    (
                        "marginal_gain_from_previous",
                        "doubling_marginal_gain",
                        "marginal_gain",
                    ),
                )
        if next_gain is None:
            next_gain = first_mapping_value(
                [elbow_row], ("marginal_gain_from_previous", "marginal_gain")
            )

    coverage_value = first_mapping_value(
        mappings, ("coverage_ratio", "coverage", "entry_coverage")
    )
    explicit_elbow = first_mapping_value(
        mappings, ("elbow", "elbow_budget_bytes", "elbow_point")
    )
    if explicit_elbow is None:
        explicit_elbow = policy.get("elbow_budget_bytes")
    if explicit_elbow is None and elbow_row is not None:
        explicit_elbow = elbow_row.get("budget_bytes")
    explicit_gain = first_mapping_value(
        mappings,
        (
            "doubling_marginal_gain",
            "marginal_gain_after_doubling",
            "next_doubling_gain",
        ),
    )
    return {
        "coverage_ratio": entry_coverage if coverage_value is None else coverage_value,
        "omitted_member_opinion_count": first_mapping_value(
            mappings,
            (
                "omitted_member_opinion_count",
                "omitted_member_count",
                "omitted_position_card_count",
            ),
        ),
        "member_opinion_coverage": first_mapping_value(
            mappings, ("member_opinion_coverage", "position_card_coverage")
        ),
        "critical_decision_conflict_coverage": first_mapping_value(
            mappings,
            (
                "critical_decision_conflict_coverage",
                "decision_conflict_coverage",
                "key_conflict_coverage",
            ),
        ),
        "duplicate_rate": first_mapping_value(
            mappings, ("duplicate_rate", "repetition_rate", "repeat_rate")
        ),
        "retrieval_supplement_count": first_mapping_value(
            mappings,
            ("retrieval_supplement_count", "supplement_count", "retrieval_additions"),
        ),
        "latency_ms": first_mapping_value(
            mappings, ("latency_ms", "retrieval_latency_ms", "latency")
        ),
        "elbow": explicit_elbow,
        "doubling_marginal_gain": (
            explicit_gain if explicit_gain is not None else next_gain
        ),
        "budget_curve": curve_rows,
        "curve_available": bool(curve_rows),
        "curve_source": policy.get("curve_source"),
        "adaptive_budget": bool(policy.get("adaptive")),
        "metrics_source": (
            "bottom_layer_or_anonymous_fixture"
            if raw_metrics or policy.get("benefit_curve")
            else "compatibility_fallback"
        ),
    }


def fallback_filtered_context(
    view: dict[str, Any],
    member: dict[str, Any],
    requested_scope: str,
) -> dict[str, Any]:
    """Compatibility projection for older runtimes without filtered_context."""
    entries = [
        item
        for item in view.get("entries", [])
        if isinstance(item, dict)
        and str(item.get("scope") or "") in {requested_scope, "*"}
    ]
    policy = view.get("context_policy")
    if not isinstance(policy, dict):
        policy = {}
    budget_bytes = int(policy.get("budget_bytes", DEFAULT_CONTEXT_BUDGET_BYTES))
    max_entries = int(policy.get("max_entries", DEFAULT_CONTEXT_MAX_ENTRIES))
    ranked = sorted(
        entries,
        key=lambda item: int(item.get("entry_seq") or 0),
        reverse=True,
    )
    included = ranked[:max_entries]
    visible_ids = {item.get("entry_id") for item in included}
    omitted = [
        item.get("entry_id")
        for item in ranked
        if item.get("entry_id") not in visible_ids
    ]
    result = {
        "schema_version": "agent_brain_workgroup_runtime_v1",
        "group_id": view.get("group_id"),
        "task_id": view.get("task_id"),
        "state": view.get("state"),
        "requested_scope": requested_scope,
        "member": member,
        "view_version": view.get("view_version", 0),
        "context_kind": "bounded_injection_slice",
        "context_is_complete_workgroup_memory": False,
        "entries": included,
        "member_position_cards": [],
        "open_question_entry_ids": [
            item for item in view.get("open_question_entry_ids", []) if item in visible_ids
        ],
        "open_conflict_entry_ids": [
            item for item in view.get("open_conflict_entry_ids", []) if item in visible_ids
        ],
        "current_best_model_entry_ids": [
            item for item in view.get("current_best_model_entry_ids", []) if item in visible_ids
        ],
        "retrieval": {
            "full_visible_entry_count": len(ranked),
            "included_entry_count": len(included),
            "omitted_entry_count": len(omitted),
            "omitted_entry_id_sample": omitted[:24],
            "member_position_card_count": 0,
            "member_position_card_limit": DEFAULT_MEMBER_POSITION_CARD_LIMIT,
            "exact_entry_lookup_available": True,
            "exact_entry_lookup_command": "get-entry",
            "raw_event_archive_preserved": True,
        },
        "context_budget": {
            "budget_bytes": budget_bytes,
            "max_entries": max_entries,
            "policy": "compatibility_fallback_ranked_entries",
        },
        "authority_notice": {
            "host_control_plane_is_final_authority": True,
            "shared_brain_is_long_term_memory": False,
            "shared_brain_entries_are_project_truth": False,
        },
    }
    raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    result["context_budget"]["final_bytes"] = len(raw)
    result["context_budget"]["approx_tokens_div4"] = (len(raw) + 3) // 4
    return result


def build_context_projection(
    group_dir: Path,
    group: dict[str, Any],
    view: dict[str, Any] | None,
    context_members: list[dict[str, Any]],
    member_titles: dict[str, str],
) -> dict[str, Any]:
    context_policy = normalize_context_policy(group, view)
    policy = {
        "budget_bytes": int(context_policy["selected_budget_bytes"]),
        "max_entries": int(context_policy["max_scan_entries"]),
        "member_position_card_limit": int(
            group.get("member_position_card_limit", DEFAULT_MEMBER_POSITION_CARD_LIMIT)
        ),
    }
    unavailable = {
        "available": False,
        "label": "当前注入切片",
        "context_kind": "bounded_injection_slice",
        "context_is_complete_workgroup_memory": False,
        "notice": "尚未发现已物化的 view.json；完整工作组记忆仍不由本页代替。",
        "context_budget": {
            **policy,
            "final_bytes": 0,
            "approx_tokens_div4": 0,
            "budget_tiers": context_policy["budget_tiers"],
            "selected_budget_bytes": context_policy["selected_budget_bytes"],
            "budget_mode": context_policy["mode"],
            "adaptive": context_policy["adaptive"],
            "elbow": None,
            "doubling_marginal_gain": None,
        },
        "retrieval": {
            "full_visible_entry_count": 0,
            "included_entry_count": 0,
            "omitted_entry_count": 0,
            "member_position_card_count": 0,
            "member_position_card_limit": policy["member_position_card_limit"],
            "exact_entry_lookup_available": False,
            "exact_entry_lookup_command": "get-entry",
            "raw_event_archive_preserved": True,
        },
        "entries": [],
        "member_position_cards": [],
        "historical_diagnostic_evidence": [],
        "omitted_opinion_warning": False,
        "benefit_metrics": {
            "metrics_source": "compatibility_fallback",
            "curve_available": False,
        },
        "complete_workgroup_memory": {
            "available": False,
            "label": "完整工作组记忆",
            "injected": False,
            "retrieval_only": True,
            "source": "events.jsonl_or_archive",
        },
        "exact_entry_lookup": {
            "available": False,
            "endpoint": "/api/entry",
        },
    }
    if not isinstance(view, dict):
        return unavailable

    view_copy = dict(view)
    view_copy["members"] = context_members
    requested_scope = normalize_scope(group, view_copy)
    candidates = [
        member
        for member in context_members
        if member_is_active(member) and scope_matches(member, requested_scope)
    ]
    candidates.extend(
        member
        for member in context_members
        if member not in candidates and scope_matches(member, requested_scope)
    )
    if not candidates:
        candidates = context_members[:1]
    if not candidates:
        candidates = [
            {
                "member_id": "anonymous-reader",
                "role": "observer",
                "host_id": "",
                "thread_id": "",
                "read_scope": ["*"],
                "write_scope": [],
                "scopes": ["*"],
                "active": True,
                "status": "ACTIVE",
                "codex_task_title": "匿名只读观察者",
            }
        ]
    try:
        if callable(filtered_context):
            raw_context = filtered_context(view_copy, candidates[0], requested_scope)
        else:
            raw_context = fallback_filtered_context(
                view_copy, candidates[0], requested_scope
            )
    except (KeyError, TypeError, ValueError):
        # Older runtimes may not expose every member identity field required by
        # the core context builder.  Keep the frontend read-only and degrade
        # to the ranked projection instead of hiding the whole slice.
        raw_context = fallback_filtered_context(
            view_copy, candidates[0], requested_scope
        )

    source_entries = raw_context.get("entries", [])
    if isinstance(view, dict) and isinstance(view.get("entries"), list):
        summary_entries = view.get("entries", [])
    else:
        summary_entries = source_entries
    event_summary, event_meta = build_event_summary(
        group_dir,
        group,
        view,
        [item for item in summary_entries if isinstance(item, dict)],
    )
    safe_cards = [
        safe_card_for_frontend(card, member_titles)
        for card in raw_context.get("member_position_cards", [])
        if isinstance(card, dict)
    ]
    safe_entries = [
        safe_entry_for_frontend(
            entry,
            member_titles,
            event_meta.get(str(entry.get("entry_id") or "")),
        )
        for entry in source_entries
        if isinstance(entry, dict)
    ]
    raw_budget = raw_context.get("context_budget", {})
    raw_retrieval = raw_context.get("retrieval", {})
    retrieval = {
        "full_visible_entry_count": int(
            raw_retrieval.get("full_visible_entry_count", 0)
        ),
        "included_entry_count": int(
            raw_retrieval.get("included_entry_count", len(safe_entries))
        ),
        "omitted_entry_count": int(raw_retrieval.get("omitted_entry_count", 0)),
        "omitted_entry_id_sample": [
            str(entry_id)
            for entry_id in raw_retrieval.get("omitted_entry_id_sample", [])
            if entry_id
        ],
        "member_position_card_count": len(safe_cards),
        "member_position_card_limit": int(
            raw_retrieval.get(
                "member_position_card_limit",
                policy["member_position_card_limit"],
            )
        ),
        "exact_entry_lookup_available": True,
        "exact_entry_lookup_command": "get-entry",
        "raw_event_archive_preserved": bool(
            raw_retrieval.get("raw_event_archive_preserved", True)
        ),
        "scanned_candidate_count": int(
            raw_retrieval.get(
                "scanned_candidate_count",
                raw_retrieval.get("full_visible_entry_count", 0),
            )
        ),
        "core_event_count": event_summary["core_event_count"],
        "current_entry_count": event_summary["current_entry_count"],
    }
    # Anonymous fixtures may carry a precomputed backend curve so the public
    # screenshot can demonstrate a large-workgroup elbow without pretending
    # that five tiny fixture entries are a 176-entry production run.  Real
    # runtimes do not need this field: their filtered_context curve wins.
    display_context = raw_context
    demo_projection = (
        view.get("frontend_context_projection")
        if isinstance(view, dict)
        else None
    )
    display_curve: list[dict[str, Any]] = []
    if isinstance(demo_projection, dict):
        display_curve = normalize_benefit_curve(
            demo_projection.get("budget_curve")
            or demo_projection.get("benefit_curve")
        )
    if display_curve:
        display_context = dict(raw_context)
        display_budget = dict(raw_context.get("context_budget") or {})
        display_budget.update(
            {
                "budget_bytes": demo_projection.get(
                    "selected_budget_bytes",
                    context_policy["selected_budget_bytes"],
                ),
                "selected_budget_bytes": demo_projection.get(
                    "selected_budget_bytes",
                    context_policy["selected_budget_bytes"],
                ),
                "selected_reason": demo_projection.get("selected_reason"),
                "elbow_budget_bytes": demo_projection.get(
                    "elbow_budget_bytes"
                ),
                "doubling_marginal_gain": demo_projection.get(
                    "doubling_marginal_gain"
                ),
            }
        )
        display_context["context_budget"] = display_budget
        display_context["context_budget_curve"] = display_curve

    benefit = benefit_metrics(display_context, context_policy, retrieval)
    raw_budget = display_context.get("context_budget", {})
    if not isinstance(raw_budget, dict):
        raw_budget = {}
    budget_bytes = int(
        raw_budget.get("budget_bytes", context_policy["selected_budget_bytes"])
    )
    selected_budget_bytes = int(
        raw_budget.get("selected_budget_bytes", budget_bytes)
    )
    final_bytes = int(raw_budget.get("final_bytes", 0))
    approx_tokens = int(raw_budget.get("approx_tokens_div4", (final_bytes + 3) // 4))
    raw_curve = display_context.get("context_budget_curve")
    if not isinstance(raw_curve, list):
        raw_curve = []
    return {
        "available": True,
        "label": "当前注入切片",
        "context_kind": raw_context.get("context_kind", "bounded_injection_slice"),
        "context_is_complete_workgroup_memory": False,
        "notice": "这是当前注入切片，不是完整工作组记忆；遗漏条目仍保留在追加式归档中。",
        "requested_scope": requested_scope,
        "context_budget": {
            "budget_bytes": budget_bytes,
            "selected_budget_bytes": selected_budget_bytes,
            "final_bytes": final_bytes,
            "approx_tokens_div4": approx_tokens,
            "estimated_tokens": approx_tokens,
            "max_entries": int(raw_budget.get("max_entries", policy["max_entries"])),
            "policy": raw_budget.get("policy"),
            "budget_tiers": context_policy["budget_tiers"],
            "budget_mode": context_policy["mode"],
            "adaptive": context_policy["adaptive"],
            "minimum_budget_bytes": context_policy["minimum_budget_bytes"],
            "target_coverage": context_policy["target_coverage"],
            "minimum_marginal_gain": context_policy["minimum_marginal_gain"],
            "elbow": benefit.get("elbow"),
            "doubling_marginal_gain": benefit.get("doubling_marginal_gain"),
            "selected_reason": raw_budget.get(
                "selected_reason", context_policy.get("selected_reason")
            ),
            "budget_curve_source": (
                "anonymous_fixture_precomputed_backend_curve"
                if display_curve
                else benefit.get("curve_source")
            ),
        },
        "budget_curve": raw_curve or context_policy["benefit_curve"],
        "retrieval": retrieval,
        "event_summary": event_summary,
        "open_question_entry_ids": [
            str(item)
            for item in raw_context.get("open_question_entry_ids", [])
            if item
        ],
        "open_conflict_entry_ids": [
            str(item)
            for item in raw_context.get("open_conflict_entry_ids", [])
            if item
        ],
        "current_best_model_entry_ids": [
            str(item)
            for item in raw_context.get("current_best_model_entry_ids", [])
            if item
        ],
        "entries": safe_entries,
        "member_position_cards": safe_cards,
        "historical_diagnostic_evidence": [
            card for card in safe_cards if not card.get("member_active")
        ],
        "omitted_opinion_warning": retrieval["omitted_entry_count"] > 0,
        "benefit_metrics": benefit,
        "complete_workgroup_memory": {
            "available": True,
            "label": "完整工作组记忆",
            "injected": False,
            "retrieval_only": True,
            "source": "view.json + append-only events.jsonl",
        },
        "exact_entry_lookup": {
            "available": True,
            "endpoint": "/api/entry",
            "source": "exact_get_entry_projection_or_anonymous_fixture",
        },
    }


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
        "objective": compact_text(group.get("objective"), 220) or "未提供目标",
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

    empty_active_groups = [
        row
        for row in groups
        if row["active"] and row["active_member_count"] == 0
    ]
    visible_groups = [row for row in groups if row not in empty_active_groups]
    active_groups = [row for row in visible_groups if row["active"]]
    archived_groups = [row for row in visible_groups if not row["active"]]
    recent_groups = sorted(
        archived_groups,
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
        "schema_version": FRONTEND_STATUS_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "display_name": "工作组",
        "active_group_count": len(active_groups),
        "active_member_count": active_members,
        "role_counts": role_counts,
        "active_groups": active_groups,
        "recent_groups": recent_groups,
        "archived_group_count": len(archived_groups),
        "all_group_count": len(groups),
        "hidden_empty_group_count": len(empty_active_groups),
        "hidden_empty_groups": [
            {
                "group_id": row["group_id"],
                "display_title": row["display_title"],
                "state": row["state"],
                "reason": "ACTIVE_NO_ACTIVE_MEMBERS",
            }
            for row in empty_active_groups
        ],
        "runtime_available": runtime_root.is_dir(),
        "detail_endpoint": "/api/workgroup",
        "exact_entry_endpoint": "/api/entry",
        "context_semantics": {
            "current_injection_is_bounded_slice": True,
            "complete_workgroup_memory_is_retrieval_only": True,
            "raw_archive_is_append_only_source": True,
        },
        "privacy": {
            "internal_working_content_exposed": False,
            "host_or_thread_identity_exposed": False,
            "lease_token_exposed": False,
        },
    }


def anonymous_demo_runtime_root() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "frontend-demo" / "runtime"


def load_group_view(group_dir: Path) -> dict[str, Any] | None:
    view = load_optional_json(group_dir / "view.json")
    if view is not None:
        return view
    if callable(materialize_view):
        try:
            return materialize_view(group_dir)
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None
    return None


def group_detail_projection(
    runtime_root: Path,
    group_id: str,
    *,
    codex_state_db: Path = DEFAULT_CODEX_STATE_DB,
    title_map_path: Path | None = None,
) -> dict[str, Any] | None:
    group_dir = runtime_root / group_id
    group = load_optional_json(group_dir / "group.json")
    members_doc = load_optional_json(group_dir / "members.json")
    if group is None or members_doc is None:
        return None
    raw_members = members_doc.get("members")
    if not isinstance(raw_members, dict):
        raw_members = {}
    resolved_title_map = title_map_path or (runtime_root / "CODEX_THREAD_TITLE_MAP.json")
    title_map = load_codex_thread_titles(
        codex_state_db,
        [str(item.get("thread_id") or "") for item in raw_members.values() if isinstance(item, dict)],
        resolved_title_map,
    )
    context_members: list[dict[str, Any]] = []
    member_titles: dict[str, str] = {}
    for member_id, raw_member in raw_members.items():
        if not isinstance(raw_member, dict):
            continue
        title, _source = resolved_member_title(raw_member, title_map)
        member_titles[str(member_id)] = title
        context_members.append(
            context_member_copy(
                {**raw_member, "member_id": str(raw_member.get("member_id") or member_id)},
                title,
            )
        )

    group_row = safe_group_row(
        group_dir,
        codex_state_db=codex_state_db,
        title_map_path=resolved_title_map,
    )
    if group_row is None:
        return None
    view = load_group_view(group_dir)
    context = build_context_projection(
        group_dir,
        group,
        view,
        context_members,
        member_titles,
    )
    task_pool = normalize_task_pool(group_dir, group, member_titles)
    inactive_member_count = sum(
        1 for member in context_members if not member_is_active(member)
    )
    return {
        "schema_version": FRONTEND_STATUS_SCHEMA_VERSION,
        "group_id": group_row["group_id"],
        "group": group_row,
        "objective": group_row["objective"],
        "state": group_row["state"],
        "members": group_row["members"],
        "historical_member_count": inactive_member_count,
        "context": context,
        "task_pool": task_pool,
        "source": {
            "view": "view.json" if (group_dir / "view.json").is_file() else "materialized read-only view",
            "event_archive": "events.jsonl",
            "task_pool": task_pool.get("source"),
            "exact_entry": "get-entry-compatible read-only projection",
        },
        "privacy": {
            "raw_payload_exposed": False,
            "lease_tokens_exposed": False,
            "full_memory_injected": False,
        },
    }


def exact_entry_projection(
    runtime_root: Path,
    group_id: str,
    entry_id: str,
    *,
    codex_state_db: Path = DEFAULT_CODEX_STATE_DB,
    title_map_path: Path | None = None,
) -> dict[str, Any] | None:
    detail = group_detail_projection(
        runtime_root,
        group_id,
        codex_state_db=codex_state_db,
        title_map_path=title_map_path,
    )
    if detail is None:
        return None
    group_dir = runtime_root / group_id
    view = load_group_view(group_dir)
    if not isinstance(view, dict):
        return None
    entry = next(
        (
            item
            for item in view.get("entries", [])
            if isinstance(item, dict) and str(item.get("entry_id") or "") == entry_id
        ),
        None,
    )
    if entry is None:
        return None
    member_titles = {
        str(member.get("member_id")): str(member.get("conversation_title") or UNRESOLVED_TASK_TITLE)
        for member in detail.get("members", [])
        if member.get("member_id")
    }
    return {
        "schema_version": EXACT_ENTRY_SCHEMA_VERSION,
        "group_id": group_id,
        "entry_id": entry_id,
        "entry": exact_entry_for_frontend(entry, member_titles),
        "retrieval": "exact_get_entry_projection",
        "notice": "这是按引用读取的完整条目，不代表它会被全部注入当前模型上下文。",
        "privacy": {"lease_tokens_exposed": False, "raw_thread_identity_exposed": False},
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


def render_html_legacy() -> str:
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


def render_html() -> str:
    """Render the read-only workgroup diagnostic dashboard.

    The page deliberately asks the API for a bounded projection first.  Full
    entries are fetched only after the operator clicks an exact-entry action;
    the UI never treats the complete archive as the model injection context.
    """
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>临时工作组 · 只读诊断</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07111f;
      --panel: #0d1b2e;
      --panel-2: #101f35;
      --panel-3: #132641;
      --line: #284666;
      --line-soft: rgba(148, 184, 224, .18);
      --text: #e8f1ff;
      --muted: #94add0;
      --faint: #6e88ac;
      --blue: #78aaff;
      --green: #4be0a3;
      --amber: #f3c36d;
      --red: #ff7f87;
      --shadow: 0 22px 70px rgba(0, 0, 0, .28);
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      background: radial-gradient(circle at 82% -10%, rgba(72, 126, 214, .25), transparent 34%), var(--bg);
      color: var(--text);
    }
    button { font: inherit; }
    .app { min-height: 100vh; display: flex; flex-direction: column; }
    .topbar {
      display: flex; align-items: center; justify-content: space-between; gap: 18px;
      padding: 18px 24px; border-bottom: 1px solid var(--line-soft);
      background: rgba(7, 17, 31, .9); backdrop-filter: blur(10px);
      position: sticky; top: 0; z-index: 4;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-mark { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 11px; background: #7fa7ff; color: #081426; font-weight: 900; }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 18px; letter-spacing: .03em; }
    .subtitle { margin-top: 4px; color: var(--muted); font-size: 12px; }
    .connection { display: inline-flex; align-items: center; gap: 8px; color: var(--green); font-size: 12px; white-space: nowrap; }
    .connection::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 12px currentColor; }
    .connection.offline { color: var(--red); }
    .layout { flex: 1; min-height: 0; display: grid; grid-template-columns: 320px minmax(0, 1fr); }
    .sidebar { min-height: 0; display: flex; flex-direction: column; border-right: 1px solid var(--line-soft); background: rgba(9, 23, 40, .82); }
    .sidebar-head { padding: 18px 18px 12px; border-bottom: 1px solid var(--line-soft); }
    .sidebar-head h2 { font-size: 15px; }
    .sidebar-head p { margin-top: 5px; color: var(--muted); font-size: 12px; line-height: 1.55; }
    .group-list { min-height: 0; overflow: auto; padding: 12px; }
    .list-label { margin: 8px 6px 7px; color: var(--muted); font-size: 11px; font-weight: 800; letter-spacing: .08em; }
    .group-card {
      display: block; width: 100%; margin: 0 0 8px; padding: 13px 14px; text-align: left;
      color: var(--text); border: 1px solid var(--line-soft); border-radius: 12px;
      background: rgba(16, 31, 53, .7); cursor: pointer; transition: border-color .16s, background .16s, transform .16s;
    }
    .group-card:hover { border-color: #5c8dd8; background: var(--panel-3); transform: translateY(-1px); }
    .group-card.selected { border-color: var(--blue); background: rgba(50, 91, 154, .35); box-shadow: inset 3px 0 0 var(--blue); }
    .group-card.archived { opacity: .78; }
    .group-card-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; font-weight: 800; line-height: 1.45; }
    .group-card-meta { margin-top: 5px; color: var(--muted); font-size: 11px; line-height: 1.55; }
    .state-pill, .tag, .badge { display: inline-flex; align-items: center; border-radius: 999px; white-space: nowrap; }
    .state-pill { padding: 3px 7px; color: #b8ffe1; background: rgba(27, 135, 95, .3); font-size: 10px; font-weight: 800; }
    .state-pill.archived { color: #c5d0df; background: rgba(103, 123, 151, .24); }
    .empty { padding: 14px 7px; color: var(--faint); font-size: 12px; }
    .detail { min-width: 0; min-height: 0; max-height: calc(100vh - 71px); overflow: auto; padding: 22px 24px 42px; }
    .detail-inner { max-width: 1250px; margin: 0 auto; }
    .detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
    .detail-head h2 { font-size: 23px; line-height: 1.35; }
    .detail-head p { max-width: 760px; margin-top: 7px; color: var(--muted); font-size: 13px; line-height: 1.65; }
    .read-only { color: var(--amber); font-size: 11px; font-weight: 800; white-space: nowrap; }
    .section { margin-top: 17px; padding: 16px; border: 1px solid var(--line-soft); border-radius: 15px; background: rgba(13, 27, 46, .78); box-shadow: var(--shadow); }
    .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; font-size: 15px; }
    .section-title small { color: var(--muted); font-size: 11px; font-weight: 500; }
    .metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; }
    .metric { min-width: 0; padding: 11px 12px; border: 1px solid var(--line-soft); border-radius: 11px; background: rgba(7, 17, 31, .5); }
    .metric-label { color: var(--muted); font-size: 11px; line-height: 1.35; }
    .metric-value { margin-top: 5px; color: var(--text); font-size: 18px; font-weight: 850; font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; }
    .metric-note { margin-top: 3px; color: var(--faint); font-size: 10px; line-height: 1.35; }
    .context-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(260px, .8fr); gap: 12px; }
    .context-box { min-width: 0; padding: 14px; border: 1px solid #3f6fa8; border-radius: 13px; background: linear-gradient(145deg, rgba(28, 62, 107, .48), rgba(14, 29, 50, .78)); }
    .context-box.complete { border-color: rgba(243, 195, 109, .48); background: rgba(86, 63, 21, .22); }
    .context-box h3 { font-size: 14px; }
    .context-box p { margin-top: 6px; color: var(--muted); font-size: 12px; line-height: 1.55; }
    .big-number { margin-top: 13px; font-size: 23px; font-weight: 900; font-variant-numeric: tabular-nums; }
    .sub-number { margin-top: 3px; color: #b5cafa; font-size: 12px; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 11px; }
    .tag { padding: 4px 8px; color: #cce0ff; border: 1px solid rgba(120, 170, 255, .26); background: rgba(47, 89, 151, .25); font-size: 10px; }
    .tag.active { color: #e6efff; border-color: var(--blue); background: rgba(68, 124, 209, .45); }
    .tag.amber { color: #ffe5ad; border-color: rgba(243,195,109,.35); background: rgba(127, 89, 21, .28); }
    .tag.green { color: #b7ffdf; border-color: rgba(75,224,163,.34); background: rgba(20, 113, 83, .28); }
    .notice { margin-top: 12px; padding: 9px 11px; color: #cadbfa; border-left: 3px solid var(--blue); background: rgba(55, 97, 162, .2); font-size: 12px; line-height: 1.55; }
    .notice.warn { color: #ffe9bd; border-left-color: var(--amber); background: rgba(127, 89, 21, .22); }
    .two-col { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .scroll-box { max-height: 330px; overflow: auto; padding-right: 4px; }
    .task-card, .entry-card, .member-card { padding: 12px; border: 1px solid var(--line-soft); border-radius: 11px; background: rgba(16, 31, 53, .7); }
    .task-card + .task-card, .entry-card + .entry-card, .member-card + .member-card { margin-top: 8px; }
    .task-top, .entry-top, .member-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
    .mono { color: #a6c8ff; font: 11px/1.4 Consolas, "SFMono-Regular", monospace; word-break: break-word; }
    .task-title, .entry-title, .member-title { margin-top: 4px; color: var(--text); font-size: 13px; font-weight: 800; line-height: 1.5; }
    .task-detail, .entry-detail, .member-detail { margin-top: 5px; color: var(--muted); font-size: 11px; line-height: 1.6; }
    .task-card.claimed { border-color: rgba(75,224,163,.52); }
    .task-card.conflict { border-color: rgba(255,127,135,.7); }
    .badge { padding: 3px 7px; color: #c8d8f1; background: rgba(103, 123, 151, .24); font-size: 10px; font-weight: 800; }
    .badge.good { color: #b7ffdf; background: rgba(20, 113, 83, .35); }
    .badge.bad { color: #ffd0d3; background: rgba(137, 41, 57, .42); }
    .badge.warn { color: #ffe8b7; background: rgba(127, 89, 21, .35); }
    .badge + .badge { margin-left: 4px; }
    .member-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
    .member-card { min-width: 0; }
    .member-card.history { opacity: .82; border-color: rgba(243, 195, 109, .32); }
    .member-card .claim { margin-top: 10px; color: #dbe8ff; font-size: 12px; line-height: 1.55; }
    .label { color: var(--faint); }
    .ref-list { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
    .ref { padding: 3px 6px; color: #b8d1ff; border: 1px solid rgba(120,170,255,.2); border-radius: 6px; font: 10px Consolas, monospace; }
    .ghost-button { margin-top: 9px; padding: 5px 9px; color: #cfe0ff; border: 1px solid rgba(120,170,255,.36); border-radius: 7px; background: rgba(42, 79, 134, .35); cursor: pointer; font-size: 11px; }
    .ghost-button:hover { border-color: var(--blue); background: rgba(56, 105, 181, .55); }
    .exact-panel { margin-top: 12px; padding: 13px; border: 1px solid rgba(120,170,255,.42); border-radius: 11px; background: #091526; }
    .exact-panel pre { max-height: 300px; overflow: auto; margin: 10px 0 0; color: #dbe8ff; font: 11px/1.6 Consolas, "SFMono-Regular", monospace; white-space: pre-wrap; word-break: break-word; }
    .footer { padding: 10px 24px 14px; color: var(--faint); border-top: 1px solid var(--line-soft); font-size: 11px; }
    .loading { padding: 44px 18px; color: var(--muted); text-align: center; }
    @media (max-width: 1060px) { .layout { grid-template-columns: 270px minmax(0, 1fr); } .member-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { .layout { display: block; } .sidebar { max-height: 38vh; border-right: 0; border-bottom: 1px solid var(--line-soft); } .detail { max-height: none; } .context-grid, .two-col { grid-template-columns: 1fr; } .member-grid { grid-template-columns: 1fr; } .topbar { align-items: flex-start; flex-direction: column; } }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark">脑</div>
        <div><h1>临时工作组 · 只读诊断</h1><p class="subtitle">事件切片、成员观点与任务池投影；完整工作组记忆只按引用检索</p></div>
      </div>
      <div class="connection" id="connection">读取中</div>
    </header>
    <div class="layout">
      <aside class="sidebar">
        <div class="sidebar-head"><h2>工作组</h2><p>运行中的工作组在上方，已归档工作组用于历史诊断。点击左侧卡片查看详情。</p></div>
        <div class="group-list" id="group-list"><div class="loading">正在读取工作组……</div></div>
      </aside>
      <main class="detail" id="detail"><div class="detail-inner"><div class="loading">选择一个工作组以查看只读切片。</div></div></main>
    </div>
    <footer class="footer" id="updated">只读投影初始化中……</footer>
  </div>
  <script>
    const query = new URLSearchParams(window.location.search);
    const demoQuery = query.get('demo') === '1' ? '&demo=1' : '';
    let statusData = null;
    let selectedGroupId = null;
    let detailRequestSerial = 0;
    let currentDetail = null;
    let showAllEvents = false;
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const text = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : String(value);
    const apiUrl = (path, params = {}) => {
      const search = new URLSearchParams(params);
      if (demoQuery) search.set('demo', '1');
      const suffix = search.toString();
      return `${path}${suffix ? '?' + suffix : ''}`;
    };
    const bytes = (value) => {
      const number = Number(value);
      if (!Number.isFinite(number) || number <= 0) return '—';
      if (number >= 1024 * 1024) return `${(number / 1024 / 1024).toFixed(1)} MB`;
      if (number >= 1024) return `${Math.round(number / 1024)} KB`;
      return `${number} B`;
    };
    const percent = (value) => {
      const number = Number(value);
      return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '—';
    };
    const gain = (value) => {
      const number = Number(value);
      return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '—';
    };
    const badgeClass = (value) => {
      const lower = String(value || '').toLowerCase();
      if (lower.includes('不合规') || lower.includes('冲突') || lower.includes('失败')) return 'bad';
      if (lower.includes('无最终') || lower.includes('未提供') || lower.includes('未决')) return 'warn';
      if (lower.includes('有效') || lower.includes('合规') || lower.includes('一对一')) return 'good';
      return '';
    };
    const badge = (value) => `<span class="badge ${badgeClass(value)}">${escapeHtml(text(value))}</span>`;
    const formatList = (value) => Array.isArray(value) ? (value.length ? value.map((item) => text(item)).join('、') : '无') : text(value);
    const renderGroupCard = (group, archived) => {
      const selected = group.group_id === selectedGroupId ? ' selected' : '';
      const state = archived ? '已归档' : (group.state === 'ACTIVE' ? '运行中' : text(group.state));
      return `<button class="group-card ${archived ? 'archived' : ''}${selected}" data-group-id="${escapeHtml(group.group_id)}">
        <div class="group-card-title"><span>${escapeHtml(text(group.display_title, '工作组'))}</span><span class="state-pill ${archived ? 'archived' : ''}">${escapeHtml(state)}</span></div>
        <div class="group-card-meta">${escapeHtml(text(group.objective, '未提供目标'))}</div>
        <div class="group-card-meta">${archived ? '历史诊断 · ' + escapeHtml(text(group.closed_at)) : '活跃成员 ' + escapeHtml(group.active_member_count) + ' · ' + escapeHtml(text(group.task_id))}</div>
      </button>`;
    };
    function renderGroupList() {
      const root = document.querySelector('#group-list');
      if (!statusData) return;
      const active = statusData.active_groups || [];
      const archived = statusData.recent_groups || [];
      root.innerHTML = `<div class="list-label">运行中的工作组 · ${active.length}</div>${active.length ? active.map((item) => renderGroupCard(item, false)).join('') : '<div class="empty">当前没有运行中的工作组。</div>'}
        <div class="list-label">已归档的工作组 · ${archived.length}</div>${archived.length ? archived.map((item) => renderGroupCard(item, true)).join('') : '<div class="empty">暂无已归档的工作组。</div>'}`;
    }
    const metric = (label, value, note = '') => `<div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(text(value))}</div>${note ? `<div class="metric-note">${escapeHtml(note)}</div>` : ''}</div>`;
    const refs = (values) => {
      const list = Array.isArray(values) ? values : [];
      return list.length ? `<div class="ref-list">${list.map((item) => `<span class="ref">${escapeHtml(item)}</span>`).join('')}</div>` : '<div class="task-detail">暂无引用</div>';
    };
    const renderBudget = (context) => {
      const budget = context.context_budget || {};
      const metrics = context.benefit_metrics || {};
      const tiers = Array.isArray(budget.budget_tiers) ? budget.budget_tiers : [];
      const selected = Number(budget.selected_budget_bytes || budget.budget_bytes || 0);
      const elbow = budget.elbow || metrics.elbow;
      const curve = Array.isArray(context.budget_curve) ? context.budget_curve : [];
      const reasonLabels = {
        target_coverage_and_marginal_gain_elbow_reached: '达到目标覆盖并遇到边际收益拐点',
        target_coverage_reached: '达到目标覆盖',
        marginal_gain_elbow_reached: '达到边际收益拐点',
        fixed_budget_requested: '固定预算',
        maximum_budget_reached_before_elbow: '达到最大预算仍未遇到拐点'
      };
      const reason = reasonLabels[budget.selected_reason] || text(budget.selected_reason, '未提供');
      const curveText = curve.length ? curve.map((row) => `${bytes(row.budget_bytes)} ${percent(row.coverage_score || row.coverage_ratio)}`).join(' · ') : '暂无实测曲线';
      return `<div class="context-grid">
        <div class="context-box">
          <h3>当前注入切片</h3>
          <p>本轮实际下发给 Agent 的有界投影；它不是完整工作组记忆。</p>
          <div class="big-number">${escapeHtml(bytes(budget.final_bytes))}</div>
          <div class="sub-number">粗略 tokens ≈ ${escapeHtml(text(budget.estimated_tokens || budget.approx_tokens_div4))} · 预算档位 ${escapeHtml(bytes(selected))}</div>
          <div class="chips">${tiers.map((tier) => `<span class="tag ${Number(tier.budget_bytes) === selected ? 'active' : ''}">${escapeHtml(bytes(tier.budget_bytes))}</span>`).join('')}</div>
          <div class="notice">自适应：${escapeHtml(text(budget.budget_mode, 'compatibility_fallback'))} · 当前 elbow：${escapeHtml(bytes(elbow))} · 再翻倍边际增益：${escapeHtml(gain(budget.doubling_marginal_gain ?? metrics.doubling_marginal_gain))} · 选择原因：${escapeHtml(reason)}</div>
          <div class="metric-note" style="margin-top:10px">预算曲线：${escapeHtml(curveText)}</div>
        </div>
        <div class="context-box complete">
          <h3>完整工作组记忆</h3>
          <p>原始事件/归档和完整观点只作为检索源，不会被整包注入模型上下文。</p>
          <div class="big-number">${escapeHtml(text(context.complete_workgroup_memory?.retrieval_only ? '仅检索' : '未提供'))}</div>
          <div class="sub-number">当前注入：否 · 原始归档：保留</div>
          <div class="notice warn">点击“查看完整条目”时才按 entry 引用读取，读取结果用于诊断，不改变当前注入切片。</div>
        </div>
      </div>`;
    };
    const renderTask = (item, claimed) => {
      const conflict = claimed && statusData && false;
      const owner = item.claimed_by_task_title ? `成员 / Codex 任务：${item.claimed_by_task_title}` : '暂无 owner';
      const timing = item.claimed_at || item.created_at || item.updated_at;
      return `<article class="task-card ${claimed ? 'claimed' : ''} ${conflict ? 'conflict' : ''}">
        <div class="task-top"><div class="mono">${escapeHtml(text(item.task_id))}</div>${badge(claimed ? '已领取' : '待领取')}</div>
        <div class="task-title">${escapeHtml(text(item.title, '未命名任务'))}</div>
        <div class="task-detail">${escapeHtml(claimed ? owner : '可由符合角色的成员领取')}</div>
        <div class="task-detail">优先级：${escapeHtml(text(item.priority, '未提供'))} · 依赖：${escapeHtml(formatList(item.dependencies))} · 所需角色：${escapeHtml(text(item.required_role, '未指定'))}</div>
        <div class="task-detail">${claimed ? '领取/更新时间' : '创建时间'}：${escapeHtml(text(timing))} · 状态：${escapeHtml(text(item.raw_status || item.assignment_status))}</div>
        ${item.evidence_ref ? `<div class="task-detail">证据入口：${escapeHtml(item.evidence_ref)}</div>` : ''}
      </article>`;
    };
    const renderTaskPool = (pool) => {
      const waiting = pool.waiting || [];
      const claimed = pool.claimed || [];
      const conflict = Number(pool.conflict_count || 0);
      return `<section class="section"><div class="section-title"><span>任务池</span><small>只读投影 · ${escapeHtml(text(pool.policy_label, '一人一任务'))}</small></div>
        <div class="metric-grid">${metric('待领取', pool.waiting_count)}${metric('已领取', pool.claimed_count)}${metric('一人一任务', pool.one_person_one_task ? '启用' : '未声明')}${metric('冲突', conflict, conflict ? '需要上游处理' : '当前未发现')}</div>
        ${conflict ? `<div class="notice warn">${escapeHtml(JSON.stringify(pool.member_conflicts || pool.task_conflicts))}</div>` : ''}
        <div class="two-col" style="margin-top:12px"><div><h3 class="section-title">待领取 <small>${waiting.length} 项</small></h3><div class="scroll-box">${waiting.length ? waiting.map((item) => renderTask(item, false)).join('') : '<div class="empty">暂无待领取任务。</div>'}</div></div><div><h3 class="section-title">已领取 · 任务 ↔ Codex 任务 <small>${claimed.length} 项</small></h3><div class="scroll-box">${claimed.length ? claimed.map((item) => renderTask(item, true)).join('') : '<div class="empty">暂无已领取任务。</div>'}</div></div></div>
      </section>`;
    };
    const renderMember = (member) => `<article class="member-card"><div class="member-top"><div><div class="member-title">${escapeHtml(text(member.conversation_title, '任务名称待同步'))}</div><div class="member-detail">${escapeHtml(text(member.role))} · ${escapeHtml(text(member.status))}</div></div>${badge(member.is_controller ? '总控' : '活跃')}</div><div class="member-detail">当前活动成员；任务名称来自 Codex 任务标题适配器。</div></article>`;
    const renderCard = (card, history) => {
      const badges = card.badges || {};
      return `<article class="member-card ${history ? 'history' : ''}">
        <div class="member-top"><div><div class="member-title">${escapeHtml(text(card.codex_task_title, '任务名称待同步'))}</div><div class="member-detail">${history ? '历史诊断证据 · 已退出成员' : '当前工作组成员'} · scope=${escapeHtml(text(card.scope))}</div></div>${badge(history ? '历史只读' : '观点卡')}</div>
        <div class="claim"><span class="label">核心论点：</span>${escapeHtml(text(card.core_claim, '未提供'))}</div>
        <div class="member-detail"><span class="label">最强证据：</span>${escapeHtml(text(card.strongest_evidence, '未提供'))}</div>
        <div class="member-detail"><span class="label">最强反证/质疑：</span>${escapeHtml(text(card.strongest_counterevidence, '未提供'))}</div>
        <div class="member-detail"><span class="label">claim ceiling：</span>${escapeHtml(text(card.claim_ceiling, '未提供'))}</div>
        <div class="chips">${badge(badges.evidence)}${badge(badges.model_gate)}${badge(badges.signing)}</div>
        ${refs(card.evidence_refs)}
        ${card.source_entry_id ? `<button class="ghost-button" data-entry-id="${escapeHtml(card.source_entry_id)}">查看完整观点 / get-entry</button>` : ''}
      </article>`;
    };
    const renderEntries = (context) => {
      const allEntries = context.entries || [];
      const summary = context.event_summary || {};
      const entries = showAllEvents ? allEntries : allEntries.filter((entry) => entry.is_core_event !== false);
      const filterLabel = showAllEvents ? '全部流水' : '当前核心事件';
      const toggleLabel = showAllEvents ? '只看核心事件' : '查看全部流水';
      const eventCards = entries.map((entry) => {
        const globalSeq = entry.global_event_seq ?? entry.entry_seq;
        const coreSeq = entry.core_event_seq == null ? '—' : `${entry.core_event_seq}/${entry.core_event_count || summary.core_event_count || '—'}`;
        const seqLabel = showAllEvents ? `工作组总流水号 #${globalSeq} · 核心局部序号 ${coreSeq}` : `当前核心事件 ${coreSeq} · 工作组总流水号 #${globalSeq}`;
        return `<article class="entry-card"><div class="entry-top"><div><div class="mono">${escapeHtml(seqLabel)}</div><div class="entry-title">${escapeHtml(text(entry.entry_type))} · ${escapeHtml(text(entry.event_category))}</div></div>${badge(text(entry.status, 'active'))}</div><div class="entry-detail">${escapeHtml(text(entry.author_task_title))} · ${escapeHtml(text(entry.content_preview, '无摘要'))}</div><div class="entry-detail">主题：${escapeHtml(text(entry.subject_key))} · 时间：${escapeHtml(text(entry.created_at))} · 置信度：${escapeHtml(text(entry.confidence))}</div>${refs(entry.evidence_refs)}<button class="ghost-button" data-entry-id="${escapeHtml(entry.entry_id)}">查看完整条目</button></article>`;
      }).join('');
      return `<section class="section"><div class="section-title"><span>${filterLabel}</span><div><small>总流水 ${escapeHtml(summary.total_stream_count)} · 核心 ${escapeHtml(summary.core_event_count)} · 真实效果 ${escapeHtml(summary.real_effect_count)} · 证据 ${escapeHtml(summary.evidence_count)}</small><button class="ghost-button" data-event-toggle="1">${toggleLabel}</button></div></div>
        <div class="notice">${escapeHtml(text(summary.core_filter_notice, '默认只展示会影响任务裁决或真实效果的核心事件。'))}</div>
        <div class="scroll-box">${eventCards || '<div class="empty">当前筛选没有事件；可切换“查看全部流水”。</div>'}</div>
        <div id="exact-entry-panel" class="exact-panel" hidden></div>
      </section>`;
    };
    function renderDetail(detail) {
      currentDetail = detail;
      const group = detail.group || {};
      const context = detail.context || {};
      const retrieval = context.retrieval || {};
      const metrics = context.benefit_metrics || {};
      const cards = context.member_position_cards || [];
      const activeCards = cards.filter((card) => card.member_active);
      const historyCards = (context.historical_diagnostic_evidence || cards.filter((card) => !card.member_active));
      const html = `<div class="detail-inner">
        <div class="detail-head"><div><h2>${escapeHtml(text(group.display_title, '工作组'))}</h2><p>${escapeHtml(text(detail.objective, '未提供目标'))}</p></div><div class="read-only">只读 · ${escapeHtml(text(detail.state))}</div></div>
        <section class="section"><div class="section-title"><span>运行摘要</span><small>来源：工作组 runtime 只读适配器</small></div><div class="metric-grid">${metric('活动成员', group.active_member_count)}${metric('工作组总流水', context.event_summary?.total_stream_count, 'append-only 全局序号不重写')}${metric('当前核心事件', context.event_summary?.core_event_count, `真实效果 ${context.event_summary?.real_effect_count ?? '—'}`)}${metric('证据事件', context.event_summary?.evidence_count, '当前工作组筛选')}</div></section>
        <section class="section"><div class="section-title"><span>上下文边界</span><small>context_kind=${escapeHtml(text(context.context_kind))}</small></div>${renderBudget(context)}<div class="metric-grid" style="margin-top:12px">${metric('原始候选', retrieval.full_visible_entry_count)}${metric('遗漏条目', retrieval.omitted_entry_count)}${metric('遗漏成员观点', metrics.omitted_member_opinion_count ?? 0)}${metric('重复率', percent(metrics.duplicate_rate))}${metric('检索补充', metrics.retrieval_supplement_count ?? 0)}${metric('检索延迟', metrics.latency_ms == null ? '—' : `${metrics.latency_ms} ms`)}${metric('开放冲突', (context.open_conflict_entry_ids || []).length)}${metric('开放问题', (context.open_question_entry_ids || []).length)}</div>${retrieval.omitted_entry_count ? '<div class="notice warn">当前切片存在截断；遗漏条目仍可通过精确引用检索，不等于丢失原始记忆。</div>' : ''}</section>
        ${renderTaskPool(detail.task_pool || {})}
        <section class="section"><div class="section-title"><span>当前成员观点卡 <small>member_position_cards</small></span><small>${activeCards.length} 名活动成员 · 已退出成员不占活动位</small></div><div class="member-grid">${activeCards.length ? activeCards.map((card) => renderCard(card, false)).join('') : '<div class="empty">当前没有可展示的活动成员观点卡。</div>'}</div></section>
        <section class="section"><div class="section-title"><span>当前工作组成员</span><small>实际 Codex 任务名称</small></div><div class="member-grid">${(group.members || []).length ? group.members.map(renderMember).join('') : '<div class="empty">暂无活动成员。</div>'}</div></section>
        ${renderEntries(context)}
        <details class="section"><summary class="section-title"><span>历史诊断证据区</span><small>${historyCards.length} 张历史观点卡 · 不参与当前 claim</small></summary><div class="member-grid" style="margin-top:12px">${historyCards.length ? historyCards.map((card) => renderCard(card, true)).join('') : '<div class="empty">暂无已退出成员的历史诊断证据。</div>'}</div></details>
        <section class="section"><div class="section-title"><span>来源与边界</span><small>不改变 project truth</small></div><div class="notice">${escapeHtml(text(context.notice))}<br>当前页只读；不会自动 claim、不会重启任务、不会把历史成员重新放回活动成员列表。</div></section>
      </div>`;
      document.querySelector('#detail').innerHTML = html;
    }
    async function loadExact(entryId) {
      const panel = document.querySelector('#exact-entry-panel');
      if (!panel || !selectedGroupId) return;
      panel.hidden = false;
      panel.innerHTML = '<div class="task-detail">正在按 entry 引用读取完整条目……</div>';
      try {
        const response = await fetch(apiUrl('/api/entry', {group_id: selectedGroupId, entry_id: entryId}), {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        panel.innerHTML = `<div class="section-title"><span>精确条目：${escapeHtml(entryId)}</span><small>get-entry 只读返回</small></div><div class="notice">${escapeHtml(text(payload.notice))}</div><pre>${escapeHtml(JSON.stringify(payload.entry?.exact_content ?? payload.entry, null, 2))}</pre>${refs(payload.entry?.evidence_refs || [])}`;
      } catch (error) {
        panel.innerHTML = `<div class="notice warn">精确条目读取失败：${escapeHtml(error.message || error)}</div>`;
      }
    }
    async function loadDetail(groupId) {
      selectedGroupId = groupId;
      renderGroupList();
      const serial = ++detailRequestSerial;
      document.querySelector('#detail').innerHTML = '<div class="detail-inner"><div class="loading">正在读取工作组详情……</div></div>';
      try {
        const response = await fetch(apiUrl('/api/workgroup', {group_id: groupId}), {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (serial !== detailRequestSerial) return;
        renderDetail(payload);
      } catch (error) {
        if (serial !== detailRequestSerial) return;
        document.querySelector('#detail').innerHTML = `<div class="detail-inner"><div class="notice warn">工作组详情读取失败：${escapeHtml(error.message || error)}</div></div>`;
      }
    }
    async function refreshStatus() {
      try {
        const response = await fetch(apiUrl('/api/status'), {cache: 'no-store'});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        statusData = await response.json();
        document.querySelector('#connection').textContent = statusData.demo_mode ? '匿名演示数据' : '实时只读';
        document.querySelector('#connection').classList.remove('offline');
        renderGroupList();
        const all = [...(statusData.active_groups || []), ...(statusData.recent_groups || [])];
        if (!selectedGroupId || !all.some((group) => group.group_id === selectedGroupId)) {
          const first = (statusData.active_groups || [])[0] || (statusData.recent_groups || [])[0];
          if (first) await loadDetail(first.group_id);
        }
        document.querySelector('#updated').textContent = `更新时间：${text(statusData.generated_at)} · ${statusData.privacy?.internal_working_content_exposed ? '内部内容可见' : '不暴露内部工作内容'}`;
      } catch (error) {
        document.querySelector('#connection').textContent = '连接中断';
        document.querySelector('#connection').classList.add('offline');
        document.querySelector('#updated').textContent = String(error.message || error);
      }
    }
    document.addEventListener('click', (event) => {
      const groupButton = event.target.closest('[data-group-id]');
      if (groupButton) { loadDetail(groupButton.dataset.groupId); return; }
      const toggleButton = event.target.closest('[data-event-toggle]');
      if (toggleButton) { showAllEvents = !showAllEvents; if (currentDetail) renderDetail(currentDetail); return; }
      const entryButton = event.target.closest('[data-entry-id]');
      if (entryButton) { loadExact(entryButton.dataset.entryId); }
    });
    refreshStatus();
    setInterval(refreshStatus, 5000);
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
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        demo_mode = (query.get("demo") or ["0"])[0] == "1"
        runtime_root = (
            anonymous_demo_runtime_root() if demo_mode else self.runtime_root
        )
        title_map_path = (
            runtime_root / "CODEX_THREAD_TITLE_MAP.json"
            if demo_mode
            else self.title_map_path
        )
        status = build_workgroup_status(
            runtime_root,
            codex_state_db=self.codex_state_db,
            title_map_path=title_map_path,
        )
        status["demo_mode"] = demo_mode
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
        if path == "/api/workgroup":
            group_id = str((query.get("group_id") or [""])[0]).strip()
            detail = group_detail_projection(
                runtime_root,
                group_id,
                codex_state_db=self.codex_state_db,
                title_map_path=title_map_path,
            ) if group_id else None
            if detail is None:
                self.send_payload(
                    b'{"ok":false,"error":"workgroup_not_found"}\n',
                    content_type="application/json; charset=utf-8",
                    status_code=404,
                )
                return
            self.send_payload(
                (json.dumps(detail, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
                content_type="application/json; charset=utf-8",
            )
            return
        if path == "/api/entry":
            group_id = str((query.get("group_id") or [""])[0]).strip()
            entry_id = str((query.get("entry_id") or [""])[0]).strip()
            entry = exact_entry_projection(
                runtime_root,
                group_id,
                entry_id,
                codex_state_db=self.codex_state_db,
                title_map_path=title_map_path,
            ) if group_id and entry_id else None
            if entry is None:
                self.send_payload(
                    b'{"ok":false,"error":"entry_not_found"}\n',
                    content_type="application/json; charset=utf-8",
                    status_code=404,
                )
                return
            self.send_payload(
                (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode(
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
