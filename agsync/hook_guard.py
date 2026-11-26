"""Antigravity PreToolUse lifecycle hook handler enforcing active agsync leases."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agsync.client import SyncClient, SyncClientError

WRITE_TOOLS = frozenset(
    {
        "edit_file",
        "write_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "edit",
        "write",
    }
)

PATH_ARG_KEYS = (
    "Path",
    "TargetFile",
    "path",
    "target_file",
    "file_path",
    "filePath",
    "filename",
)


def _find_repo_root_strict(start: Path) -> Path | None:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists() or (candidate / ".agsync").exists():
            return candidate
    return None


def _find_repo_root(start: Path) -> Path:
    return _find_repo_root_strict(start) or start.resolve()


def _read_local_setting(repo_root: Path, filenames: tuple[str, ...]) -> str | None:
    for rel in filenames:
        p = repo_root / rel
        if p.is_file():
            val = p.read_text(encoding="utf-8").strip().splitlines()
            if val and val[0].strip():
                return val[0].strip()
    return None


def _extract_target_path(args: dict[str, Any]) -> str | None:
    for key in PATH_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def evaluate_hook_payload(
    payload: dict[str, Any],
    *,
    server_url: str | None = None,
    agent_id: str | None = None,
    token: str | None = None,
    fail_closed: bool = False,
    require_lease: bool = False,
    timeout: float = 2.0,
) -> dict[str, Any]:
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "").strip().lower()
    if tool_name not in WRITE_TOOLS:
        return {"decision": "allow"}

    tool_args = tool_call.get("args")
    if not isinstance(tool_args, dict):
        return {"decision": "allow"}

    raw_target = _extract_target_path(tool_args)
    if not raw_target:
        return {"decision": "allow"}

    raw_cwd = payload.get("cwd")
    ws_list = payload.get("workspacePaths") or []
    if not raw_cwd:
        raw_cwd = ws_list[0] if isinstance(ws_list, list) and ws_list else os.getcwd()
    cwd_path = Path(str(raw_cwd)).resolve()

    target_path = Path(raw_target)
    if not target_path.is_absolute():
        target_path = cwd_path / target_path
    resolved_target = target_path.resolve()

    candidate_roots: list[Path] = []
    cwd_repo = _find_repo_root_strict(cwd_path)
    if cwd_repo is not None:
        candidate_roots.append(cwd_repo)
    if isinstance(ws_list, list):
        for ws in ws_list:
            if isinstance(ws, str) and ws.strip():
                ws_repo = _find_repo_root_strict(Path(ws.strip()).resolve())
                if ws_repo is not None and ws_repo not in candidate_roots:
                    candidate_roots.append(ws_repo)
    target_repo = _find_repo_root_strict(resolved_target.parent)
    if target_repo is not None and target_repo not in candidate_roots:
        candidate_roots.append(target_repo)

    repo_root: Path | None = None
    rel_path: str | None = None
    for cand in candidate_roots:
        try:
            rel_path = resolved_target.relative_to(cand).as_posix()
            repo_root = cand
            break
        except ValueError:
            continue

    if repo_root is None or rel_path is None:
        # Target is outside any project repository (e.g. ~/.gemini/ brain/artifacts) -> allow.
        return {"decision": "allow"}

    if not rel_path or rel_path == "." or rel_path.startswith(".git/"):
        return {"decision": "allow"}

    eff_server = (
        server_url
        or os.environ.get("AGSYNC_SERVER")
        or _read_local_setting(repo_root, (".agsync/server_url",))
        or "http://127.0.0.1:8765"
    )
    eff_agent = (
        agent_id
        or os.environ.get("AGSYNC_AGENT")
        or _read_local_setting(repo_root, (".agsync-agent", ".agsync/agent_id"))
        or socket.gethostname()
    )
    eff_token = (
        token
        or os.environ.get("AGSYNC_TOKEN")
        or _read_local_setting(repo_root, (".agsync/token",))
        or ""
    )

    client = SyncClient(eff_server, token=eff_token or None, timeout=timeout)
    try:
        res = client.check_paths([rel_path], agent_id=eff_agent, scope="file")
    except SyncClientError as exc:
        if exc.unreachable and not fail_closed:
            sys.stderr.write(
                f"[agsync-hook] Server unreachable ({eff_server}); fail-open allow for {rel_path}\n"
            )
            return {"decision": "allow"}
        return {
            "decision": "deny",
            "reason": (
                f"agsync PreToolUse guard blocked '{rel_path}': {exc.message}. "
                "Start the agsync server or check AGSYNC_SERVER."
            ),
        }

    blocked = res.get("blocked") or []
    if blocked:
        conflict = blocked[0]["conflicts"][0]
        holder = conflict.get("owner_id", "peer")
        scope = conflict.get("scope", "file")
        locked_res = conflict.get("resource_norm", rel_path)
        reason = conflict.get("reason") or "no reason specified"
        return {
            "decision": "deny",
            "reason": (
                f"AGSYNC LEASE CONFLICT: '{rel_path}' is currently locked ({scope}='{locked_res}') "
                f"by '{holder}' (reason: {reason}). Coordinate via sync_send_message or work on a different file."
            ),
        }

    if require_lease and not (res.get("owned_by_self") or []):
        return {
            "decision": "deny",
            "reason": (
                f"AGSYNC LEASE REQUIRED: '{eff_agent}' does not hold an active lease on '{rel_path}'. "
                "Call sync_acquire_lease before editing."
            ),
        }

    return {"decision": "allow"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agsync-hook-guard",
        description="Antigravity PreToolUse hook checking agsync file leases via stdin/stdout JSON.",
    )
    parser.add_argument("--server", default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument(
        "--fail-closed",
        action="store_true",
        help="Deny edits if the agsync server is unreachable (default: fail-open).",
    )
    parser.add_argument(
        "--require-lease",
        action="store_true",
        help="Require this agent to already hold an active lease before allowing edit/write.",
    )
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    raw_stdin = sys.stdin.read().lstrip("\ufeff").strip()
    if not raw_stdin:
        out = {"decision": "allow"}
    else:
        try:
            payload = json.loads(raw_stdin)
            if not isinstance(payload, dict):
                payload = {}
            out = evaluate_hook_payload(
                payload,
                server_url=args.server,
                agent_id=args.agent,
                token=args.token,
                fail_closed=args.fail_closed,
                require_lease=args.require_lease,
                timeout=args.timeout,
            )
        except json.JSONDecodeError as exc:
            out = (
                {"decision": "deny", "reason": f"Invalid hook stdin JSON: {exc}"}
                if args.fail_closed
                else {"decision": "allow"}
            )

    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
