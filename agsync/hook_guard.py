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

from agsync.adapters.ide import decode_bytes_auto, read_text_auto
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


def _logical_abs(path: Path | str) -> str:
    """Normalize a path logically without dereferencing Windows Junctions or symlinks."""
    return os.path.abspath(os.path.normpath(str(path)))


def _relative_to_logical(target_abs: str, root_abs: str) -> str | None:
    """Return forward-slash relative path if target_abs is inside root_abs logically."""
    if os.name == "nt":
        t_cmp = os.path.normcase(target_abs)
        r_cmp = os.path.normcase(root_abs)
    else:
        t_cmp = target_abs
        r_cmp = root_abs
    try:
        common = os.path.commonpath([r_cmp, t_cmp])
    except ValueError:
        return None
    if common != r_cmp:
        return None
    rel = os.path.relpath(target_abs, root_abs).replace("\\", "/")
    return "." if rel == "." else rel.strip("/")


def _is_protected_rel_path(rel_path: str) -> bool:
    """Protect agsync config, identity, and hook files from agent modification."""
    norm = rel_path.replace("\\", "/").strip("/")
    if os.name == "nt":
        norm = norm.lower()
    return (
        norm == ".agsync"
        or norm.startswith(".agsync/")
        or norm == ".agsync-agent"
        or norm == ".agents/hooks.json"
    )


def _find_repo_root_strict(start: Path) -> Path | None:
    cur = Path(_logical_abs(start))
    home = Path.home().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
        if candidate.resolve() != home and (candidate / ".agsync").exists():
            return candidate
    return None


def _find_repo_root(start: Path) -> Path:
    return _find_repo_root_strict(start) or Path(_logical_abs(start))


def _read_local_setting(repo_root: Path, filenames: tuple[str, ...]) -> str | None:
    for rel in filenames:
        p = repo_root / rel
        try:
            if p.is_file():
                val = read_text_auto(p).strip().splitlines()
                if val and val[0].strip():
                    return val[0].strip()
        except OSError:
            continue
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
    if not isinstance(tool_call, dict):
        return {"decision": "allow"}
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
    cwd_abs = _logical_abs(str(raw_cwd))
    cwd_path = Path(cwd_abs)

    if os.path.isabs(raw_target):
        target_abs = _logical_abs(raw_target)
    else:
        target_abs = _logical_abs(os.path.join(cwd_abs, raw_target))
    target_path = Path(target_abs)

    candidate_roots: list[Path] = []
    cwd_repo = _find_repo_root_strict(cwd_path)
    if cwd_repo is not None:
        candidate_roots.append(cwd_repo)
    if isinstance(ws_list, list):
        for ws in ws_list:
            if isinstance(ws, str) and ws.strip():
                ws_repo = _find_repo_root_strict(Path(_logical_abs(ws.strip())))
                if ws_repo is not None and ws_repo not in candidate_roots:
                    candidate_roots.append(ws_repo)
    target_repo = _find_repo_root_strict(target_path.parent)
    if target_repo is not None and target_repo not in candidate_roots:
        candidate_roots.append(target_repo)

    # Sort candidates by longest path first (most specific inner repository takes precedence)
    candidate_roots.sort(key=lambda p: len(str(p)), reverse=True)

    repo_root: Path | None = None
    rel_path: str | None = None
    for cand in candidate_roots:
        rel = _relative_to_logical(target_abs, str(cand))
        if rel is not None:
            rel_path = rel
            repo_root = cand
            break

    if repo_root is None or rel_path is None:
        # Target is outside any project repository (e.g. ~/.gemini/ brain/artifacts) -> allow.
        return {"decision": "allow"}

    if not rel_path or rel_path == "." or rel_path.startswith(".git/") or rel_path == ".git":
        return {"decision": "allow"}

    if _is_protected_rel_path(rel_path):
        return {
            "decision": "deny",
            "reason": (
                f"AGSYNC PROTECTED PATH: '{rel_path}' is a protected agsync config/hook file "
                "and cannot be modified by an agent."
            ),
        }

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
            try:
                sys.stderr.write(
                    f"[agsync-hook] Server unreachable ({eff_server}); fail-open allow for {rel_path}\n"
                )
            except OSError:
                pass
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


def _read_stdin_text() -> str:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return decode_bytes_auto(buf.read())
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    fail_closed = False
    try:
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
        fail_closed = bool(args.fail_closed)

        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")

        raw_stdin = _read_stdin_text().lstrip("\ufeff").strip()
        if not raw_stdin:
            out: dict[str, Any] = {"decision": "allow"}
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
                    fail_closed=fail_closed,
                    require_lease=args.require_lease,
                    timeout=args.timeout,
                )
            except json.JSONDecodeError as exc:
                out = (
                    {"decision": "deny", "reason": f"Invalid hook stdin JSON: {exc}"}
                    if fail_closed
                    else {"decision": "allow"}
                )
    except SystemExit:
        raise
    except Exception as exc:
        try:
            sys.stderr.write(f"[agsync-hook] Unexpected error: {exc}\n")
        except OSError:
            pass
        out = (
            {"decision": "deny", "reason": f"agsync hook_guard error: {exc}"}
            if fail_closed
            else {"decision": "allow"}
        )

    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
