#!/usr/bin/env python3
"""Project-owned, task-scoped shared working memory for coding-agent workgroups.

This is deliberately not a project-control writer and not a long-term-memory
backend.  Every mutable group lives below an explicitly supplied private
runtime root.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "agent_brain_workgroup_runtime_v1"
COORDINATION_SCHEMA_VERSION = "agent_brain_workgroup_coordination_v1"
COORDINATION_MODES = {"strict", "legacy"}
ROLES = {"controller", "worker", "reviewer", "observer"}
ENTRY_TYPES = {
    "FACT_CONFIRMED",
    "HYPOTHESIS",
    "HYPOTHESIS_REJECTED",
    "PARTIAL_RESULT",
    "ARTIFACT_PUBLISHED",
    "EVIDENCE_ATTACHED",
    "QUESTION_OPENED",
    "QUESTION_RESOLVED",
    "CONFLICT_RECORDED",
    "LOCAL_DECISION",
    "SCOPE_WARNING",
    "HANDOFF_READY",
    "CURRENT_BEST_MODEL",
}
POST_ROLE_ALLOW = {
    "controller": ENTRY_TYPES,
    "worker": {
        "FACT_CONFIRMED",
        "HYPOTHESIS",
        "PARTIAL_RESULT",
        "ARTIFACT_PUBLISHED",
        "EVIDENCE_ATTACHED",
        "QUESTION_OPENED",
        "SCOPE_WARNING",
        "CURRENT_BEST_MODEL",
    },
    "reviewer": {
        "FACT_CONFIRMED",
        "HYPOTHESIS",
        "HYPOTHESIS_REJECTED",
        "PARTIAL_RESULT",
        "ARTIFACT_PUBLISHED",
        "EVIDENCE_ATTACHED",
        "QUESTION_OPENED",
        "QUESTION_RESOLVED",
        "CONFLICT_RECORDED",
        "SCOPE_WARNING",
        "CURRENT_BEST_MODEL",
    },
    "observer": set(),
}
WRITABLE_STATES = {"MEMBERS_BOUND", "ACTIVE"}
READABLE_STATES = {
    "MEMBERS_BOUND",
    "ACTIVE",
    "FREEZING",
    "HANDOFF_READY",
    "RECONCILED",
}
CONTROL_EVENT_TYPES = {
    "GROUP_CREATED",
    "MEMBER_ADDED",
    "MEMBER_REMOVED",
    "ENTRY_POSTED",
    "ENTRY_RESOLVED",
    "GROUP_FROZEN",
    "HANDOFF_CREATED",
    "GROUP_CLOSED",
}


class BrainError(RuntimeError):
    """Closed-world runtime error returned as structured JSON."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BrainError("NOT_FOUND", f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BrainError("INVALID_JSON", f"Invalid JSON: {path}: {exc}") from exc


@contextmanager
def group_lock(group_dir: Path, timeout_seconds: float = 15.0):
    lock_path = group_dir / ".write.lock"
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            os.write(
                descriptor,
                canonical_bytes({"pid": os.getpid(), "at": now_iso()}),
            )
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > 120:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise BrainError("LOCK_TIMEOUT", f"Cannot acquire {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def group_dir_for(root: Path, group_id: str) -> Path:
    if not group_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in group_id):
        raise BrainError(
            "INVALID_GROUP_ID",
            "group_id may contain only lowercase letters, digits, '-' and '_'",
        )
    result = (root / group_id).resolve()
    root_resolved = root.resolve()
    if root_resolved != result.parent:
        raise BrainError("INVALID_GROUP_PATH", "group must be a direct child of root")
    return result


def find_active_identity_membership(
    root: Path,
    *,
    host_id: str,
    thread_id: str,
    exclude_group_id: str | None = None,
) -> str | None:
    """Return another writable group that already owns this task identity."""
    if not root.exists():
        return None
    for candidate in sorted(root.iterdir(), key=lambda item: item.name):
        if not candidate.is_dir() or candidate.name == exclude_group_id:
            continue
        meta_path = candidate / "group.json"
        members_path = candidate / "members.json"
        if not meta_path.exists() or not members_path.exists():
            continue
        try:
            meta = read_json(meta_path)
            members_doc = read_json(members_path)
        except BrainError:
            continue
        if meta.get("state") not in WRITABLE_STATES:
            continue
        for member in members_doc.get("members", {}).values():
            if (
                member.get("active") is True
                and member.get("host_id") == host_id
                and member.get("thread_id") == thread_id
            ):
                return str(meta.get("group_id", candidate.name))
    return None


def coordination_document(
    group_id: str,
    mode: str,
    member_ids: Iterable[str],
) -> dict[str, Any]:
    return {
        "schema_version": COORDINATION_SCHEMA_VERSION,
        "group_id": group_id,
        "mode": mode,
        "members": {
            member_id: {
                "last_context_view_version": None,
                "last_context_at": None,
                "last_context_scope": None,
            }
            for member_id in sorted(member_ids)
        },
    }


def read_coordination(
    group_dir: Path,
    meta: dict[str, Any],
    members_doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = group_dir / "coordination.json"
    if path.exists():
        return read_json(path)
    members_doc = members_doc or read_json(group_dir / "members.json")
    # Existing v0.1 groups remain readable as legacy groups. New groups always
    # create this document explicitly and default to strict coordination.
    return coordination_document(
        meta["group_id"],
        meta.get("coordination_mode", "legacy"),
        members_doc.get("members", {}),
    )


def scopes_overlap(left: str | None, right: str) -> bool:
    if left is None:
        return False
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def require_fresh_context(
    group_dir: Path,
    *,
    meta: dict[str, Any],
    member: dict[str, Any],
    expected_view_version: int | None,
    requested_scope: str,
) -> dict[str, Any]:
    """Fail closed when a strict-group write is based on stale shared state."""
    current_view = materialize_view(group_dir)
    if meta.get("coordination_mode", "legacy") != "strict":
        return current_view
    if expected_view_version is None:
        raise BrainError(
            "CONTEXT_VERSION_REQUIRED",
            "strict coordination requires --expected-view-version from context",
        )
    coordination = read_coordination(group_dir, meta)
    member_state = coordination.get("members", {}).get(member["member_id"], {})
    last_context_version = member_state.get("last_context_view_version")
    if last_context_version is None:
        raise BrainError(
            "CONTEXT_NOT_SYNCED",
            f"{member['member_id']} must call context before publishing",
        )
    if not scopes_overlap(member_state.get("last_context_scope"), requested_scope):
        raise BrainError(
            "CONTEXT_SCOPE_NOT_SYNCED",
            f"context scope {member_state.get('last_context_scope')!r} "
            f"does not cover {requested_scope!r}",
        )
    if (
        expected_view_version != current_view["view_version"]
        or last_context_version != expected_view_version
    ):
        raise BrainError(
            "CONTEXT_STALE",
            (
                f"expected={expected_view_version}; "
                f"last_context={last_context_version}; "
                f"current={current_view['view_version']}; reread context"
            ),
        )
    return current_view


def read_events(group_dir: Path) -> list[dict[str, Any]]:
    path = group_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    previous = "GENESIS"
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                raise BrainError(
                    "EVENT_LEDGER_BLANK_LINE", f"Blank event line {line_number}"
                )
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BrainError(
                    "EVENT_LEDGER_INVALID_JSON",
                    f"Invalid event line {line_number}: {exc}",
                ) from exc
            expected_seq = len(events) + 1
            if event.get("seq") != expected_seq:
                raise BrainError(
                    "EVENT_SEQUENCE_BREAK",
                    f"Expected seq {expected_seq}, got {event.get('seq')}",
                )
            if event.get("prev_event_hash") != previous:
                raise BrainError(
                    "EVENT_CHAIN_BREAK", f"Broken prev hash at seq {expected_seq}"
                )
            claimed = event.get("event_hash")
            unhashed = {key: value for key, value in event.items() if key != "event_hash"}
            actual = semantic_hash(unhashed)
            if not isinstance(claimed, str) or not hmac.compare_digest(claimed, actual):
                raise BrainError(
                    "EVENT_HASH_MISMATCH", f"Event hash mismatch at seq {expected_seq}"
                )
            if event.get("event_type") not in CONTROL_EVENT_TYPES:
                raise BrainError(
                    "EVENT_TYPE_INVALID",
                    f"Unsupported control event {event.get('event_type')}",
                )
            previous = claimed
            events.append(event)
    return events


def append_event_locked(
    group_dir: Path,
    event_type: str,
    actor_member_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if event_type not in CONTROL_EVENT_TYPES:
        raise BrainError("EVENT_TYPE_INVALID", event_type)
    events = read_events(group_dir)
    event = {
        "seq": len(events) + 1,
        "event_id": f"wgb:{uuid.uuid4().hex}",
        "event_type": event_type,
        "at": now_iso(),
        "actor_member_id": actor_member_id,
        "payload": payload,
        "prev_event_hash": events[-1]["event_hash"] if events else "GENESIS",
    }
    event["event_hash"] = semantic_hash(event)
    with (group_dir / "events.jsonl").open(
        "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(canonical_bytes(event).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def public_member(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "member_id": member["member_id"],
        "role": member["role"],
        "host_id": member["host_id"],
        "thread_id": member["thread_id"],
        "scopes": member["scopes"],
        "read_scope": member["read_scope"],
        "write_scope": member["write_scope"],
        "active": member["active"],
        "status": member["status"],
        "added_at": member["added_at"],
        "joined_at": member["joined_at"],
        "lease_expires_at": member["lease_expires_at"],
        "revoked_at": member.get("revoked_at"),
    }


def sync_member_registry_hash(group_dir: Path) -> str:
    members_doc = read_json(group_dir / "members.json")
    registry_hash = semantic_hash(members_doc)
    meta = read_json(group_dir / "group.json")
    meta["member_registry_sha256"] = registry_hash
    atomic_write_json(group_dir / "group.json", meta)
    return registry_hash


def materialize_view(group_dir: Path) -> dict[str, Any]:
    meta = read_json(group_dir / "group.json")
    members_doc = read_json(group_dir / "members.json")
    events = read_events(group_dir)
    entries: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event_type"] == "ENTRY_POSTED":
            entry = event["payload"]["entry"]
            entries[entry["entry_id"]] = dict(entry)
        elif event["event_type"] == "ENTRY_RESOLVED":
            target = event["payload"]["target_entry_id"]
            resolutions[target] = {
                "status": event["payload"]["status"],
                "resolution": event["payload"]["resolution"],
                "resolved_by": event["actor_member_id"],
                "resolved_at": event["at"],
                "resolution_event_id": event["event_id"],
            }
    for entry_id, resolution in resolutions.items():
        if entry_id in entries:
            entries[entry_id]["resolution"] = resolution
            entries[entry_id]["status"] = resolution["status"]
    ordered_entries = sorted(entries.values(), key=lambda item: item["entry_seq"])
    open_questions = [
        item["entry_id"]
        for item in ordered_entries
        if item["entry_type"] == "QUESTION_OPENED" and item["status"] == "active"
    ]
    open_conflicts = [
        item["entry_id"]
        for item in ordered_entries
        if item["entry_type"] == "CONFLICT_RECORDED" and item["status"] == "active"
    ]
    current_best = [
        item["entry_id"]
        for item in ordered_entries
        if item["entry_type"] == "CURRENT_BEST_MODEL"
        and item["status"] == "active"
    ]
    view = {
        "schema_version": SCHEMA_VERSION,
        "group_id": meta["group_id"],
        "task_id": meta["task_id"],
        "objective": meta["objective"],
        "state": meta["state"],
        "status": meta["status"],
        "scope": meta["scope"],
        "authority_bundle_sha256": meta["authority_bundle_sha256"],
        "loopx_goal_id": meta["loopx_goal_id"],
        "member_registry_sha256": meta["member_registry_sha256"],
        "view_version": events[-1]["seq"] if events else 0,
        "event_chain_head": events[-1]["event_hash"] if events else "GENESIS",
        "members": [
            public_member(member)
            for member in sorted(
                members_doc["members"].values(), key=lambda item: item["member_id"]
            )
        ],
        "entries": ordered_entries,
        "open_question_entry_ids": open_questions,
        "open_conflict_entry_ids": open_conflicts,
        "current_best_model_entry_ids": current_best,
        "shared_view_sections": {
            "objective_and_scope": {
                "objective": meta["objective"],
                "scope": meta["scope"],
            },
            "frozen_authority_context": {
                "authority_bundle_sha256": meta["authority_bundle_sha256"],
                "long_term_memory_mounted": False,
            },
            "current_claims": [
                item["entry_id"]
                for item in ordered_entries
                if item["status"] == "active"
                and item["entry_type"] in {"FACT_CONFIRMED", "HYPOTHESIS"}
            ],
            "confirmed_facts": [
                item["entry_id"]
                for item in ordered_entries
                if item["status"] == "active"
                and item["entry_type"] == "FACT_CONFIRMED"
            ],
            "current_best_hypotheses": [
                item["entry_id"]
                for item in ordered_entries
                if item["status"] == "active"
                and item["entry_type"] in {"HYPOTHESIS", "CURRENT_BEST_MODEL"}
            ],
            "rejected_routes": [
                item["entry_id"]
                for item in ordered_entries
                if item["entry_type"] == "HYPOTHESIS_REJECTED"
                or item["status"] in {"rejected", "superseded"}
            ],
            "partial_results": [
                item["entry_id"]
                for item in ordered_entries
                if item["entry_type"] == "PARTIAL_RESULT"
            ],
            "artifact_and_evidence_refs": [
                item["entry_id"]
                for item in ordered_entries
                if item["entry_type"] in {"ARTIFACT_PUBLISHED", "EVIDENCE_ATTACHED"}
                or item["evidence_refs"]
            ],
            "open_questions": open_questions,
            "conflicts": open_conflicts,
            "next_actions": [
                item["entry_id"]
                for item in ordered_entries
                if item["status"] == "active"
                and item["entry_type"] in {"QUESTION_OPENED", "SCOPE_WARNING"}
            ],
            "handoff_readiness": meta["state"]
            in {"HANDOFF_READY", "RECONCILED", "ARCHIVED", "EXPIRED"},
        },
        "counts": {
            "events": len(events),
            "entries": len(ordered_entries),
            "active_members": sum(
                1 for member in members_doc["members"].values() if member["active"]
            ),
            "open_questions": len(open_questions),
            "open_conflicts": len(open_conflicts),
        },
        "generated_at": now_iso(),
    }
    hash_payload = {
        key: value for key, value in view.items() if key not in {"generated_at"}
    }
    view["semantic_hash"] = semantic_hash(hash_payload)
    atomic_write_json(group_dir / "view.json", view)
    return view


def scope_allowed(
    member: dict[str, Any], requested_scope: str, scope_field: str = "read_scope"
) -> bool:
    for allowed in member[scope_field]:
        if allowed == "*":
            return True
        if requested_scope == allowed or requested_scope.startswith(allowed + "/"):
            return True
    return False


def authenticate(
    group_dir: Path,
    *,
    member_id: str,
    lease_token: str,
    host_id: str,
    thread_id: str,
    requested_scope: str | None = None,
    scope_field: str = "read_scope",
    roles: Iterable[str] | None = None,
    allow_closed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = read_json(group_dir / "group.json")
    if not allow_closed and meta["state"] not in READABLE_STATES:
        raise BrainError("GROUP_NOT_READABLE", f"Group state is {meta['state']}")
    members_doc = read_json(group_dir / "members.json")
    member = members_doc["members"].get(member_id)
    if not member:
        raise BrainError("MEMBER_NOT_FOUND", member_id)
    if not member["active"]:
        raise BrainError("MEMBER_REVOKED", member_id)
    if member["host_id"] != host_id or member["thread_id"] != thread_id:
        raise BrainError("IDENTITY_BINDING_MISMATCH", member_id)
    if not hmac.compare_digest(member["token_hash"], token_hash(lease_token)):
        raise BrainError("LEASE_TOKEN_INVALID", member_id)
    if parse_time(member["lease_expires_at"]) <= datetime.now(timezone.utc).astimezone():
        raise BrainError("LEASE_EXPIRED", member_id)
    if requested_scope is not None and not scope_allowed(
        member, requested_scope, scope_field
    ):
        raise BrainError(
            "SCOPE_DENIED", f"{member_id} cannot access {requested_scope}"
        )
    if roles is not None and member["role"] not in set(roles):
        raise BrainError(
            "ROLE_DENIED", f"{member['role']} not allowed for this operation"
        )
    return meta, member


def new_member(
    member_id: str,
    role: str,
    host_id: str,
    thread_id: str,
    scopes: list[str],
    lease_hours: int,
) -> tuple[dict[str, Any], str]:
    if role not in ROLES:
        raise BrainError("ROLE_INVALID", role)
    if not member_id or not host_id or not thread_id:
        raise BrainError("IDENTITY_FIELD_MISSING", "member/host/thread are required")
    if not scopes:
        raise BrainError("SCOPE_MISSING", "at least one scope is required")
    # Prefix the opaque token so it can never be parsed as a command-line
    # option when passed to the CLI.
    token = f"wg_{secrets.token_urlsafe(32)}"
    member = {
        "member_id": member_id,
        "role": role,
        "host_id": host_id,
        "thread_id": thread_id,
        "scopes": sorted(set(scopes)),
        "read_scope": sorted(set(scopes)),
        "write_scope": sorted(set(scopes)) if role != "observer" else [],
        "token_hash": token_hash(token),
        "active": True,
        "status": "ACTIVE",
        "added_at": now_iso(),
        "joined_at": now_iso(),
        "lease_expires_at": (
            datetime.now(timezone.utc).astimezone() + timedelta(hours=lease_hours)
        ).isoformat(timespec="seconds"),
        "revoked_at": None,
    }
    return member, token


def command_create(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    if args.coordination_mode not in COORDINATION_MODES:
        raise BrainError("COORDINATION_MODE_INVALID", args.coordination_mode)
    if not args.allow_concurrent_groups:
        existing_group = find_active_identity_membership(
            root,
            host_id=args.host_id,
            thread_id=args.thread_id,
        )
        if existing_group is not None:
            raise BrainError(
                "THREAD_ALREADY_ACTIVE_IN_OTHER_GROUP",
                f"{args.host_id}/{args.thread_id} already belongs to {existing_group}",
            )
    group_dir = group_dir_for(root, args.group_id)
    if group_dir.exists():
        raise BrainError("GROUP_ALREADY_EXISTS", str(group_dir))
    group_dir.mkdir(parents=False)
    controller, token = new_member(
        args.controller_member_id,
        "controller",
        args.host_id,
        args.thread_id,
        args.scope,
        args.lease_hours,
    )
    meta = {
        "schema_version": SCHEMA_VERSION,
        "group_id": args.group_id,
        "task_id": args.task_id,
        "objective": args.objective,
        "state": "MEMBERS_BOUND",
        "status": "MEMBERS_BOUND",
        "scope": sorted(set(args.scope)),
        "created_at": now_iso(),
        "expires_at": (
            datetime.now(timezone.utc).astimezone()
            + timedelta(hours=args.expires_hours)
        ).isoformat(timespec="seconds"),
        "created_by": args.controller_member_id,
        "controller_member_id": args.controller_member_id,
        "authority_bundle_sha256": args.authority_bundle_sha256,
        "loopx_goal_id": args.loopx_goal_id,
        "coordination_mode": args.coordination_mode,
        "single_active_group_default": not args.allow_concurrent_groups,
        "member_registry_sha256": None,
        "long_term_memory_mounted": False,
        "project_authority_write_enabled": False,
        "visible_message_transport_enabled": False,
        "frozen_snapshot_sha256": None,
        "handoff_sha256": None,
        "closed_at": None,
        "retention": None,
    }
    atomic_write_json(group_dir / "group.json", meta)
    atomic_write_json(
        group_dir / "members.json",
        {"schema_version": SCHEMA_VERSION, "members": {controller["member_id"]: controller}},
    )
    atomic_write_json(
        group_dir / "coordination.json",
        coordination_document(
            args.group_id,
            args.coordination_mode,
            [controller["member_id"]],
        ),
    )
    sync_member_registry_hash(group_dir)
    (group_dir / "events.jsonl").write_text("", encoding="utf-8")
    with group_lock(group_dir):
        event = append_event_locked(
            group_dir,
            "GROUP_CREATED",
            args.controller_member_id,
            {
                "task_id": args.task_id,
                "objective": args.objective,
                "scope": meta["scope"],
                "authority_bundle_sha256": args.authority_bundle_sha256,
                "loopx_goal_id": args.loopx_goal_id,
                "coordination_mode": args.coordination_mode,
                "controller": public_member(controller),
            },
        )
        view = materialize_view(group_dir)
    return {
        "status": "CREATED",
        "group_id": args.group_id,
        "group_dir": str(group_dir),
        "controller_member_id": args.controller_member_id,
        "lease_token": token,
        "lease_token_display_policy": "display_once_do_not_persist",
        "event_id": event["event_id"],
        "view_version": view["view_version"],
        "semantic_hash": view["semantic_hash"],
        "coordination_mode": args.coordination_mode,
    }


def command_add_member(args: argparse.Namespace) -> dict[str, Any]:
    group_dir = group_dir_for(Path(args.root), args.group_id)
    with group_lock(group_dir):
        meta, _ = authenticate(
            group_dir,
            member_id=args.actor_member_id,
            lease_token=args.lease_token,
            host_id=args.actor_host_id,
            thread_id=args.actor_thread_id,
            roles={"controller"},
        )
        if meta["state"] not in WRITABLE_STATES:
            raise BrainError("GROUP_WRITE_CLOSED", meta["state"])
        members_doc = read_json(group_dir / "members.json")
        if args.member_id in members_doc["members"]:
            raise BrainError("MEMBER_ALREADY_EXISTS", args.member_id)
        if (
            meta.get("coordination_mode", "legacy") == "strict"
            and not args.allow_concurrent_groups
        ):
            existing_group = find_active_identity_membership(
                Path(args.root),
                host_id=args.host_id,
                thread_id=args.thread_id,
                exclude_group_id=args.group_id,
            )
            if existing_group is not None:
                raise BrainError(
                    "THREAD_ALREADY_ACTIVE_IN_OTHER_GROUP",
                    f"{args.host_id}/{args.thread_id} already belongs to {existing_group}",
                )
        member, token = new_member(
            args.member_id,
            args.role,
            args.host_id,
            args.thread_id,
            args.scope,
            args.lease_hours,
        )
        members_doc["members"][args.member_id] = member
        atomic_write_json(group_dir / "members.json", members_doc)
        coordination = read_coordination(group_dir, meta, members_doc)
        coordination["members"][args.member_id] = {
            "last_context_view_version": None,
            "last_context_at": None,
            "last_context_scope": None,
        }
        atomic_write_json(group_dir / "coordination.json", coordination)
        meta["state"] = "ACTIVE"
        meta["status"] = "ACTIVE"
        atomic_write_json(group_dir / "group.json", meta)
        registry_hash = sync_member_registry_hash(group_dir)
        event = append_event_locked(
            group_dir,
            "MEMBER_ADDED",
            args.actor_member_id,
            {"member": public_member(member)},
        )
        view = materialize_view(group_dir)
    return {
        "status": "MEMBER_ADDED",
        "group_id": args.group_id,
        "member": public_member(member),
        "lease_token": token,
        "lease_token_display_policy": "display_once_do_not_persist",
        "event_id": event["event_id"],
        "view_version": view["view_version"],
        "semantic_hash": view["semantic_hash"],
        "member_registry_sha256": registry_hash,
    }


def filtered_context(
    view: dict[str, Any], member: dict[str, Any], requested_scope: str
) -> dict[str, Any]:
    entries = [
        item
        for item in view["entries"]
        if scope_allowed(member, item["scope"], "read_scope")
        and (
            item["scope"] == requested_scope
            or item["scope"].startswith(requested_scope + "/")
            or requested_scope.startswith(item["scope"] + "/")
        )
    ]
    visible_ids = {item["entry_id"] for item in entries}
    return {
        "schema_version": SCHEMA_VERSION,
        "group_id": view["group_id"],
        "task_id": view["task_id"],
        "state": view["state"],
        "requested_scope": requested_scope,
        "member": public_member(member),
        "view_version": view["view_version"],
        "event_chain_head": view["event_chain_head"],
        "shared_semantic_hash": view["semantic_hash"],
        "entries": entries,
        "open_question_entry_ids": [
            item for item in view["open_question_entry_ids"] if item in visible_ids
        ],
        "open_conflict_entry_ids": [
            item for item in view["open_conflict_entry_ids"] if item in visible_ids
        ],
        "current_best_model_entry_ids": [
            item for item in view["current_best_model_entry_ids"] if item in visible_ids
        ],
        "authority_notice": {
            "host_control_plane_is_final_authority": True,
            "shared_brain_is_long_term_memory": False,
            "shared_brain_entries_are_project_truth": False,
        },
    }


def command_context(args: argparse.Namespace) -> dict[str, Any]:
    group_dir = group_dir_for(Path(args.root), args.group_id)
    with group_lock(group_dir):
        meta, member = authenticate(
            group_dir,
            member_id=args.member_id,
            lease_token=args.lease_token,
            host_id=args.host_id,
            thread_id=args.thread_id,
            requested_scope=args.scope,
        )
        view = materialize_view(group_dir)
        coordination = read_coordination(group_dir, meta)
        coordination.setdefault("members", {}).setdefault(args.member_id, {})
        coordination["members"][args.member_id] = {
            "last_context_view_version": view["view_version"],
            "last_context_at": now_iso(),
            "last_context_scope": args.scope,
        }
        atomic_write_json(group_dir / "coordination.json", coordination)
        result = filtered_context(view, member, args.scope)
    result["coordination"] = {
        "mode": meta.get("coordination_mode", "legacy"),
        "context_view_version": view["view_version"],
        "publish_with_expected_view_version": view["view_version"],
        "is_fresh": True,
    }
    return result


def parse_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_json and args.payload_file:
        raise BrainError("PAYLOAD_AMBIGUOUS", "use payload-json or payload-file")
    if args.payload_file:
        payload = read_json(Path(args.payload_file))
    elif args.payload_json:
        try:
            payload = json.loads(args.payload_json)
        except json.JSONDecodeError as exc:
            raise BrainError("PAYLOAD_INVALID_JSON", str(exc)) from exc
    else:
        payload = {"text": args.content}
    if not isinstance(payload, dict):
        raise BrainError("PAYLOAD_NOT_OBJECT", "payload must be a JSON object")
    return payload


def command_post(args: argparse.Namespace) -> dict[str, Any]:
    if args.entry_type not in ENTRY_TYPES:
        raise BrainError("ENTRY_TYPE_INVALID", args.entry_type)
    group_dir = group_dir_for(Path(args.root), args.group_id)
    payload = parse_payload(args)
    with group_lock(group_dir):
        meta, member = authenticate(
            group_dir,
            member_id=args.member_id,
            lease_token=args.lease_token,
            host_id=args.host_id,
            thread_id=args.thread_id,
            requested_scope=args.scope,
            scope_field="write_scope",
        )
        if meta["state"] not in WRITABLE_STATES:
            raise BrainError("GROUP_WRITE_CLOSED", meta["state"])
        if args.entry_type not in POST_ROLE_ALLOW[member["role"]]:
            raise BrainError(
                "ENTRY_ROLE_DENIED",
                f"{member['role']} cannot post {args.entry_type}",
            )
        if not 0 <= args.confidence <= 1:
            raise BrainError("CONFIDENCE_OUT_OF_RANGE", str(args.confidence))
        prior_view = require_fresh_context(
            group_dir,
            meta=meta,
            member=member,
            expected_view_version=args.expected_view_version,
            requested_scope=args.scope,
        )
        current_events = read_events(group_dir)
        entry = {
            "entry_id": f"wgbe:{uuid.uuid4().hex}",
            "entry_seq": len(current_events) + 1,
            "group_id": meta["group_id"],
            "task_id": meta["task_id"],
            "entry_type": args.entry_type,
            "subject_key": args.subject_key,
            "scope": args.scope,
            "author_member_id": args.member_id,
            "author_role": member["role"],
            "created_at": now_iso(),
            "status": "active",
            "confidence": args.confidence,
            "content": payload,
            "payload": payload,
            "payload_sha256": semantic_hash(payload),
            "content_hash": semantic_hash(payload),
            "evidence_refs": args.evidence_ref or [],
            "supersedes": args.supersedes_entry_id or [],
            "supersedes_entry_ids": args.supersedes_entry_id or [],
            "based_on_view_version": prior_view["view_version"],
        }
        event = append_event_locked(
            group_dir,
            "ENTRY_POSTED",
            args.member_id,
            {"entry": entry},
        )
        view = materialize_view(group_dir)
    return {
        "status": "ENTRY_POSTED",
        "entry_id": entry["entry_id"],
        "entry_type": entry["entry_type"],
        "event_id": event["event_id"],
        "view_version": view["view_version"],
        "based_on_view_version": prior_view["view_version"],
        "coordination_state_after_post": "STALE_UNTIL_CONTEXT_REFRESH",
        "semantic_hash": view["semantic_hash"],
    }


def command_resolve(args: argparse.Namespace) -> dict[str, Any]:
    group_dir = group_dir_for(Path(args.root), args.group_id)
    with group_lock(group_dir):
        meta, member = authenticate(
            group_dir,
            member_id=args.member_id,
            lease_token=args.lease_token,
            host_id=args.host_id,
            thread_id=args.thread_id,
            roles={"controller", "reviewer"},
        )
        if meta["state"] not in WRITABLE_STATES:
            raise BrainError("GROUP_WRITE_CLOSED", meta["state"])
        view = require_fresh_context(
            group_dir,
            meta=meta,
            member=member,
            expected_view_version=args.expected_view_version,
            requested_scope=args.scope,
        )
        target = next(
            (
                entry
                for entry in view["entries"]
                if entry["entry_id"] == args.target_entry_id
            ),
            None,
        )
        if target is None:
            raise BrainError("TARGET_ENTRY_NOT_FOUND", args.target_entry_id)
        if target["status"] != "active":
            raise BrainError("TARGET_ALREADY_RESOLVED", args.target_entry_id)
        if target["entry_type"] == "CONFLICT_RECORDED" and member["role"] != "controller":
            raise BrainError(
                "CONFLICT_FINAL_DECISION_REQUIRES_CONTROLLER",
                args.target_entry_id,
            )
        event = append_event_locked(
            group_dir,
            "ENTRY_RESOLVED",
            args.member_id,
            {
                "target_entry_id": args.target_entry_id,
                "status": args.status,
                "resolution": args.resolution,
            },
        )
        view = materialize_view(group_dir)
    return {
        "status": "ENTRY_RESOLVED",
        "target_entry_id": args.target_entry_id,
        "event_id": event["event_id"],
        "view_version": view["view_version"],
        "semantic_hash": view["semantic_hash"],
    }


def command_remove_member(args: argparse.Namespace) -> dict[str, Any]:
    group_dir = group_dir_for(Path(args.root), args.group_id)
    with group_lock(group_dir):
        meta, _ = authenticate(
            group_dir,
            member_id=args.actor_member_id,
            lease_token=args.lease_token,
            host_id=args.actor_host_id,
            thread_id=args.actor_thread_id,
            roles={"controller"},
        )
        if meta["state"] not in WRITABLE_STATES:
            raise BrainError("GROUP_WRITE_CLOSED", meta["state"])
        if args.member_id == args.actor_member_id:
            raise BrainError(
                "CONTROLLER_SELF_REMOVAL_FORBIDDEN",
                "controller is revoked by close, not remove-member",
            )
        members_doc = read_json(group_dir / "members.json")
        member = members_doc["members"].get(args.member_id)
        if not member:
            raise BrainError("MEMBER_NOT_FOUND", args.member_id)
        if not member["active"]:
            raise BrainError("MEMBER_ALREADY_REVOKED", args.member_id)
        member["active"] = False
        member["status"] = "REVOKED"
        member["revoked_at"] = now_iso()
        atomic_write_json(group_dir / "members.json", members_doc)
        registry_hash = sync_member_registry_hash(group_dir)
        event = append_event_locked(
            group_dir,
            "MEMBER_REMOVED",
            args.actor_member_id,
            {"member_id": args.member_id, "reason": args.reason},
        )
        view = materialize_view(group_dir)
    return {
        "status": "MEMBER_REMOVED",
        "member_id": args.member_id,
        "event_id": event["event_id"],
        "view_version": view["view_version"],
        "semantic_hash": view["semantic_hash"],
        "member_registry_sha256": registry_hash,
    }


def command_freeze(args: argparse.Namespace) -> dict[str, Any]:
    group_dir = group_dir_for(Path(args.root), args.group_id)
    with group_lock(group_dir):
        meta, _ = authenticate(
            group_dir,
            member_id=args.member_id,
            lease_token=args.lease_token,
            host_id=args.host_id,
            thread_id=args.thread_id,
            roles={"controller"},
        )
        if meta["state"] not in WRITABLE_STATES:
            raise BrainError("GROUP_CANNOT_FREEZE", meta["state"])
        pre_view = materialize_view(group_dir)
        meta["state"] = "FREEZING"
        meta["status"] = "FREEZING"
        atomic_write_json(group_dir / "group.json", meta)
        event = append_event_locked(
            group_dir,
            "GROUP_FROZEN",
            args.member_id,
            {
                "reason": args.reason,
                "pre_freeze_view_version": pre_view["view_version"],
                "pre_freeze_semantic_hash": pre_view["semantic_hash"],
            },
        )
        meta["state"] = "HANDOFF_READY"
        meta["status"] = "HANDOFF_READY"
        meta["frozen_at"] = now_iso()
        atomic_write_json(group_dir / "group.json", meta)
        frozen_view = materialize_view(group_dir)
        coordination = read_coordination(group_dir, meta)
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_kind": "frozen_workgroup_context",
            "group_id": args.group_id,
            "frozen_at": meta["frozen_at"],
            "view": frozen_view,
            "coordination": coordination,
            "coordination_sha256": semantic_hash(coordination),
        }
        snapshot["snapshot_sha256"] = semantic_hash(
            {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
        )
        atomic_write_json(group_dir / "FROZEN_SNAPSHOT.json", snapshot)
        meta["frozen_snapshot_sha256"] = snapshot["snapshot_sha256"]
        atomic_write_json(group_dir / "group.json", meta)
        frozen_view = materialize_view(group_dir)
    return {
        "status": "HANDOFF_READY",
        "event_id": event["event_id"],
        "view_version": frozen_view["view_version"],
        "semantic_hash": frozen_view["semantic_hash"],
        "frozen_snapshot_sha256": snapshot["snapshot_sha256"],
    }


def command_handoff(args: argparse.Namespace) -> dict[str, Any]:
    group_dir = group_dir_for(Path(args.root), args.group_id)
    with group_lock(group_dir):
        meta, _ = authenticate(
            group_dir,
            member_id=args.member_id,
            lease_token=args.lease_token,
            host_id=args.host_id,
            thread_id=args.thread_id,
            roles={"controller"},
        )
        if meta["state"] != "HANDOFF_READY":
            raise BrainError("HANDOFF_STATE_INVALID", meta["state"])
        snapshot = read_json(group_dir / "FROZEN_SNAPSHOT.json")
        handoff = {
            "schema_version": SCHEMA_VERSION,
            "handoff_kind": "ephemeral_workgroup_handoff",
            "group_id": args.group_id,
            "task_id": meta["task_id"],
            "objective": meta["objective"],
            "authority_bundle_sha256": meta["authority_bundle_sha256"],
            "loopx_goal_id": meta["loopx_goal_id"],
            "created_at": now_iso(),
            "created_by": args.member_id,
            "frozen_snapshot_sha256": snapshot["snapshot_sha256"],
            "frozen_view_version": snapshot["view"]["view_version"],
            "frozen_view_semantic_hash": snapshot["view"]["semantic_hash"],
            "coordination_sha256": snapshot["coordination_sha256"],
            "entry_count": snapshot["view"]["counts"]["entries"],
            "open_question_entry_ids": snapshot["view"]["open_question_entry_ids"],
            "open_conflict_entry_ids": snapshot["view"]["open_conflict_entry_ids"],
            "summary": args.summary,
            "evidence_refs": args.evidence_ref or [],
            "loopx_reconciliation_ref": args.loopx_reconciliation_ref,
            "promotion": {
                "host_control_plane_review_required": True,
                "automatic_project_promotion": False,
                "automatic_long_term_memory_write": False,
            },
        }
        handoff["handoff_sha256"] = semantic_hash(
            {key: value for key, value in handoff.items() if key != "handoff_sha256"}
        )
        atomic_write_json(group_dir / "STABLE_HANDOFF.json", handoff)
        event = append_event_locked(
            group_dir,
            "HANDOFF_CREATED",
            args.member_id,
            {
                "handoff_sha256": handoff["handoff_sha256"],
                "frozen_snapshot_sha256": snapshot["snapshot_sha256"],
            },
        )
        meta["state"] = "RECONCILED"
        meta["status"] = "RECONCILED"
        meta["handoff_sha256"] = handoff["handoff_sha256"]
        meta["handoff_at"] = now_iso()
        atomic_write_json(group_dir / "group.json", meta)
        view = materialize_view(group_dir)
    return {
        "status": "RECONCILED",
        "event_id": event["event_id"],
        "handoff_path": str(group_dir / "STABLE_HANDOFF.json"),
        "handoff_sha256": handoff["handoff_sha256"],
        "view_version": view["view_version"],
        "semantic_hash": view["semantic_hash"],
    }


def command_close(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    group_dir = group_dir_for(root, args.group_id)
    with group_lock(group_dir):
        meta, _ = authenticate(
            group_dir,
            member_id=args.member_id,
            lease_token=args.lease_token,
            host_id=args.host_id,
            thread_id=args.thread_id,
            roles={"controller"},
        )
        if meta["state"] != "RECONCILED":
            raise BrainError("CLOSE_STATE_INVALID", meta["state"])
        members_doc = read_json(group_dir / "members.json")
        revoked_ids = []
        revoked_at = now_iso()
        for member in members_doc["members"].values():
            if member["active"]:
                member["active"] = False
                member["status"] = "REVOKED"
                member["revoked_at"] = revoked_at
                revoked_ids.append(member["member_id"])
        atomic_write_json(group_dir / "members.json", members_doc)
        registry_hash = sync_member_registry_hash(group_dir)
        event = append_event_locked(
            group_dir,
            "GROUP_CLOSED",
            args.member_id,
            {
                "revoked_member_ids": sorted(revoked_ids),
                "retention": args.retention,
                "reason": args.reason,
            },
        )
        meta["state"] = "ARCHIVED" if args.retention == "archive" else "EXPIRED"
        meta["status"] = meta["state"]
        meta["member_registry_sha256"] = registry_hash
        meta["closed_at"] = revoked_at
        meta["retention"] = args.retention
        atomic_write_json(group_dir / "group.json", meta)
        view = materialize_view(group_dir)
    result = {
        "status": meta["state"],
        "event_id": event["event_id"],
        "revoked_member_ids": sorted(revoked_ids),
        "revoked_count": len(revoked_ids),
        "view_version": view["view_version"],
        "semantic_hash": view["semantic_hash"],
        "retention": args.retention,
        "member_registry_sha256": registry_hash,
    }
    if args.retention == "delete":
        tombstones = root / "_tombstones"
        tombstones.mkdir(parents=True, exist_ok=True)
        tombstone = {
            "schema_version": SCHEMA_VERSION,
            "group_id": args.group_id,
            "closed_at": revoked_at,
            "handoff_sha256": meta["handoff_sha256"],
            "final_event_chain_head": view["event_chain_head"],
            "final_view_semantic_hash": view["semantic_hash"],
            "revoked_count": len(revoked_ids),
            "deleted_runtime": True,
        }
        tombstone["tombstone_sha256"] = semantic_hash(
            {key: value for key, value in tombstone.items() if key != "tombstone_sha256"}
        )
        atomic_write_json(tombstones / f"{args.group_id}.json", tombstone)
        shutil.rmtree(group_dir)
        result["tombstone_path"] = str(tombstones / f"{args.group_id}.json")
        result["runtime_deleted"] = True
    return result


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    group_dir = group_dir_for(Path(args.root), args.group_id)
    meta = read_json(group_dir / "group.json")
    view = materialize_view(group_dir)
    return {
        "schema_version": SCHEMA_VERSION,
        "group_id": meta["group_id"],
        "task_id": meta["task_id"],
        "state": meta["state"],
        "view_version": view["view_version"],
        "semantic_hash": view["semantic_hash"],
        "counts": view["counts"],
        "frozen_snapshot_sha256": meta.get("frozen_snapshot_sha256"),
        "handoff_sha256": meta.get("handoff_sha256"),
    }


def add_auth_arguments(parser: argparse.ArgumentParser, actor_prefix: str = "") -> None:
    option_prefix = f"{actor_prefix}-" if actor_prefix else ""
    destination_prefix = f"{actor_prefix}_" if actor_prefix else ""
    parser.add_argument(
        f"--{option_prefix}member-id", dest=f"{destination_prefix}member_id", required=True
    )
    parser.add_argument("--lease-token", required=True)
    parser.add_argument(
        f"--{option_prefix}host-id", dest=f"{destination_prefix}host_id", required=True
    )
    parser.add_argument(
        f"--{option_prefix}thread-id", dest=f"{destination_prefix}thread_id", required=True
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(Path.home() / ".agent-brain" / "runtime"),
        help="Private runtime root",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--group-id", required=True)
    create.add_argument("--task-id", required=True)
    create.add_argument("--objective", required=True)
    create.add_argument("--controller-member-id", required=True)
    create.add_argument("--host-id", required=True)
    create.add_argument("--thread-id", required=True)
    create.add_argument("--scope", action="append", required=True)
    create.add_argument("--lease-hours", type=int, default=24)
    create.add_argument("--expires-hours", type=int, default=48)
    create.add_argument("--authority-bundle-sha256", required=True)
    create.add_argument("--loopx-goal-id", required=True)
    create.add_argument(
        "--coordination-mode",
        choices=sorted(COORDINATION_MODES),
        default="strict",
    )
    create.add_argument("--allow-concurrent-groups", action="store_true")
    create.set_defaults(handler=command_create)

    add_member = sub.add_parser("add-member")
    add_member.add_argument("--group-id", required=True)
    add_auth_arguments(add_member, "actor")
    add_member.add_argument("--member-id", required=True)
    add_member.add_argument("--role", choices=sorted(ROLES), required=True)
    add_member.add_argument("--host-id", required=True)
    add_member.add_argument("--thread-id", required=True)
    add_member.add_argument("--scope", action="append", required=True)
    add_member.add_argument("--lease-hours", type=int, default=24)
    add_member.add_argument("--allow-concurrent-groups", action="store_true")
    add_member.set_defaults(handler=command_add_member)

    context = sub.add_parser("context")
    context.add_argument("--group-id", required=True)
    add_auth_arguments(context)
    context.add_argument("--scope", required=True)
    context.set_defaults(handler=command_context)

    post = sub.add_parser("post")
    post.add_argument("--group-id", required=True)
    add_auth_arguments(post)
    post.add_argument("--entry-type", required=True)
    post.add_argument("--subject-key", required=True)
    post.add_argument("--scope", required=True)
    post.add_argument("--content")
    post.add_argument("--payload-json")
    post.add_argument("--payload-file")
    post.add_argument("--evidence-ref", action="append")
    post.add_argument("--supersedes-entry-id", action="append")
    post.add_argument("--confidence", type=float, default=1.0)
    post.add_argument("--expected-view-version", type=int)
    post.set_defaults(handler=command_post)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--group-id", required=True)
    add_auth_arguments(resolve)
    resolve.add_argument("--target-entry-id", required=True)
    resolve.add_argument(
        "--status", choices=["resolved", "rejected", "superseded"], required=True
    )
    resolve.add_argument("--resolution", required=True)
    resolve.add_argument("--scope", required=True)
    resolve.add_argument("--expected-view-version", type=int)
    resolve.set_defaults(handler=command_resolve)

    remove_member = sub.add_parser("remove-member")
    remove_member.add_argument("--group-id", required=True)
    add_auth_arguments(remove_member, "actor")
    remove_member.add_argument("--member-id", required=True)
    remove_member.add_argument("--reason", required=True)
    remove_member.set_defaults(handler=command_remove_member)

    freeze = sub.add_parser("freeze")
    freeze.add_argument("--group-id", required=True)
    add_auth_arguments(freeze)
    freeze.add_argument("--reason", required=True)
    freeze.set_defaults(handler=command_freeze)

    handoff = sub.add_parser("handoff")
    handoff.add_argument("--group-id", required=True)
    add_auth_arguments(handoff)
    handoff.add_argument("--summary", required=True)
    handoff.add_argument("--evidence-ref", action="append")
    handoff.add_argument("--loopx-reconciliation-ref", required=True)
    handoff.set_defaults(handler=command_handoff)

    close = sub.add_parser("close")
    close.add_argument("--group-id", required=True)
    add_auth_arguments(close)
    close.add_argument("--reason", required=True)
    close.add_argument("--retention", choices=["archive", "delete"], default="archive")
    close.set_defaults(handler=command_close)

    status = sub.add_parser("status")
    status.add_argument("--group-id", required=True)
    status.set_defaults(handler=command_status)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except BrainError as exc:
        print(
            json.dumps(
                {"status": "REJECTED", "error_code": exc.code, "message": exc.message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # fail closed and keep traceback out of shared artifacts
        print(
            json.dumps(
                {
                    "status": "REJECTED",
                    "error_code": "INTERNAL_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
