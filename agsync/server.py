"""LAN Coordination HTTP Server (REST JSON API + Streamable HTTP /mcp)."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agsync import __version__
from agsync.adapters.ide import decode_bytes_auto, read_text_auto
from agsync.adapters.network import (
    PidLockFile,
    is_ip_literal,
    is_loopback_host,
)
from agsync.dashboard import DASHBOARD_HTML
from agsync.db import default_data_dir, default_db_path
from agsync.discovery import DEFAULT_DISCOVERY_PORT, DiscoveryBeacon
from agsync.protocol import (
    DEFAULT_AGENT_TTL_SEC,
    DEFAULT_HOST,
    DEFAULT_LEASE_TTL_SEC,
    DEFAULT_PORT,
    MCP_PROTOCOL_VERSION,
    MCP_TOOLS,
)
from agsync.store import NotFoundError, SyncError, SyncStore, ValidationError


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


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


def _setup_file_logger(log_file: Path | None) -> logging.Logger | None:
    if log_file is None:
        return None
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"agsync.server.{log_file}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            str(log_file),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _parse_host_header(raw_host: str) -> tuple[str, int | None]:
    """Parse HTTP Host header into (hostname, optional_port)."""
    text = raw_host.strip()
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            raise ValueError("Invalid IPv6 Host header")
        host = text[1:end]
        rest = text[end + 1 :]
        if rest.startswith(":"):
            port = int(rest[1:])
            return host, port
        if rest:
            raise ValueError("Invalid Host header suffix")
        return host, None
    if ":" in text:
        host, port_str = text.rsplit(":", 1)
        return host, int(port_str)
    return text, None


def execute_mcp_tool(
    store: SyncStore,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    default_agent: str | None = None,
) -> dict[str, Any]:
    """Execute one of the sync_* MCP tools directly against SyncStore."""
    args = arguments or {}

    def need_agent(field: str = "agent_id") -> str:
        val = args.get(field) or args.get("agent_id") or default_agent
        if not val or not str(val).strip():
            raise ValidationError(
                "missing_agent_id",
                f"Provide '{field}' in tool arguments or set X-Agent-Id / ?agent= on /mcp",
            )
        return str(val).strip()

    try:
        if tool_name == "sync_status":
            aid = args.get("agent_id") or default_agent
            return _tool_result(store.snapshot(agent_id=aid))

        if tool_name == "sync_register":
            aid = need_agent("agent_id")
            res = store.register_agent(
                aid,
                machine_name=str(args.get("machine_name", "")),
                role=str(args.get("role", "agent")),
                branch=str(args.get("branch", "")),
                status=str(args.get("status", "online")),
                ttl_sec=int(args.get("ttl_sec", DEFAULT_AGENT_TTL_SEC)),
            )
            return _tool_result(res)

        if tool_name == "sync_send_message":
            sid = need_agent("sender_id")
            rid = str(args.get("recipient_id", ""))
            body = str(args.get("body", ""))
            metadata = args.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValidationError("validation_error", "'metadata' must be an object")
            res = store.send_message(
                sid,
                rid,
                body,
                msg_type=str(args.get("msg_type", "info")),
                metadata=metadata,
            )
            return _tool_result(res)

        if tool_name == "sync_poll_messages":
            aid = need_agent("agent_id")
            since_raw = args.get("since_id")
            since_id = int(since_raw) if since_raw is not None else None
            res = store.poll_messages(
                aid,
                since_id=since_id,
                unread_only=bool(args.get("unread_only", False)),
                auto_ack=bool(args.get("auto_ack", False)),
                limit=int(args.get("limit", 100)),
            )
            return _tool_result(res)

        if tool_name == "sync_create_task":
            creator = need_agent("created_by")
            scope_paths = args.get("scope_paths")
            if scope_paths is not None and not isinstance(scope_paths, list):
                raise ValidationError("validation_error", "'scope_paths' must be an array")
            res = store.create_task(
                str(args.get("title", "")),
                creator,
                description=str(args.get("description", "")),
                scope_paths=scope_paths,
                task_id=args.get("task_id"),
            )
            return _tool_result(res)

        if tool_name == "sync_claim_task":
            aid = need_agent("agent_id")
            res = store.claim_task(str(args.get("task_id", "")), aid)
            return _tool_result(res)

        if tool_name == "sync_update_task":
            aid = need_agent("agent_id")
            res = store.update_task(
                str(args.get("task_id", "")),
                aid,
                status=args.get("status"),
                result_summary=args.get("result_summary"),
            )
            return _tool_result(res)

        if tool_name == "sync_acquire_lease":
            oid = need_agent("owner_id")
            res = store.acquire_lease(
                str(args.get("resource", "")),
                oid,
                scope=str(args.get("scope", "file")),
                ttl_sec=int(args.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)),
                reason=str(args.get("reason", "")),
            )
            return _tool_result(res)

        if tool_name == "sync_renew_lease":
            oid = need_agent("owner_id")
            res = store.renew_lease(
                oid,
                lease_id=args.get("lease_id"),
                resource=args.get("resource"),
                scope=str(args.get("scope", "file")),
                ttl_sec=int(args.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)),
            )
            return _tool_result(res)

        if tool_name == "sync_release_lease":
            oid = need_agent("owner_id")
            res = store.release_lease(
                oid,
                lease_id=args.get("lease_id"),
                resource=args.get("resource"),
                scope=str(args.get("scope", "file")),
                release_all=bool(args.get("release_all", False)),
            )
            return _tool_result(res)

        if tool_name == "sync_check_paths":
            aid = args.get("agent_id") or default_agent
            raw_paths = args.get("paths")
            if not isinstance(raw_paths, list):
                raise ValidationError("validation_error", "'paths' must be an array of paths")
            res = store.check_paths(
                raw_paths,
                agent_id=aid,
                scope=str(args.get("scope", "file")),
            )
            return _tool_result(res)

        if tool_name == "sync_publish_context":
            actor = need_agent("updated_by")
            if "value" not in args:
                raise ValidationError("validation_error", "'value' is required")
            exp_raw = args.get("expected_version")
            exp_ver = int(exp_raw) if exp_raw is not None else None
            res = store.put_context(
                str(args.get("key", "")),
                args["value"],
                actor,
                expected_version=exp_ver,
            )
            return _tool_result(res)

        if tool_name == "sync_get_context":
            key = args.get("key")
            if key:
                return _tool_result(store.get_context(str(key)))
            return _tool_result({"entries": store.list_context(prefix=args.get("prefix"))})

        return _tool_result(
            {"error": "unknown_tool", "message": f"Unknown tool: {tool_name}"},
            is_error=True,
        )
    except SyncError as exc:
        return _tool_result(exc.to_dict(), is_error=True)
    except (ValueError, TypeError) as exc:
        return _tool_result(
            {"error": "validation_error", "message": str(exc)},
            is_error=True,
        )


def handle_mcp_jsonrpc(
    store: SyncStore,
    req: dict[str, Any],
    *,
    default_agent: str | None = None,
) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 MCP message; returns None for notifications."""
    req_id = req.get("id")
    method = req.get("method")

    if not isinstance(method, str):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "Invalid Request: missing method"},
        }

    if req_id is None or method.startswith("notifications/"):
        return None

    raw_params = req.get("params")
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
        result = execute_mcp_tool(store, name, arguments, default_agent=default_agent)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


class CoordinationHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: SyncStore,
        *,
        token: str | None = None,
        admin_token: str | None = None,
        allow_insecure_lan: bool = False,
        allowed_hosts: list[str] | None = None,
        pid_lock: bool = True,
        log_file: Path | None = None,
        purge_interval_sec: float = 60.0,
    ) -> None:
        self.store = store
        self.token = token.strip() if (token and token.strip()) else None
        self.admin_token = admin_token.strip() if (admin_token and admin_token.strip()) else None
        self.allow_insecure_lan = bool(allow_insecure_lan)
        self.started_at = time.time()
        self.allowed_hosts = {
            h.strip().lower() for h in (allowed_hosts or []) if h and h.strip()
        }
        self.file_logger = _setup_file_logger(log_file)

        bind_host = str(server_address[0] or DEFAULT_HOST)
        if not is_loopback_host(bind_host) and not self.token and not self.allow_insecure_lan:
            raise ValueError(
                f"Refusing to bind non-loopback address '{bind_host}' without a Bearer token. "
                "Provide token, --auto-token, or explicit allow_insecure_lan=True."
            )

        self._pid_lock: PidLockFile | None = (
            PidLockFile(store.db_path) if (pid_lock and store.db_path != ":memory:") else None
        )
        if self._pid_lock is not None:
            self._pid_lock.acquire(host=bind_host, port=int(server_address[1]))

        self._purge_stop = threading.Event()
        self._purge_thread: threading.Thread | None = None
        self._purge_interval_sec = float(purge_interval_sec)

        try:
            super().__init__(server_address, CoordinationHandler)
        except Exception:
            if self._pid_lock is not None:
                self._pid_lock.release()
            raise

        if self._pid_lock is not None:
            actual_host, actual_port = self.server_address[:2]
            self._pid_lock.update_port(host=str(actual_host), port=int(actual_port))

        if self._purge_interval_sec > 0:
            self._purge_thread = threading.Thread(
                target=self._periodic_purge_loop,
                name="agsync-lease-purge",
                daemon=True,
            )
            self._purge_thread.start()

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def _periodic_purge_loop(self) -> None:
        while not self._purge_stop.wait(self._purge_interval_sec):
            try:
                self.store.purge_expired_leases()
            except Exception:
                pass

    def server_close(self) -> None:
        self._purge_stop.set()
        if self._purge_thread is not None:
            self._purge_thread.join(timeout=1.0)
            self._purge_thread = None
        try:
            super().server_close()
        finally:
            if self._pid_lock is not None:
                self._pid_lock.release()


class CoordinationHandler(BaseHTTPRequestHandler):
    server: CoordinationHTTPServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"
    timeout = 30.0

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        line = f"[agsync-server] {self.address_string()} - {format % args}"
        try:
            if sys.stderr is not None:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
        except OSError:
            pass
        if getattr(self.server, "file_logger", None) is not None:
            try:
                self.server.file_logger.info(line)
            except Exception:
                pass

    def _send_json(
        self,
        status_code: int,
        payload: Any,
        *,
        extra_headers: dict[str, str] | None = None,
        close_conn: bool = False,
    ) -> None:
        body = _json_bytes(payload)
        if close_conn or status_code in (400, 401, 403, 408, 413, 415, 500):
            self.close_connection = True
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(
        self,
        status_code: int,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status_code)
        self.send_header("Content-Length", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()

    def _drain_body(self, max_drain: int = 65536) -> None:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return
        try:
            length = int(raw_len)
        except ValueError:
            self.close_connection = True
            return
        if 0 < length <= max_drain:
            try:
                self.rfile.read(length)
            except OSError:
                self.close_connection = True
        elif length > max_drain:
            self.close_connection = True

    def _read_json_body(self) -> Any:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError as exc:
            self.close_connection = True
            raise ValidationError("bad_content_length", "Invalid Content-Length") from exc
        if length < 0 or length > 8 * 1024 * 1024:
            self.close_connection = True
            raise ValidationError("payload_too_large", "Payload size out of bounds")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            text = decode_bytes_auto(raw).lstrip("\ufeff")
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("invalid_json", f"Invalid JSON body: {exc}") from exc

    def _check_host_and_origin(self) -> bool:
        raw_host = self.headers.get("Host")
        if not raw_host:
            if self.request_version == "HTTP/1.1":
                self._drain_body()
                self._send_json(
                    400,
                    {"error": "missing_host", "message": "Host header is required"},
                    close_conn=True,
                )
                return False
            req_host = "127.0.0.1"
            req_port: int | None = None
        else:
            try:
                req_host, req_port = _parse_host_header(raw_host)
            except ValueError:
                self._drain_body()
                self._send_json(
                    400,
                    {"error": "invalid_host", "message": "Malformed Host header"},
                    close_conn=True,
                )
                return False

        host_lower = req_host.strip().lower()
        host_allowed = (
            is_loopback_host(host_lower)
            or is_ip_literal(host_lower)
            or host_lower in self.server.allowed_hosts
        )
        if not host_allowed:
            self._drain_body()
            self._send_json(
                403,
                {"error": "forbidden_host", "message": f"Disallowed Host header: {raw_host}"},
                close_conn=True,
            )
            return False

        origin = self.headers.get("Origin")
        if origin is not None:
            origin_clean = origin.strip()
            if not origin_clean or origin_clean.lower() == "null":
                self._drain_body()
                self._send_json(
                    403,
                    {"error": "forbidden_origin", "message": "Null Origin is not allowed"},
                    close_conn=True,
                )
                return False
            parsed_origin = urlparse(origin_clean)
            if parsed_origin.scheme not in ("http", "https") or not parsed_origin.hostname:
                self._drain_body()
                self._send_json(
                    403,
                    {"error": "forbidden_origin", "message": "Invalid Origin header"},
                    close_conn=True,
                )
                return False
            orig_host = parsed_origin.hostname.strip().lower()
            orig_port = parsed_origin.port
            if orig_port is None:
                orig_port = 443 if parsed_origin.scheme == "https" else 80
            eff_req_port = req_port if req_port is not None else int(self.server.server_address[1])
            if orig_host != host_lower or orig_port != eff_req_port:
                self._drain_body()
                self._send_json(
                    403,
                    {"error": "forbidden_origin", "message": f"Cross-origin request blocked: {origin_clean}"},
                    close_conn=True,
                )
                return False

        return True

    def _require_json_content_type(self) -> bool:
        raw_ct = self.headers.get("Content-Type") or ""
        media_type = raw_ct.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self._drain_body()
            self._send_json(
                415,
                {
                    "error": "unsupported_media_type",
                    "message": "Content-Type must be application/json",
                },
                close_conn=True,
            )
            return False
        return True

    def _check_auth(self, *, admin_required: bool = False) -> bool:
        expected = (
            self.server.admin_token
            if (admin_required and self.server.admin_token)
            else self.server.token
        )
        if not expected:
            if admin_required and not is_loopback_host(str(self.client_address[0])):
                self._drain_body()
                self._send_json(
                    403,
                    {
                        "error": "forbidden",
                        "message": "Remote /state/* access requires Bearer token authentication",
                    },
                    close_conn=True,
                )
                return False
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[len("Bearer ") :].strip()
            if hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
                return True
        self._drain_body()
        self._send_json(
            401,
            {"error": "unauthorized", "message": "Missing or invalid Bearer token"},
            extra_headers={"WWW-Authenticate": "Bearer"},
            close_conn=True,
        )
        return False

    def _handle_sync_error(self, exc: SyncError) -> None:
        self._send_json(exc.status_code, exc.to_dict())

    def _dispatch_guarded(self, fn: Any) -> None:
        try:
            if not self._check_host_and_origin():
                return
            fn()
        except SyncError as exc:
            self._handle_sync_error(exc)
        except (ValueError, TypeError) as exc:
            self._send_json(
                400,
                {"error": "validation_error", "message": str(exc)},
                close_conn=True,
            )
        except Exception as exc:
            self.log_message("Unhandled exception: %s", exc)
            try:
                self._send_json(
                    500,
                    {"error": "internal_error", "message": "Internal server error"},
                    close_conn=True,
                )
            except OSError:
                self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch_guarded(self._do_GET_impl)

    def _do_GET_impl(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/dashboard"):
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/health":
            snap = self.server.store.snapshot()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "version": __version__,
                    "uptime_sec": round(time.time() - self.server.started_at, 3),
                    "auth_required": self.server.token is not None,
                    "case_insensitive": snap["case_insensitive"],
                    "online_agents": len(snap["online_agents"]),
                    "active_leases": len(snap["active_leases"]),
                    "tasks_count": len(snap["tasks"]),
                },
            )
            return

        is_state_route = path.startswith("/state/")
        if not self._check_auth(admin_required=is_state_route):
            return

        if path == "/mcp":
            self._send_json(
                405,
                {
                    "error": "method_not_allowed",
                    "message": "Use POST /mcp for Streamable HTTP MCP",
                },
                extra_headers={"Allow": "POST, DELETE"},
            )
            return

        if path == "/agents":
            inc = qs.get("include_offline", ["1"])[0] not in ("0", "false", "False")
            self._send_json(
                200,
                {"agents": self.server.store.list_agents(include_offline=inc)},
            )
            return

        if path == "/messages":
            aid = qs.get("agent_id", [""])[0]
            if not aid:
                raise ValidationError("validation_error", "agent_id query param is required")
            if qs.get("auto_ack", ["0"])[0] in ("1", "true", "True"):
                raise ValidationError(
                    "validation_error",
                    "GET /messages is read-only; use POST /messages/ack to acknowledge messages",
                )
            since_raw = qs.get("since_id", [None])[0]
            since_id = int(since_raw) if since_raw is not None else None
            unread = qs.get("unread", ["0"])[0] in ("1", "true", "True")
            limit = int(qs.get("limit", ["100"])[0])
            self._send_json(
                200,
                self.server.store.poll_messages(
                    aid,
                    since_id=since_id,
                    unread_only=unread,
                    auto_ack=False,
                    limit=limit,
                ),
            )
            return

        if path == "/tasks":
            status = qs.get("status", [None])[0]
            owner = qs.get("owner_id", [None])[0]
            self._send_json(
                200,
                {"tasks": self.server.store.list_tasks(status=status, owner_id=owner)},
            )
            return

        if path == "/leases":
            owner = qs.get("owner_id", [None])[0]
            self._send_json(
                200, {"leases": self.server.store.list_leases(owner_id=owner)}
            )
            return

        if path == "/context":
            prefix = qs.get("prefix", [None])[0]
            self._send_json(
                200, {"entries": self.server.store.list_context(prefix=prefix)}
            )
            return

        if path.startswith("/context/"):
            key = unquote(path[len("/context/") :])
            self._send_json(200, self.server.store.get_context(key))
            return

        if path == "/snapshot":
            aid = qs.get("agent_id", [None])[0]
            self._send_json(200, self.server.store.snapshot(agent_id=aid))
            return

        if path == "/state/export":
            self._send_json(200, self.server.store.export_state())
            return

        raise NotFoundError("not_found", f"Unknown endpoint: GET {path}")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch_guarded(self._do_POST_impl)

    def _do_POST_impl(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        is_state_route = path.startswith("/state/")

        if not self._check_auth(admin_required=is_state_route):
            return
        if not self._require_json_content_type():
            return

        body = self._read_json_body()

        if path == "/mcp":
            default_agent = (
                self.headers.get("X-Agent-Id")
                or qs.get("agent", [None])[0]
                or qs.get("agent_id", [None])[0]
            )
            session_id = self.headers.get("Mcp-Session-Id")
            extra_headers = {"Mcp-Session-Id": session_id} if session_id else None

            if isinstance(body, list):
                responses = []
                for item in body:
                    if isinstance(item, dict):
                        resp = handle_mcp_jsonrpc(
                            self.server.store, item, default_agent=default_agent
                        )
                        if resp is not None:
                            responses.append(resp)
                if not responses:
                    self._send_empty(202, extra_headers=extra_headers)
                else:
                    self._send_json(200, responses, extra_headers=extra_headers)
                return

            if not isinstance(body, dict):
                self._send_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid JSON-RPC payload"},
                    },
                    close_conn=True,
                )
                return

            resp = handle_mcp_jsonrpc(
                self.server.store, body, default_agent=default_agent
            )
            if resp is None:
                self._send_empty(202, extra_headers=extra_headers)
            else:
                self._send_json(200, resp, extra_headers=extra_headers)
            return

        if not isinstance(body, dict):
            raise ValidationError("validation_error", "JSON object body expected")

        if path == "/agents/register":
            res = self.server.store.register_agent(
                body.get("agent_id", ""),
                machine_name=body.get("machine_name", ""),
                role=body.get("role", "agent"),
                branch=body.get("branch", ""),
                status=body.get("status", "online"),
                capabilities=body.get("capabilities"),
                ttl_sec=int(body.get("ttl_sec", DEFAULT_AGENT_TTL_SEC)),
            )
            self._send_json(200, res)
            return

        if path.startswith("/agents/") and path.endswith("/heartbeat"):
            aid = unquote(path[len("/agents/") : -len("/heartbeat")])
            res = self.server.store.heartbeat_agent(
                aid,
                status=body.get("status"),
                branch=body.get("branch"),
                ttl_sec=body.get("ttl_sec"),
            )
            self._send_json(200, res)
            return

        if path == "/messages":
            res = self.server.store.send_message(
                body.get("sender_id", ""),
                body.get("recipient_id", ""),
                body.get("body", ""),
                msg_type=body.get("msg_type", "info"),
                metadata=body.get("metadata"),
            )
            self._send_json(201, res)
            return

        if path == "/messages/ack":
            res = self.server.store.ack_messages(
                body.get("agent_id", ""),
                int(body.get("up_to_id", 0)),
            )
            self._send_json(200, res)
            return

        if path == "/tasks":
            res = self.server.store.create_task(
                body.get("title", ""),
                body.get("created_by", ""),
                description=body.get("description", ""),
                scope_paths=body.get("scope_paths"),
                task_id=body.get("task_id"),
            )
            self._send_json(201, res)
            return

        if path.startswith("/tasks/") and path.endswith("/claim"):
            tid = unquote(path[len("/tasks/") : -len("/claim")])
            res = self.server.store.claim_task(tid, body.get("agent_id", ""))
            self._send_json(200, res)
            return

        if path == "/leases/acquire":
            res = self.server.store.acquire_lease(
                body.get("resource", ""),
                body.get("owner_id", ""),
                scope=body.get("scope", "file"),
                ttl_sec=int(body.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)),
                reason=body.get("reason", ""),
            )
            self._send_json(200, res)
            return

        if path == "/leases/renew":
            res = self.server.store.renew_lease(
                body.get("owner_id", ""),
                lease_id=body.get("lease_id"),
                resource=body.get("resource"),
                scope=body.get("scope", "file"),
                ttl_sec=int(body.get("ttl_sec", DEFAULT_LEASE_TTL_SEC)),
            )
            self._send_json(200, res)
            return

        if path == "/leases/release":
            res = self.server.store.release_lease(
                body.get("owner_id", ""),
                lease_id=body.get("lease_id"),
                resource=body.get("resource"),
                scope=body.get("scope", "file"),
                release_all=bool(body.get("release_all", False)),
            )
            self._send_json(200, res)
            return

        if path == "/leases/check":
            res = self.server.store.check_paths(
                list(body.get("paths") or []),
                agent_id=body.get("agent_id"),
                scope=body.get("scope", "file"),
            )
            self._send_json(200, res)
            return

        if path == "/state/import":
            res = self.server.store.import_state(
                body,
                replace=bool(body.get("replace", True)),
            )
            self._send_json(200, res)
            return

        raise NotFoundError("not_found", f"Unknown endpoint: POST {path}")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch_guarded(self._do_PATCH_impl)

    def _do_PATCH_impl(self) -> None:
        if not self._check_auth():
            return
        if not self._require_json_content_type():
            return
        path = urlparse(self.path).path
        body = self._read_json_body()
        if not isinstance(body, dict):
            raise ValidationError("validation_error", "JSON object body expected")
        if path.startswith("/tasks/"):
            tid = unquote(path[len("/tasks/") :])
            res = self.server.store.update_task(
                tid,
                body.get("agent_id", ""),
                status=body.get("status"),
                result_summary=body.get("result_summary"),
                force=bool(body.get("force", False)),
            )
            self._send_json(200, res)
            return
        raise NotFoundError("not_found", f"Unknown endpoint: PATCH {path}")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch_guarded(self._do_PUT_impl)

    def _do_PUT_impl(self) -> None:
        if not self._check_auth():
            return
        if not self._require_json_content_type():
            return
        path = urlparse(self.path).path
        body = self._read_json_body()
        if not isinstance(body, dict):
            raise ValidationError("validation_error", "JSON object body expected")
        if path.startswith("/context/"):
            key = unquote(path[len("/context/") :])
            if "value" not in body:
                raise ValidationError("validation_error", "'value' field is required")
            res = self.server.store.put_context(
                key,
                body["value"],
                body.get("updated_by", ""),
                expected_version=body.get("expected_version"),
            )
            self._send_json(200, res)
            return
        raise NotFoundError("not_found", f"Unknown endpoint: PUT {path}")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch_guarded(self._do_DELETE_impl)

    def _do_DELETE_impl(self) -> None:
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        if path == "/mcp":
            self._send_json(200, {"closed": True})
            return
        raise NotFoundError("not_found", f"Unknown endpoint: DELETE {path}")


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    db_path: str | Path | None = None,
    token: str | None = None,
    admin_token: str | None = None,
    allow_insecure_lan: bool = False,
    allowed_hosts: list[str] | None = None,
    case_insensitive: bool | None = None,
    pid_lock: bool = True,
    log_file: Path | None = None,
) -> CoordinationHTTPServer:
    store = SyncStore(db_path, case_insensitive=case_insensitive)
    try:
        return CoordinationHTTPServer(
            (host, port),
            store,
            token=token,
            admin_token=admin_token,
            allow_insecure_lan=allow_insecure_lan,
            allowed_hosts=allowed_hosts,
            pid_lock=pid_lock,
            log_file=log_file,
        )
    except Exception:
        store.close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agsync-server",
        description="Run the agsync LAN Coordination HTTP Server (REST + Streamable HTTP /mcp).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=str(default_db_path()))
    parser.add_argument("--token", default=os.environ.get("AGSYNC_TOKEN", ""))
    parser.add_argument("--token-file", default=os.environ.get("AGSYNC_TOKEN_FILE", ""))
    parser.add_argument("--admin-token", default=os.environ.get("AGSYNC_ADMIN_TOKEN", ""))
    parser.add_argument(
        "--auto-token",
        action="store_true",
        help="Auto-generate a Bearer token if none is configured.",
    )
    parser.add_argument(
        "--allow-insecure-lan",
        action="store_true",
        help="Explicitly allow binding non-loopback address without a Bearer token.",
    )
    parser.add_argument(
        "--log-file",
        default=str(default_data_dir() / "logs" / "agsync-server.log"),
        help="Rotating log file path.",
    )
    parser.add_argument(
        "--case-insensitive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Force case-insensitive path matching (defaults to enabled on Windows).",
    )
    parser.add_argument(
        "--discover",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Broadcast a UDP LAN discovery beacon while serving.",
    )
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=DEFAULT_DISCOVERY_PORT,
        help="UDP port for LAN discovery beacons.",
    )
    args = parser.parse_args(argv)

    eff_token = (args.token or "").strip()
    if not eff_token and args.token_file:
        tf = Path(args.token_file).expanduser()
        if tf.is_file():
            eff_token = read_text_auto(tf).strip().splitlines()[0].strip()
    if not eff_token and args.auto_token:
        eff_token = secrets.token_urlsafe(18)

    try:
        srv = create_server(
            host=args.host,
            port=args.port,
            db_path=args.db,
            token=eff_token or None,
            admin_token=(args.admin_token or "").strip() or None,
            allow_insecure_lan=args.allow_insecure_lan,
            case_insensitive=args.case_insensitive,
            pid_lock=True,
            log_file=Path(args.log_file) if args.log_file else None,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        try:
            sys.stderr.write(f"[agsync-server] ERROR: {exc}\n")
        except OSError:
            pass
        return 2

    actual_host, actual_port = srv.server_address[:2]
    beacon: DiscoveryBeacon | None = None
    if args.discover:
        beacon = DiscoveryBeacon(
            http_port=actual_port,
            discovery_port=args.discovery_port,
        )
        beacon.start()
    try:
        sys.stderr.write(
            f"[agsync-server] Listening on http://{actual_host}:{actual_port} "
            f"(db={args.db}, auth={'on' if srv.token else 'off'}, "
            f"discover={'on' if args.discover else 'off'}, "
            f"case_insensitive={srv.store.case_insensitive})\n"
        )
        sys.stderr.flush()
    except OSError:
        pass

    def _stop_handler(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop_handler)

    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        try:
            sys.stderr.write("[agsync-server] Shutting down...\n")
        except OSError:
            pass
    finally:
        if beacon is not None:
            beacon.stop()
        srv.server_close()
        srv.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
