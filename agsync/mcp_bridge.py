"""Newline-delimited JSON-RPC 2.0 Stdio MCP server bridging Antigravity -> agsync HTTP hub."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agsync import __version__
from agsync.client import SyncClient, SyncClientError
from agsync.protocol import (
    DEFAULT_AGENT_TTL_SEC,
    DEFAULT_LEASE_TTL_SEC,
    DEFAULT_SYNC_WAIT_SEC,
    MCP_PROTOCOL_VERSION,
    MCP_TOOLS,
)


def _log(msg: str) -> None:
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"[agsync-mcp] {msg}\n")
            sys.stderr.flush()
    except OSError:
        pass


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            }
        ],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
        "isError": is_error,
    }


def dispatch_tool(
    client: SyncClient,
    default_agent: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    args = arguments or {}
    try:
        if tool_name == "sync_status":
            aid = str(args.get("agent_id") or default_agent)
            return _tool_result(client.snapshot(agent_id=aid))

        if tool_name == "sync_register":
            aid = str(args.get("agent_id") or default_agent)
            return _tool_result(
                client.register(
                    aid,
                    machine_name=str(args.get("machine_name") or socket.gethostname()),
                    role=str(args.get("role") or "agent"),
                    branch=str(args.get("branch") or ""),
                    status=str(args.get("status") or "online"),
                    ttl_sec=int(args.get("ttl_sec", DEFAULT_AGENT_TTL_SEC)),
                )
            )

        if tool_name == "sync_send_message":
            sid = str(args.get("sender_id") or default_agent)
            metadata = args.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise TypeError("'metadata' must be an object")
            return _tool_result(
                client.send_message(
                    sid,
                    str(args.get("recipient_id", "")),
                    str(args.get("body", "")),
                    msg_type=str(args.get("msg_type") or "info"),
                    metadata=metadata,
                )
            )

        if tool_name == "sync_poll_messages":
            aid = str(args.get("agent_id") or default_agent)
            since_raw = args.get("since_id")
            since_id = int(since_raw) if since_raw is not None else None
            return _tool_result(
                client.poll_messages(
                    aid,
                    since_id=since_id,
                    unread_only=bool(args.get("unread_only", False)),
                    auto_ack=bool(args.get("auto_ack", False)),
                    limit=int(args.get("limit", 100)),
                )
            )

        if tool_name == "sync_create_task":
            creator = str(args.get("created_by") or default_agent)
            scope_paths = args.get("scope_paths")
            if scope_paths is not None and not isinstance(scope_paths, list):
                raise TypeError("'scope_paths' must be an array")
            return _tool_result(
                client.create_task(
                    str(args.get("title", "")),
                    creator,
                    description=str(args.get("description") or ""),
                    scope_paths=scope_paths,
                    task_id=args.get("task_id"),
                )
            )

        if tool_name == "sync_claim_task":
            aid = str(args.get("agent_id") or default_agent)
            return _tool_result(client.claim_task(str(args.get("task_id", "")), aid))

        if tool_name == "sync_update_task":
            aid = str(args.get("agent_id") or default_agent)
            return _tool_result(
                client.update_task(
                    str(args.get("task_id", "")),
                    aid,
                    status=args.get("status"),
                    result_summary=args.get("result_summary"),
                )
            )

        if tool_name == "sync_acquire_lease":
            oid = str(args.get("owner_id") or default_agent)
            return _tool_result(
                client.acquire_lease(
                    str(args.get("resource", "")),
                    oid,
                    scope=str(args.get("scope") or "file"),
                    ttl_sec=int(args.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)),
                    reason=str(args.get("reason") or ""),
                )
            )

        if tool_name == "sync_renew_lease":
            oid = str(args.get("owner_id") or default_agent)
            return _tool_result(
                client.renew_lease(
                    oid,
                    lease_id=args.get("lease_id"),
                    resource=args.get("resource"),
                    scope=str(args.get("scope") or "file"),
                    ttl_sec=int(args.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)),
                )
            )

        if tool_name == "sync_release_lease":
            oid = str(args.get("owner_id") or default_agent)
            return _tool_result(
                client.release_lease(
                    oid,
                    lease_id=args.get("lease_id"),
                    resource=args.get("resource"),
                    scope=str(args.get("scope") or "file"),
                    release_all=bool(args.get("release_all", False)),
                )
            )

        if tool_name == "sync_check_paths":
            aid = str(args.get("agent_id") or default_agent)
            raw_paths = args.get("paths")
            if not isinstance(raw_paths, list):
                raise TypeError("'paths' must be an array of paths")
            return _tool_result(
                client.check_paths(
                    raw_paths,
                    agent_id=aid,
                    scope=str(args.get("scope") or "file"),
                )
            )

        if tool_name == "sync_publish_context":
            actor = str(args.get("updated_by") or default_agent)
            if "value" not in args:
                return _tool_result(
                    {"error": "validation_error", "message": "'value' is required"},
                    is_error=True,
                )
            exp_raw = args.get("expected_version")
            exp_ver = int(exp_raw) if exp_raw is not None else None
            return _tool_result(
                client.put_context(
                    str(args.get("key", "")),
                    args["value"],
                    actor,
                    expected_version=exp_ver,
                )
            )

        if tool_name == "sync_get_context":
            key = args.get("key")
            if key:
                return _tool_result(client.get_context(str(key)))
            return _tool_result(client.list_context(prefix=args.get("prefix")))

        if tool_name == "sync_wait":
            since_raw = args.get("since_id", 0)
            if since_raw is None:
                since_id = 0
            elif isinstance(since_raw, bool) or (
                isinstance(since_raw, float) and not since_raw.is_integer()
            ):
                raise TypeError("'since_id' must be a non-negative integer")
            else:
                since_id = int(since_raw)
            if since_id < 0:
                raise ValueError("'since_id' must be >= 0")

            timeout_raw = args.get("timeout_sec", DEFAULT_SYNC_WAIT_SEC)
            if timeout_raw is None:
                timeout_sec = DEFAULT_SYNC_WAIT_SEC
            elif isinstance(timeout_raw, bool):
                raise TypeError("'timeout_sec' must be a number")
            else:
                timeout_sec = float(timeout_raw)
            if not math.isfinite(timeout_sec) or timeout_sec < 0:
                raise ValueError("'timeout_sec' must be finite and >= 0")

            boot_id = args.get("boot_id")
            if boot_id is not None and not isinstance(boot_id, str):
                raise TypeError("'boot_id' must be a string")

            event_types = args.get("event_types")
            if event_types is not None:
                if not isinstance(event_types, list) or not all(
                    isinstance(t, str) and t.strip() for t in event_types
                ):
                    raise TypeError("'event_types' must be an array of non-empty strings")

            agent_id = args.get("agent_id")
            if agent_id is not None and not isinstance(agent_id, str):
                raise TypeError("'agent_id' must be a string")
            if isinstance(agent_id, str) and not agent_id.strip():
                agent_id = None

            return _tool_result(
                client.wait_events(
                    since_id=since_id,
                    boot_id=boot_id,
                    timeout_sec=timeout_sec,
                    event_types=event_types,
                    agent_id=agent_id,
                )
            )

        return _tool_result(
            {"error": "unknown_tool", "message": f"Unknown tool: {tool_name}"},
            is_error=True,
        )
    except SyncClientError as exc:
        err_body = dict(exc.payload) if exc.payload else {"error": "client_error", "message": exc.message}
        if exc.status_code is not None:
            err_body.setdefault("status_code", exc.status_code)
        return _tool_result(err_body, is_error=True)
    except (ValueError, TypeError) as exc:
        return _tool_result(
            {"error": "validation_error", "message": str(exc)},
            is_error=True,
        )


def handle_rpc_message(
    msg: dict[str, Any], client: SyncClient, default_agent: str
) -> dict[str, Any] | None:
    req_id = msg.get("id")
    method = msg.get("method")

    if not isinstance(method, str):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "Invalid Request: missing method"},
        }

    if req_id is None or method.startswith("notifications/"):
        return None

    raw_params = msg.get("params")
    if raw_params is None:
        params: dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = raw_params
    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": "Invalid params: expected object"},
        }

    if method == "initialize":
        client_ver = params.get("protocolVersion") or MCP_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": (
                    client_ver if isinstance(client_ver, str) else MCP_PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agsync", "version": __version__},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        if "arguments" in params and params["arguments"] is not None:
            arguments = params["arguments"]
            if not isinstance(arguments, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32602,
                        "message": "Invalid params for tools/call: arguments must be an object",
                    },
                }
        else:
            arguments = {}
        if not isinstance(name, str):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params for tools/call"},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": dispatch_tool(client, default_agent, name, arguments),
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio(client: SyncClient, default_agent: str) -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    _log(f"Bridge ready (agent={default_agent}, server={client.server_url})")
    for raw_line in sys.stdin:
        line = raw_line.lstrip("\ufeff").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        if not isinstance(msg, dict):
            continue

        resp = handle_rpc_message(msg, client, default_agent)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    _log("EOF on stdin; exiting.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agsync-mcp",
        description="Stdio MCP JSON-RPC 2.0 bridge forwarding tool calls to the agsync HTTP server.",
    )
    parser.add_argument(
        "--server",
        default=os.environ.get("AGSYNC_SERVER", "http://127.0.0.1:8765"),
        help="Base URL of the agsync coordination HTTP server.",
    )
    parser.add_argument(
        "--agent",
        default=os.environ.get("AGSYNC_AGENT", socket.gethostname()),
        help="Default agent ID for this Antigravity machine.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("AGSYNC_TOKEN", ""),
        help="Optional Bearer token if server auth is enabled.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP request timeout in seconds.",
    )
    args = parser.parse_args(argv)

    client = SyncClient(args.server, token=args.token or None, timeout=args.timeout)
    return serve_stdio(client, args.agent)


if __name__ == "__main__":
    raise SystemExit(main())
