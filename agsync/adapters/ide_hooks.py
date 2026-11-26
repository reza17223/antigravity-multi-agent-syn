"""Multi-IDE PreToolUse hook translation, recording, deduplication, and safe chaining."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from agsync.hook_guard import WRITE_TOOLS, evaluate_hook_payload

SUPPORTED_IDES = ("antigravity", "claude", "cursor")


def normalize_ide_payload(host: str, raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Translate Antigravity, Claude Code, or Cursor hook payloads into canonical hook_guard shape."""
    ide = host.strip().lower()
    if ide == "antigravity":
        return raw_payload

    if ide in ("claude", "cursor"):
        # Claude Code & Cursor use snake_case: tool_name, tool_input, cwd
        if "toolCall" in raw_payload and isinstance(raw_payload["toolCall"], dict):
            return raw_payload
        tool_name = str(raw_payload.get("tool_name") or raw_payload.get("toolName") or "").strip()
        tool_input = raw_payload.get("tool_input") or raw_payload.get("toolInput") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        # Map common Claude/Cursor tool names to WRITE_TOOLS
        name_map = {
            "edit": "edit_file",
            "write": "write_file",
            "multiedit": "multi_replace_file_content",
            "str_replace_editor": "edit_file",
            "create_file": "write_file",
        }
        mapped_name = name_map.get(tool_name.lower(), tool_name)
        out: dict[str, Any] = {
            "toolCall": {"name": mapped_name, "args": tool_input},
        }
        if "cwd" in raw_payload:
            out["cwd"] = raw_payload["cwd"]
        if "workspace_roots" in raw_payload and isinstance(raw_payload["workspace_roots"], list):
            out["workspacePaths"] = raw_payload["workspace_roots"]
        elif "workspacePaths" in raw_payload:
            out["workspacePaths"] = raw_payload["workspacePaths"]
        return out

    return raw_payload


def record_hook_sample(repo_root: Path, host: str, raw_payload: dict[str, Any]) -> Path:
    """Save a real IDE hook payload sample under .agsync/hook-samples/."""
    samples_dir = repo_root / ".agsync" / "hook-samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    short = uuid.uuid4().hex[:6]
    dest = samples_dir / f"{host.strip().lower()}-{ts}-{short}.json"
    dest.write_text(json.dumps(raw_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dest


def _kill_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Terminate a spawned hook process and its entire child tree (Windows-safe)."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_chained_hook(
    command: str,
    raw_stdin: str,
    *,
    cwd: Path,
    timeout_sec: float = 5.0,
) -> tuple[int, str, str]:
    """Execute a pre-existing user hook command with timeout and process-tree cleanup."""
    if not command or not command.strip():
        return 0, "", ""
    env = dict(os.environ)
    env["AGSYNC_HOOK_PROXY_ACTIVE"] = "1"
    if os.name == "nt":
        proc = subprocess.Popen(
            f"cmd /c {command}",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            shell=True,
        )
    else:
        proc = subprocess.Popen(
            ["sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
    try:
        out_b, err_b = proc.communicate(input=raw_stdin.encode("utf-8"), timeout=timeout_sec)
        return (
            int(proc.returncode or 0),
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
        return 124, "", f"Chained user hook timed out after {timeout_sec}s"


def format_ide_response(
    host: str,
    decision_obj: dict[str, Any],
    *,
    ask_mode: bool = False,
) -> tuple[int, str, str]:
    """Format hook_guard decision into (exit_code, stdout, stderr) for the target IDE."""
    ide = host.strip().lower()
    decision = str(decision_obj.get("decision") or "allow").lower()
    reason = str(decision_obj.get("reason") or "")

    if ide == "claude":
        if decision == "allow":
            return 0, "", ""
        # Claude Code blocks PreToolUse via exit code 2 + stderr message
        return 2, "", reason or "Blocked by agsync lease guard"

    if ide == "cursor":
        if decision == "allow":
            return 0, json.dumps({"continue": True}, ensure_ascii=False) + "\n", ""
        return (
            0,
            json.dumps(
                {"continue": False, "permission": "deny", "userMessage": reason},
                ensure_ascii=False,
            )
            + "\n",
            reason,
        )

    # Antigravity (default)
    if decision != "allow" and ask_mode:
        out = {"decision": "ask", "reason": reason}
    else:
        out = decision_obj
    return 0, json.dumps(out, ensure_ascii=False) + "\n", ""


def evaluate_proxy(
    host: str,
    raw_payload: dict[str, Any],
    *,
    server_url: str | None = None,
    agent_id: str | None = None,
    token: str | None = None,
    fail_closed: bool = False,
    require_lease: bool = False,
    record: bool = False,
    chain_cmd: str | None = None,
    chain_timeout: float = 5.0,
    ask_mode: bool = False,
    timeout: float = 2.0,
) -> tuple[int, str, str]:
    """Run full hook-proxy pipeline: dedup -> optional record -> lease check -> optional chain."""
    ide = host.strip().lower()
    if ide not in SUPPORTED_IDES:
        raise ValueError(f"Unsupported --host '{host}'; expected one of {SUPPORTED_IDES}")

    # Prevent duplicate execution when Cursor also loads .claude/ hooks
    if ide == "cursor" and os.environ.get("AGSYNC_HOOK_PROXY_ACTIVE") == "1":
        return format_ide_response(ide, {"decision": "allow"}, ask_mode=ask_mode)

    raw_cwd = raw_payload.get("cwd") or os.getcwd()
    cwd_path = Path(os.path.abspath(os.path.normpath(str(raw_cwd))))

    if record:
        try:
            record_hook_sample(cwd_path, ide, raw_payload)
        except OSError:
            pass

    canonical = normalize_ide_payload(ide, raw_payload)
    decision_obj = evaluate_hook_payload(
        canonical,
        server_url=server_url,
        agent_id=agent_id,
        token=token,
        fail_closed=fail_closed,
        require_lease=require_lease,
        timeout=timeout,
    )

    if decision_obj.get("decision") == "allow" and chain_cmd:
        rc, c_out, c_err = run_chained_hook(
            chain_cmd,
            json.dumps(raw_payload, ensure_ascii=False),
            cwd=cwd_path,
            timeout_sec=chain_timeout,
        )
        if rc != 0:
            return format_ide_response(
                ide,
                {"decision": "deny", "reason": c_err.strip() or c_out.strip() or f"Chained hook exited {rc}"},
                ask_mode=ask_mode,
            )

    return format_ide_response(ide, decision_obj, ask_mode=ask_mode)
