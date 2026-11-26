"""CLI client entrypoint and parser wiring agsync.commands modules."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agsync.commands.doctor import cmd_doctor
from agsync.commands.hook_proxy import cmd_hook_proxy
from agsync.commands.hooks import (
    cmd_guard,
    cmd_install_hooks,
    read_local_setting as _read_local_setting,
    resolve_conn as _resolve_conn,
)
from agsync.commands.sync_cmds import (
    cmd_client_dispatch,
    cmd_discover,
    cmd_invite,
    cmd_join,
    cmd_serve,
    cmd_setup_mcp,
)
from agsync.commands.worktree import cmd_worktree
from agsync.core.git_ops import find_repo_root as _find_repo_root
from agsync.db import default_db_path
from agsync.discovery import DEFAULT_DISCOVERY_PORT
from agsync.protocol import (
    DEFAULT_AGENT_TTL_SEC,
    DEFAULT_HOST,
    DEFAULT_LEASE_TTL_SEC,
    DEFAULT_PORT,
    LEASE_SCOPES,
    MSG_TYPES,
    TASK_STATUSES,
)

__all__ = [
    "build_parser",
    "main",
    "_find_repo_root",
    "_read_local_setting",
    "_resolve_conn",
]


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=argparse.SUPPRESS, help="agsync server URL")
    common.add_argument("--agent", default=argparse.SUPPRESS, help="Agent ID")
    common.add_argument("--token", default=argparse.SUPPRESS, help="Bearer token")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")

    parser = argparse.ArgumentParser(
        prog="agsync",
        description="CLI for multi-machine Antigravity coordination & lease locking.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # serve
    p_serve = sub.add_parser("serve", help="Start the LAN Coordination HTTP server.")
    p_serve.add_argument("--host", default=DEFAULT_HOST)
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--db", default=str(default_db_path()))
    p_serve.add_argument("--token", default=os.environ.get("AGSYNC_TOKEN", ""))
    p_serve.add_argument("--token-file", default=os.environ.get("AGSYNC_TOKEN_FILE", ""))
    p_serve.add_argument("--auto-token", action="store_true")
    p_serve.add_argument("--allow-insecure-lan", action="store_true")
    p_serve.add_argument(
        "--case-insensitive",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    p_serve.add_argument(
        "--discover",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Broadcast UDP LAN discovery beacons.",
    )
    p_serve.add_argument(
        "--discovery-port",
        type=int,
        default=DEFAULT_DISCOVERY_PORT,
    )

    # register
    p_reg = sub.add_parser("register", parents=[common], help="Register or heartbeat agent.")
    p_reg.add_argument("--machine", default=socket.gethostname())
    p_reg.add_argument("--role", default="agent")
    p_reg.add_argument("--branch", default="")
    p_reg.add_argument("--status", default="online", choices=["online", "busy", "offline"])
    p_reg.add_argument("--ttl", type=int, default=DEFAULT_AGENT_TTL_SEC)

    # send
    p_send = sub.add_parser("send", parents=[common], help="Send message to peer or '*'.")
    p_send.add_argument("--to", required=True, help="Recipient agent_id or '*'")
    p_send.add_argument("--type", default="info", choices=list(MSG_TYPES))
    p_send.add_argument("--body", default=None, help="Message body")
    p_send.add_argument("message", nargs="*", help="Message text (if --body omitted)")

    # inbox
    p_inbox = sub.add_parser("inbox", parents=[common], help="Poll messages.")
    p_inbox.add_argument("--since", type=int, default=None)
    p_inbox.add_argument("--unread", action="store_true")
    p_inbox.add_argument("--ack", action="store_true")
    p_inbox.add_argument("--limit", type=int, default=100)

    # task
    p_task = sub.add_parser("task", parents=[common], help="Manage shared tasks.")
    task_sub = p_task.add_subparsers(dest="task_cmd", required=True)

    pt_create = task_sub.add_parser("create", parents=[common], help="Create a task.")
    pt_create.add_argument("title", help="Task title")
    pt_create.add_argument("--desc", default="", help="Task description")
    pt_create.add_argument("--paths", nargs="*", default=[], help="Target scope paths")
    pt_create.add_argument("--id", default=None, help="Optional explicit task ID")

    pt_list = task_sub.add_parser("list", parents=[common], help="List tasks.")
    pt_list.add_argument("--status", default=None, choices=list(TASK_STATUSES))
    pt_list.add_argument("--owner", default=None)

    pt_claim = task_sub.add_parser("claim", parents=[common], help="Claim an open task.")
    pt_claim.add_argument("task_id")

    pt_upd = task_sub.add_parser("update", parents=[common], help="Update a task.")
    pt_upd.add_argument("task_id")
    pt_upd.add_argument("--status", default=None, choices=list(TASK_STATUSES))
    pt_upd.add_argument("--summary", default=None)
    pt_upd.add_argument("--force", action="store_true")

    # lock
    p_lock = sub.add_parser("lock", parents=[common], help="Acquire exclusive lease.")
    p_lock.add_argument("resource", help="Repo-relative path or symbol")
    p_lock.add_argument("--scope", default="file", choices=list(LEASE_SCOPES))
    p_lock.add_argument("--ttl", type=int, default=DEFAULT_LEASE_TTL_SEC)
    p_lock.add_argument("--reason", default="")

    # unlock
    p_unlock = sub.add_parser("unlock", parents=[common], help="Release lease(s).")
    p_unlock.add_argument("resource", nargs="?", default=None)
    p_unlock.add_argument("--lease-id", default=None)
    p_unlock.add_argument("--scope", default="file", choices=list(LEASE_SCOPES))
    p_unlock.add_argument("--all", action="store_true", dest="release_all")

    # check
    p_check = sub.add_parser("check", parents=[common], help="Check if paths are clear.")
    p_check.add_argument("paths", nargs="+")
    p_check.add_argument("--scope", default="file", choices=list(LEASE_SCOPES))

    # context
    p_ctx = sub.add_parser("context", parents=[common], help="Shared context key-value store.")
    ctx_sub = p_ctx.add_subparsers(dest="ctx_cmd", required=True)

    pc_set = ctx_sub.add_parser("set", parents=[common], help="Publish/update a context key.")
    pc_set.add_argument("key")
    pc_set.add_argument("value", help="JSON value (or plain string if not valid JSON)")
    pc_set.add_argument("--expected-version", type=int, default=None)
    pc_set.add_argument("--raw", action="store_true", help="Store value strictly as a string")

    pc_get = ctx_sub.add_parser("get", parents=[common], help="Get a context key.")
    pc_get.add_argument("key")

    pc_list = ctx_sub.add_parser("list", parents=[common], help="List context keys.")
    pc_list.add_argument("--prefix", default=None)

    # status
    sub.add_parser("status", parents=[common], help="Show live coordination snapshot.")

    # guard
    p_guard = sub.add_parser("guard", parents=[common], help="Pre-commit / pre-edit lease guard.")
    p_guard.add_argument("paths", nargs="*")
    p_guard.add_argument("--staged", action="store_true", help="Inspect git staged files.")
    p_guard.add_argument("--repo", default=".")
    p_guard.add_argument("--scope", default="file", choices=list(LEASE_SCOPES))
    p_guard.add_argument("--fail-open", action="store_true")
    p_guard.add_argument("--allow-offline", action="store_true")
    p_guard.add_argument("--require-lease", action="store_true")

    # install-hooks
    p_inst = sub.add_parser(
        "install-hooks",
        parents=[common],
        help="Install Git pre-commit (+ optional Antigravity) hook.",
    )
    p_inst.add_argument("--repo", default=".")
    p_inst.add_argument("--with-antigravity-hook", action="store_true")
    p_inst.add_argument("--with-templates", action="store_true", help="Copy AGENTS.md and sync-* skills if missing.")
    p_inst.add_argument("--allow-offline", action="store_true")
    p_inst.add_argument("--require-lease", action="store_true")
    p_inst.add_argument("--fail-closed", action="store_true")
    p_inst.add_argument("--force", action="store_true")

    # doctor
    p_doc = sub.add_parser("doctor", parents=[common], help="Run environment & setup diagnostics.")
    p_doc.add_argument("--repo", default=".")
    p_doc.add_argument("--db", default=None, help="Override state DB path")
    p_doc.add_argument("--mcp-config", default=None, help="Override mcp_config.json path")

    # discover
    p_disc = sub.add_parser("discover", parents=[common], help="Discover active agsync hubs on the LAN.")
    p_disc.add_argument("--timeout", type=float, default=3.0)
    p_disc.add_argument("--port", type=int, default=DEFAULT_DISCOVERY_PORT)

    # setup-mcp
    p_mcp = sub.add_parser("setup-mcp", parents=[common], help="Configure Antigravity global mcp_config.json.")
    p_mcp.add_argument("--mode", choices=["http", "stdio"], default="http")
    p_mcp.add_argument("--mcp-config", default=None, help="Override path to mcp_config.json")

    # state
    p_state = sub.add_parser("state", parents=[common], help="Export, import, or pull hub state for host handoff.")
    state_sub = p_state.add_subparsers(dest="state_cmd", required=True)
    ps_exp = state_sub.add_parser("export", parents=[common], help="Export hub state JSON.")
    ps_exp.add_argument("--out", default=None, help="Output JSON file path (default: stdout)")

    ps_imp = state_sub.add_parser("import", parents=[common], help="Import state JSON into hub.")
    ps_imp.add_argument("bundle", help="Path to exported state JSON file")
    ps_imp.add_argument("--merge", action="store_true", help="Upsert instead of replacing existing rows")

    ps_pull = state_sub.add_parser("pull", parents=[common], help="Pull state from --from-server into --server.")
    ps_pull.add_argument("--from-server", required=True, help="Source hub URL to export from")
    ps_pull.add_argument("--from-token", default="", help="Optional Bearer token for source hub")
    ps_pull.add_argument("--merge", action="store_true", help="Upsert instead of replacing existing rows")

    # hook-proxy
    p_hp = sub.add_parser("hook-proxy", parents=[common], help="Multi-IDE PreToolUse hook proxy.")
    p_hp.add_argument("--host", required=True, choices=["antigravity", "claude", "cursor"])
    p_hp.add_argument("--record", action="store_true", help="Record payload samples to .agsync/hook-samples/")
    p_hp.add_argument("--chain", default=None, help="Optional pre-existing user hook command to chain")
    p_hp.add_argument("--chain-timeout", type=float, default=5.0)
    p_hp.add_argument("--ask", action="store_true", help="Emit 'ask' instead of 'deny' for Antigravity")
    p_hp.add_argument("--fail-closed", action="store_true")
    p_hp.add_argument("--require-lease", action="store_true")
    p_hp.add_argument("--timeout", type=float, default=2.0)

    # worktree
    p_wt = sub.add_parser("worktree", parents=[common], help="Opt-in git worktree management.")
    p_wt.add_argument("--repo", default=".")
    wt_sub = p_wt.add_subparsers(dest="wt_cmd", required=True)
    wt_create = wt_sub.add_parser("create", help="Create sibling worktree.")
    wt_create.add_argument("--agent", required=True, help="Explicit agent_id for .agsync-agent")
    wt_create.add_argument("--branch", default=None)
    wt_create.add_argument("--no-link-deps", action="store_true")
    wt_sub.add_parser("list", parents=[common], help="List git worktrees.")
    wt_rm = wt_sub.add_parser("remove", parents=[common], help="Remove a git worktree.")
    wt_rm.add_argument("path", help="Worktree directory path")
    wt_rm.add_argument("--force", action="store_true")

    # invite & join
    p_inv = sub.add_parser("invite", parents=[common], help="Create a single-use HMAC Join Link.")
    p_inv.add_argument("--target-agent", required=True, help="Agent ID for the invited peer")
    p_inv.add_argument("--ttl", type=int, default=900)
    p_inv.add_argument("--base-url", default=None, help="Public/tunnel base URL for join link")

    p_join = sub.add_parser("join", parents=[common], help="Consume a Join Link to obtain a per-agent token.")
    p_join.add_argument("join_url", help="Join URL or nonce")
    p_join.add_argument("--sig", default=None, help="Signature (if passing raw nonce)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))

    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "discover":
        return cmd_discover(args, as_json=as_json)
    if args.cmd == "setup-mcp":
        return cmd_setup_mcp(args, as_json=as_json)
    if args.cmd == "guard":
        return cmd_guard(args)
    if args.cmd == "install-hooks":
        args.server = getattr(args, "server", None)
        args.agent = getattr(args, "agent", None)
        args.token = getattr(args, "token", None)
        args.json = as_json
        return cmd_install_hooks(args)
    if args.cmd == "doctor":
        args.json = as_json
        return cmd_doctor(args)
    if args.cmd == "hook-proxy":
        return cmd_hook_proxy(args)
    if args.cmd == "worktree":
        args.json = as_json
        return cmd_worktree(args)
    if args.cmd == "invite":
        return cmd_invite(args, as_json=as_json)
    if args.cmd == "join":
        return cmd_join(args, as_json=as_json)

    return cmd_client_dispatch(args, as_json=as_json)


if __name__ == "__main__":
    raise SystemExit(main())
