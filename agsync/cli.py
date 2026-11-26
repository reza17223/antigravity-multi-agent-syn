"""CLI client, Git pre-commit guard, and hook installer for agsync."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agsync.client import SyncClient, SyncClientError
from agsync.config import configure_antigravity_mcp, copy_project_templates
from agsync.discovery import DEFAULT_DISCOVERY_PORT, discover_hubs
from agsync.protocol import (
    DEFAULT_AGENT_TTL_SEC,
    DEFAULT_HOST,
    DEFAULT_LEASE_TTL_SEC,
    DEFAULT_PORT,
    LEASE_SCOPES,
    MSG_TYPES,
    TASK_STATUSES,
)
from agsync.server import main as server_main


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists() or (candidate / ".agsync").exists():
            return candidate
    return cur


def _read_local_setting(repo_root: Path, filenames: tuple[str, ...]) -> str | None:
    for rel in filenames:
        p = repo_root / rel
        if p.is_file():
            lines = p.read_text(encoding="utf-8").strip().splitlines()
            if lines and lines[0].strip():
                return lines[0].strip()
    return None


def _resolve_conn(args: argparse.Namespace, repo_root: Path | None = None) -> tuple[str, str, str]:
    root = repo_root or _find_repo_root(Path.cwd())
    server = (
        getattr(args, "server", None)
        or os.environ.get("AGSYNC_SERVER")
        or _read_local_setting(root, (".agsync/server_url",))
        or f"http://127.0.0.1:{DEFAULT_PORT}"
    )
    agent = (
        getattr(args, "agent", None)
        or os.environ.get("AGSYNC_AGENT")
        or _read_local_setting(root, (".agsync-agent", ".agsync/agent_id"))
        or socket.gethostname()
    )
    token = (
        getattr(args, "token", None)
        or os.environ.get("AGSYNC_TOKEN")
        or _read_local_setting(root, (".agsync/token",))
        or ""
    )
    return server, agent, token


def _print_output(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_status_human(snap: dict[str, Any]) -> None:
    agents = snap.get("agents", [])
    tasks = snap.get("tasks", [])
    leases = snap.get("active_leases", [])
    ctx = snap.get("context_keys", [])
    unread = snap.get("unread_count")

    print("=== agsync Status ===")
    if unread is not None:
        print(f"Unread messages: {unread}")
    print(f"Agents ({len(agents)}):")
    for a in agents:
        state = "ONLINE" if a["is_online"] else "OFFLINE"
        branch = a.get("branch") or "-"
        role = a.get("role") or "-"
        print(f"  - {a['agent_id']} [{state}] role={role} branch={branch} host={a.get('machine_name') or '-'}")

    print(f"Active Leases ({len(leases)}):")
    for lease in leases:
        print(
            f"  - {lease['resource_norm']} ({lease['scope']}) -> {lease['owner_id']} "
            f"[token={lease['fencing_token']}] reason={lease.get('reason') or '-'}"
        )

    print(f"Tasks ({len(tasks)}):")
    for t in tasks:
        paths = ",".join(t.get("scope_paths") or []) or "-"
        print(
            f"  - {t['task_id']} [{t['status']}] owner={t.get('owner_id') or '-'} "
            f"paths={paths} :: {t['title']}"
        )

    print(f"Shared Context Keys ({len(ctx)}):")
    for c in ctx:
        print(f"  - {c['key']} (v{c['version']} by {c['updated_by']})")


def _git_staged_files(repo: Path) -> list[str]:
    proc = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACMRDT",
            "-z",
        ],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git diff --cached failed: {err}")
    raw = proc.stdout.decode("utf-8", errors="replace")
    return [item for item in raw.split("\0") if item.strip()]


def _cmd_guard(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    repo_root = _find_repo_root(Path(args.repo).resolve())
    server, agent, token = _resolve_conn(args, repo_root=repo_root)
    allow_offline = bool(args.fail_open or args.allow_offline)

    paths: list[str] = list(args.paths or [])
    if args.staged:
        try:
            staged = _git_staged_files(repo_root)
        except RuntimeError as exc:
            sys.stderr.write(f"[agsync guard] {exc}\n")
            return 2
        paths.extend(staged)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    if not unique_paths:
        if as_json:
            print(json.dumps({"all_clear": True, "checked": []}, indent=2))
        return 0

    client = SyncClient(server, token=token or None, timeout=3.0)
    try:
        res = client.check_paths(unique_paths, agent_id=agent, scope=args.scope)
    except SyncClientError as exc:
        if exc.unreachable and allow_offline:
            sys.stderr.write(
                f"[agsync guard] WARNING: {exc.message} (--allow-offline/--fail-open active, allowing)\n"
            )
            return 0
        sys.stderr.write(f"[agsync guard] ERROR: {exc.message}\n")
        return 2

    blocked = res.get("blocked") or []
    free_paths = res.get("free") or []

    if blocked:
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(f"[agsync guard] BLOCKED for agent '{agent}':\n")
            for item in blocked:
                for c in item["conflicts"]:
                    sys.stderr.write(
                        f"  - {item['path']} conflicts with {c['scope']}='{c['resource_norm']}' "
                        f"held by '{c['owner_id']}' (reason: {c.get('reason') or '-'})\n"
                    )
        return 1

    if args.require_lease and free_paths:
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(
                f"[agsync guard] BLOCKED (--require-lease): agent '{agent}' holds no lease for: "
                f"{', '.join(free_paths)}\n"
            )
        return 1

    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"[agsync guard] OK ({len(unique_paths)} path(s) clear for '{agent}').")
    return 0


def _cmd_install_hooks(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    git_dir = repo / ".git"
    if not git_dir.exists():
        sys.stderr.write(f"Error: '{repo}' is not a Git repository (.git not found).\n")
        return 2
    if git_dir.is_file():
        # Worktree or submodule .git pointer
        text = git_dir.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            git_dir = (repo / text[len("gitdir:") :].strip()).resolve()

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks_dir / "pre-commit"
    if pre_commit.exists() and not args.force:
        existing = pre_commit.read_text(encoding="utf-8", errors="replace")
        if "agsync" not in existing:
            sys.stderr.write(
                f"Error: '{pre_commit}' already exists. Re-run with --force to overwrite.\n"
            )
            return 2

    # Save local .agsync config files if specified so hooks work even when shell env vars aren't set
    agsync_dir = repo / ".agsync"
    agsync_dir.mkdir(parents=True, exist_ok=True)
    if args.server:
        (agsync_dir / "server_url").write_text(args.server.strip() + "\n", encoding="utf-8")
    if args.agent:
        (agsync_dir / "agent_id").write_text(args.agent.strip() + "\n", encoding="utf-8")
    if args.token:
        (agsync_dir / "token").write_text(args.token.strip() + "\n", encoding="utf-8")

    # Add .agsync/, .agsync-agent, and (when untracked) .agents/hooks.json to .git/info/exclude
    # so machine-local identity and absolute hook paths aren't committed.
    exclude_file = git_dir / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing_excl = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    existing_lines = {ln.strip() for ln in existing_excl.splitlines() if ln.strip()}
    exclude_candidates = [".agsync/", ".agsync-agent"]
    if args.with_antigravity_hook:
        tracked_proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".agents/hooks.json"],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )
        if tracked_proc.returncode != 0:
            exclude_candidates.append(".agents/hooks.json")
    to_add = [p for p in exclude_candidates if p not in existing_lines]
    if to_add:
        with exclude_file.open("a", encoding="utf-8") as fh:
            if existing_excl and not existing_excl.endswith("\n"):
                fh.write("\n")
            for p in to_add:
                fh.write(p + "\n")

    cli_py = Path(__file__).resolve().as_posix()
    py_exe = Path(sys.executable).resolve().as_posix()
    extra_flags = ["--staged", "--repo", repo.as_posix()]
    if args.allow_offline:
        extra_flags.append("--allow-offline")
    if args.require_lease:
        extra_flags.append("--require-lease")
    flags_str = " ".join(f'"{f}"' if " " in f else f for f in extra_flags)

    script = (
        "#!/bin/sh\n"
        "# Installed by agsync install-hooks\n"
        f'exec "{py_exe}" "{cli_py}" guard {flags_str}\n'
    )
    pre_commit.write_text(script, encoding="utf-8", newline="\n")
    try:
        pre_commit.chmod(pre_commit.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass

    installed: list[str] = [str(pre_commit)]

    if args.with_antigravity_hook:
        hook_guard_py = (Path(__file__).resolve().parent / "hook_guard.py").as_posix()
        cmd_parts = [f'"{py_exe}"', f'"{hook_guard_py}"']
        if args.fail_closed:
            cmd_parts.append("--fail-closed")
        if args.require_lease:
            cmd_parts.append("--require-lease")
        hook_command = " ".join(cmd_parts)
        if os.name == "nt":
            hook_command = f"call {hook_command}"
        ag_hooks_path = repo / ".agents" / "hooks.json"
        ag_hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hook_cfg = {
            "agsync-lease-guard": {
                "PreToolUse": [
                    {
                        "matcher": "edit_file|write_file|write_to_file|replace_file_content|multi_replace_file_content",
                        "hooks": [
                            {
                                "type": "command",
                                "command": hook_command,
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        ag_hooks_path.write_text(
            json.dumps(hook_cfg, indent=2) + "\n", encoding="utf-8"
        )
        installed.append(str(ag_hooks_path))

    if getattr(args, "with_templates", False):
        copied = copy_project_templates(repo)
        installed.extend(copied)

    result = {
        "installed": installed,
        "repo": str(repo),
        "server": args.server or os.environ.get("AGSYNC_SERVER", "http://127.0.0.1:8765"),
        "agent": args.agent or os.environ.get("AGSYNC_AGENT", socket.gethostname()),
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for path_str in installed:
            print(f"Installed hook: {path_str}")
    return 0


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
    p_serve.add_argument("--db", default=".agsync/state.db")
    p_serve.add_argument("--token", default=os.environ.get("AGSYNC_TOKEN", ""))
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
    p_inst = sub.add_parser("install-hooks", parents=[common], help="Install Git pre-commit (+ optional Antigravity) hook.")
    p_inst.add_argument("--repo", default=".")
    p_inst.add_argument("--with-antigravity-hook", action="store_true")
    p_inst.add_argument("--with-templates", action="store_true", help="Copy AGENTS.md and sync-* skills if missing.")
    p_inst.add_argument("--allow-offline", action="store_true")
    p_inst.add_argument("--require-lease", action="store_true")
    p_inst.add_argument("--fail-closed", action="store_true")
    p_inst.add_argument("--force", action="store_true")

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))

    if args.cmd == "serve":
        serve_argv = [
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--db",
            args.db,
        ]
        if args.token:
            serve_argv.extend(["--token", args.token])
        if args.case_insensitive is True:
            serve_argv.append("--case-insensitive")
        elif args.case_insensitive is False:
            serve_argv.append("--no-case-insensitive")
        if args.discover:
            serve_argv.extend(["--discover", "--discovery-port", str(args.discovery_port)])
        return server_main(serve_argv)

    if args.cmd == "discover":
        hubs = discover_hubs(timeout_sec=args.timeout, discovery_port=args.port)
        _print_output({"hubs": hubs}, as_json=as_json)
        return 0 if hubs else 1

    if args.cmd == "setup-mcp":
        server, agent, token = _resolve_conn(args)
        cfg_path = Path(args.mcp_config).expanduser() if args.mcp_config else None
        res = configure_antigravity_mcp(
            server_url=server,
            agent_id=agent,
            mode=args.mode,
            token=token,
            mcp_config_path=cfg_path,
        )
        _print_output(res, as_json=as_json)
        return 0

    if args.cmd == "guard":
        return _cmd_guard(args)

    if args.cmd == "install-hooks":
        # Normalize optional attributes on args for _cmd_install_hooks
        args.server = getattr(args, "server", None)
        args.agent = getattr(args, "agent", None)
        args.token = getattr(args, "token", None)
        args.json = as_json
        return _cmd_install_hooks(args)

    server, agent, token = _resolve_conn(args)
    client = SyncClient(server, token=token or None)

    try:
        if args.cmd == "register":
            res = client.register(
                agent,
                machine_name=args.machine,
                role=args.role,
                branch=args.branch,
                status=args.status,
                ttl_sec=args.ttl,
            )
            _print_output(res, as_json=as_json)
            return 0

        if args.cmd == "send":
            body = args.body if args.body is not None else " ".join(args.message).strip()
            if not body:
                sys.stderr.write("Error: provide --body or message text.\n")
                return 2
            res = client.send_message(agent, args.to, body, msg_type=args.type)
            _print_output(res, as_json=as_json)
            return 0

        if args.cmd == "inbox":
            res = client.poll_messages(
                agent,
                since_id=args.since,
                unread_only=args.unread,
                auto_ack=args.ack,
                limit=args.limit,
            )
            _print_output(res, as_json=as_json)
            return 0

        if args.cmd == "task":
            if args.task_cmd == "create":
                res = client.create_task(
                    args.title,
                    agent,
                    description=args.desc,
                    scope_paths=args.paths,
                    task_id=args.id,
                )
                _print_output(res, as_json=as_json)
                return 0
            if args.task_cmd == "list":
                res = client.list_tasks(status=args.status, owner_id=args.owner)
                _print_output(res, as_json=as_json)
                return 0
            if args.task_cmd == "claim":
                res = client.claim_task(args.task_id, agent)
                _print_output(res, as_json=as_json)
                return 0
            if args.task_cmd == "update":
                res = client.update_task(
                    args.task_id,
                    agent,
                    status=args.status,
                    result_summary=args.summary,
                    force=args.force,
                )
                _print_output(res, as_json=as_json)
                return 0

        if args.cmd == "lock":
            res = client.acquire_lease(
                args.resource,
                agent,
                scope=args.scope,
                ttl_sec=args.ttl,
                reason=args.reason,
            )
            _print_output(res, as_json=as_json)
            return 0

        if args.cmd == "unlock":
            res = client.release_lease(
                agent,
                lease_id=args.lease_id,
                resource=args.resource,
                scope=args.scope,
                release_all=args.release_all,
            )
            _print_output(res, as_json=as_json)
            return 0

        if args.cmd == "check":
            res = client.check_paths(args.paths, agent_id=agent, scope=args.scope)
            if as_json:
                _print_output(res, as_json=True)
            else:
                _print_output(res, as_json=False)
            return 0 if res.get("all_clear") else 1

        if args.cmd == "context":
            if args.ctx_cmd == "set":
                if args.raw:
                    val: Any = args.value
                else:
                    try:
                        val = json.loads(args.value)
                    except json.JSONDecodeError:
                        val = args.value
                res = client.put_context(
                    args.key, val, agent, expected_version=args.expected_version
                )
                _print_output(res, as_json=as_json)
                return 0
            if args.ctx_cmd == "get":
                res = client.get_context(args.key)
                _print_output(res, as_json=as_json)
                return 0
            if args.ctx_cmd == "list":
                res = client.list_context(prefix=args.prefix)
                _print_output(res, as_json=as_json)
                return 0

        if args.cmd == "status":
            snap = client.snapshot(agent_id=agent)
            if as_json:
                _print_output(snap, as_json=True)
            else:
                _print_status_human(snap)
            return 0

        if args.cmd == "state":
            if args.state_cmd == "export":
                bundle = client.export_state()
                if args.out:
                    out_path = Path(args.out)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(
                        json.dumps(bundle, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    _print_output({"exported_to": str(out_path)}, as_json=as_json)
                else:
                    _print_output(bundle, as_json=True)
                return 0
            if args.state_cmd == "import":
                bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
                res = client.import_state(bundle, replace=not args.merge)
                _print_output(res, as_json=as_json)
                return 0
            if args.state_cmd == "pull":
                src = SyncClient(args.from_server, token=args.from_token or None)
                bundle = src.export_state()
                res = client.import_state(bundle, replace=not args.merge)
                _print_output({"pulled_from": args.from_server, **res}, as_json=as_json)
                return 0

    except SyncClientError as exc:
        err = exc.payload or {"error": "client_error", "message": exc.message}
        if as_json:
            print(json.dumps(err, indent=2, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"Error ({exc.status_code or 'network'}): {exc.message}", file=sys.stderr)
            if "conflicts" in err:
                print(json.dumps(err["conflicts"], indent=2, ensure_ascii=False), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
