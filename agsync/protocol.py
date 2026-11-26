"""Shared protocol constants, validation, path normalization, and MCP schemas."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_AGENT_TTL_SEC = 120
DEFAULT_LEASE_TTL_SEC = 600
MCP_PROTOCOL_VERSION = "2025-03-26"

TASK_STATUSES: tuple[str, ...] = (
    "open",
    "claimed",
    "in_progress",
    "review",
    "done",
    "blocked",
)
LEASE_SCOPES: tuple[str, ...] = ("file", "dir", "symbol")
MSG_TYPES: tuple[str, ...] = ("info", "request", "contract", "alert", "handoff")
AGENT_STATUSES: tuple[str, ...] = ("online", "busy", "offline")

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,64}$")
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


class ProtocolError(ValueError):
    """Raised when an input fails protocol validation."""


def validate_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str):
        raise ProtocolError("agent_id must be a string")
    cleaned = agent_id.strip()
    if not cleaned or cleaned == "*" or not _AGENT_ID_RE.match(cleaned):
        raise ProtocolError(
            "agent_id must be 1-64 chars of [A-Za-z0-9._:@-] and cannot be '*'"
        )
    return cleaned


def validate_recipient_id(recipient_id: str) -> str:
    if not isinstance(recipient_id, str):
        raise ProtocolError("recipient_id must be a string")
    cleaned = recipient_id.strip()
    if cleaned == "*":
        return "*"
    return validate_agent_id(cleaned)


def validate_scope(scope: str) -> str:
    if scope not in LEASE_SCOPES:
        raise ProtocolError(
            f"Invalid lease scope '{scope}'; expected one of {LEASE_SCOPES}"
        )
    return scope


def validate_task_status(status: str) -> str:
    if status not in TASK_STATUSES:
        raise ProtocolError(
            f"Invalid task status '{status}'; expected one of {TASK_STATUSES}"
        )
    return status


def validate_msg_type(msg_type: str) -> str:
    if msg_type not in MSG_TYPES:
        raise ProtocolError(
            f"Invalid msg_type '{msg_type}'; expected one of {MSG_TYPES}"
        )
    return msg_type


def validate_agent_status(status: str) -> str:
    if status not in AGENT_STATUSES:
        raise ProtocolError(
            f"Invalid agent status '{status}'; expected one of {AGENT_STATUSES}"
        )
    return status


def _normalize_rel_path(raw: str) -> str:
    raw_stripped = raw.strip()
    if raw_stripped.startswith("\\\\") or raw_stripped.startswith("//"):
        raise ProtocolError(
            f"UNC/network path '{raw}' is not allowed; use repository-relative paths"
        )
    text = raw_stripped.replace("\\", "/")
    if text.startswith("//"):
        raise ProtocolError(
            f"UNC/network path '{raw}' is not allowed; use repository-relative paths"
        )
    if _DRIVE_PREFIX_RE.match(text):
        raise ProtocolError(
            f"Drive-prefixed path '{raw}' is not allowed; use repository-relative paths"
        )
    while "//" in text:
        text = text.replace("//", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    if not text or text == ".":
        raise ProtocolError("Resource path cannot be empty or '.'")
    segments = text.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise ProtocolError(
                f"Invalid path segment '{seg}' in resource '{raw}' (path traversal is forbidden)"
            )
    return "/".join(segments)


def normalize_resource(
    resource: str,
    *,
    scope: str = "file",
    case_insensitive: bool = False,
) -> str:
    """Normalize a file, directory, or symbol resource into canonical forward-slash form."""
    validate_scope(scope)
    if not isinstance(resource, str) or not resource.strip():
        raise ProtocolError("resource must be a non-empty string")

    if scope == "symbol":
        raw = resource.strip()
        if "::" in raw:
            file_part, sym_part = raw.split("::", 1)
            norm_file = _normalize_rel_path(file_part)
            sym_clean = sym_part.strip()
            if not sym_clean:
                raise ProtocolError("Symbol name after '::' cannot be empty")
            if case_insensitive:
                norm_file = norm_file.lower()
            return f"{norm_file}::{sym_clean}"
        if "/" in raw or "\\" in raw:
            raise ProtocolError(
                "Path-qualified symbol locks must use '<relative/path>::<SymbolName>'"
            )
        return raw

    norm = _normalize_rel_path(resource)
    return norm.lower() if case_insensitive else norm


def paths_overlap(norm_a: str, scope_a: str, norm_b: str, scope_b: str) -> bool:
    """Return True if two normalized resources with scopes conflict/overlap."""
    if scope_a == "symbol" and scope_b == "symbol":
        return norm_a == norm_b

    if scope_a == "symbol":
        if "::" not in norm_a:
            return False
        file_a = norm_a.split("::", 1)[0]
        return paths_overlap(file_a, "file", norm_b, scope_b)

    if scope_b == "symbol":
        if "::" not in norm_b:
            return False
        file_b = norm_b.split("::", 1)[0]
        return paths_overlap(norm_a, scope_a, file_b, "file")

    if norm_a == norm_b:
        return True
    if scope_a == "dir" and norm_b.startswith(norm_a + "/"):
        return True
    if scope_b == "dir" and norm_a.startswith(norm_b + "/"):
        return True
    return False


MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "sync_status",
        "description": (
            "Get a live coordination snapshot: online/offline agents, open/claimed tasks, "
            "active file/dir leases, shared context keys, and unread message count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Optional agent ID to compute unread_count for.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_register",
        "description": (
            "Register this agent (or send a heartbeat) with machine name, role, current Git branch, "
            "and status so the peer agent knows who is online and on which branch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Unique agent ID (e.g. machine-a)."},
                "machine_name": {"type": "string", "description": "Host/workstation name."},
                "role": {"type": "string", "description": "Role label (e.g. backend, frontend, reviewer)."},
                "branch": {"type": "string", "description": "Current Git branch name."},
                "status": {"type": "string", "enum": list(AGENT_STATUSES)},
                "ttl_sec": {"type": "integer", "minimum": 10, "maximum": 3600},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_send_message",
        "description": (
            "Send a real-time message to a specific peer agent_id or '*' to broadcast to all peers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sender_id": {"type": "string", "description": "Sender agent ID (defaults to configured agent)."},
                "recipient_id": {
                    "type": "string",
                    "description": "Target agent ID or '*' for broadcast.",
                },
                "body": {"type": "string", "description": "Message text."},
                "msg_type": {"type": "string", "enum": list(MSG_TYPES)},
                "metadata": {"type": "object", "description": "Optional structured JSON metadata."},
            },
            "required": ["recipient_id", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_poll_messages",
        "description": "Poll direct and broadcast messages for an agent, optionally filtering to unread only and auto-acking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "since_id": {"type": "integer", "minimum": 0},
                "unread_only": {"type": "boolean"},
                "auto_ack": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_create_task",
        "description": "Create a shared task with optional target file/directory scope paths so peers can split work cleanly.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "scope_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repository-relative files or directories touched by this task.",
                },
                "created_by": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_claim_task",
        "description": "Atomically claim an open task for this agent. Fails with a conflict error if already claimed by the peer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent_id": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_update_task",
        "description": "Update a claimed task's status (open, claimed, in_progress, review, done, blocked) and result summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "status": {"type": "string", "enum": list(TASK_STATUSES)},
                "result_summary": {"type": "string"},
            },
            "required": ["task_id", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_acquire_lease",
        "description": (
            "Acquire an exclusive TTL lease on a repository-relative file, directory, or symbol BEFORE editing. "
            "Rejects hierarchical directory/file overlaps held by the peer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Repo-relative path (e.g. src/app.py or src/api)."},
                "scope": {"type": "string", "enum": list(LEASE_SCOPES), "default": "file"},
                "owner_id": {"type": "string"},
                "ttl_sec": {"type": "integer", "minimum": 5, "maximum": 7200},
                "reason": {"type": "string"},
            },
            "required": ["resource"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_renew_lease",
        "description": "Extend the TTL on an active lease held by this agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lease_id": {"type": "string"},
                "resource": {"type": "string"},
                "scope": {"type": "string", "enum": list(LEASE_SCOPES)},
                "owner_id": {"type": "string"},
                "ttl_sec": {"type": "integer", "minimum": 5, "maximum": 7200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_release_lease",
        "description": "Release one specific lease (by lease_id or resource) or all leases owned by this agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lease_id": {"type": "string"},
                "resource": {"type": "string"},
                "scope": {"type": "string", "enum": list(LEASE_SCOPES)},
                "owner_id": {"type": "string"},
                "release_all": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_check_paths",
        "description": "Pre-flight check whether one or more repo-relative paths are free, owned by self, or locked by a peer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "agent_id": {"type": "string"},
                "scope": {"type": "string", "enum": list(LEASE_SCOPES)},
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_publish_context",
        "description": (
            "Publish or update a shared live contract/state entry (e.g. 'contract:auth_api') "
            "with monotonic versioning and optional expected_version optimistic concurrency check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"description": "JSON-serializable value (object, array, string, number, boolean)."},
                "updated_by": {"type": "string"},
                "expected_version": {"type": "integer", "minimum": 0},
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_get_context",
        "description": "Read a specific shared context key or list all shared context entries (optionally by key prefix).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "prefix": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]
