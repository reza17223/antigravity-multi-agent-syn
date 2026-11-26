"""Standard coordination CLI commands (register, messaging, tasks, leases, context, state)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agsync.adapters.ide import atomic_write_text, read_text_auto
from agsync.client import SyncClient, SyncClientError
from agsync.commands.hooks import resolve_conn
from agsync.config import configure_antigravity_mcp
from agsync.discovery import discover_hubs
from agsync.server import main as server_main


def print_output(data: Any, *, as_json: bool) -> None:
    _ = as_json
    print(json.dumps(data, indent=2, ensure_ascii=False))


def print_status_human(snap: dict[str, Any]) -> None:
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
        print(
            f"  - {a['agent_id']} [{state}] role={role} branch={branch} "
            f"host={a.get('machine_name') or '-'}"
        )

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


def cmd_serve(args: argparse.Namespace) -> int:
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
    if getattr(args, "token_file", ""):
        serve_argv.extend(["--token-file", args.token_file])
    if getattr(args, "auto_token", False):
        serve_argv.append("--auto-token")
    if getattr(args, "allow_insecure_lan", False):
        serve_argv.append("--allow-insecure-lan")
    if args.case_insensitive is True:
        serve_argv.append("--case-insensitive")
    elif args.case_insensitive is False:
        serve_argv.append("--no-case-insensitive")
    if args.discover:
        serve_argv.extend(["--discover", "--discovery-port", str(args.discovery_port)])
    return server_main(serve_argv)


def cmd_discover(args: argparse.Namespace, *, as_json: bool) -> int:
    hubs = discover_hubs(timeout_sec=args.timeout, discovery_port=args.port)
    print_output({"hubs": hubs}, as_json=as_json)
    return 0 if hubs else 1


def cmd_setup_mcp(args: argparse.Namespace, *, as_json: bool) -> int:
    server, agent, token = resolve_conn(args)
    cfg_path = Path(args.mcp_config).expanduser() if args.mcp_config else None
    res = configure_antigravity_mcp(
        server_url=server,
        agent_id=agent,
        mode=args.mode,
        token=token,
        mcp_config_path=cfg_path,
    )
    print_output(res, as_json=as_json)
    return 0


def cmd_client_dispatch(args: argparse.Namespace, *, as_json: bool) -> int:
    server, agent, token = resolve_conn(args)
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
            print_output(res, as_json=as_json)
            return 0

        if args.cmd == "send":
            body = args.body if args.body is not None else " ".join(args.message).strip()
            if not body:
                sys.stderr.write("Error: provide --body or message text.\n")
                return 2
            res = client.send_message(agent, args.to, body, msg_type=args.type)
            print_output(res, as_json=as_json)
            return 0

        if args.cmd == "inbox":
            res = client.poll_messages(
                agent,
                since_id=args.since,
                unread_only=args.unread,
                auto_ack=args.ack,
                limit=args.limit,
            )
            print_output(res, as_json=as_json)
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
                print_output(res, as_json=as_json)
                return 0
            if args.task_cmd == "list":
                res = client.list_tasks(status=args.status, owner_id=args.owner)
                print_output(res, as_json=as_json)
                return 0
            if args.task_cmd == "claim":
                res = client.claim_task(args.task_id, agent)
                print_output(res, as_json=as_json)
                return 0
            if args.task_cmd == "update":
                res = client.update_task(
                    args.task_id,
                    agent,
                    status=args.status,
                    result_summary=args.summary,
                    force=args.force,
                )
                print_output(res, as_json=as_json)
                return 0

        if args.cmd == "lock":
            res = client.acquire_lease(
                args.resource,
                agent,
                scope=args.scope,
                ttl_sec=args.ttl,
                reason=args.reason,
            )
            print_output(res, as_json=as_json)
            return 0

        if args.cmd == "unlock":
            res = client.release_lease(
                agent,
                lease_id=args.lease_id,
                resource=args.resource,
                scope=args.scope,
                release_all=args.release_all,
            )
            print_output(res, as_json=as_json)
            return 0

        if args.cmd == "check":
            res = client.check_paths(args.paths, agent_id=agent, scope=args.scope)
            print_output(res, as_json=as_json)
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
                print_output(res, as_json=as_json)
                return 0
            if args.ctx_cmd == "get":
                res = client.get_context(args.key)
                print_output(res, as_json=as_json)
                return 0
            if args.ctx_cmd == "list":
                res = client.list_context(prefix=args.prefix)
                print_output(res, as_json=as_json)
                return 0

        if args.cmd == "status":
            snap = client.snapshot(agent_id=agent)
            if as_json:
                print_output(snap, as_json=True)
            else:
                print_status_human(snap)
            return 0

        if args.cmd == "state":
            if args.state_cmd == "export":
                bundle = client.export_state()
                if args.out:
                    out_path = Path(args.out)
                    atomic_write_text(
                        out_path,
                        json.dumps(bundle, indent=2, ensure_ascii=False) + "\n",
                        create_backup=False,
                    )
                    print_output({"exported_to": str(out_path)}, as_json=as_json)
                else:
                    print_output(bundle, as_json=True)
                return 0
            if args.state_cmd == "import":
                bundle = json.loads(read_text_auto(Path(args.bundle)))
                res = client.import_state(bundle, replace=not args.merge)
                print_output(res, as_json=as_json)
                return 0
            if args.state_cmd == "pull":
                src = SyncClient(args.from_server, token=args.from_token or None)
                bundle = src.export_state()
                res = client.import_state(bundle, replace=not args.merge)
                print_output({"pulled_from": args.from_server, **res}, as_json=as_json)
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
