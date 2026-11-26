"""External Shell Hooks runner for user-defined commands on agsync lifecycle events."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from agsync.config import load_config

VALID_HOOK_EVENTS = ("on_lock", "on_task_done", "pre_merge")


def run_external_hooks(
    event_name: str,
    *,
    config: dict[str, Any] | None = None,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Run user-configured shell hooks for an event and return execution results."""
    if event_name not in VALID_HOOK_EVENTS:
        raise ValueError(f"Unknown external hook event '{event_name}'; expected one of {VALID_HOOK_EVENTS}")

    cfg = config if config is not None else load_config()
    hooks_cfg = cfg.get("hooks") or {}
    if not isinstance(hooks_cfg, dict):
        return []

    cmds = hooks_cfg.get(event_name) or []
    if isinstance(cmds, str):
        cmds = [cmds]
    if not isinstance(cmds, list):
        return []

    env = dict(os.environ)
    env["AGSYNC_EVENT"] = event_name
    if env_extra:
        for k, v in env_extra.items():
            env[str(k)] = str(v)

    results: list[dict[str, Any]] = []
    work_dir = str(cwd or Path.cwd())
    for raw_cmd in cmds:
        cmd_str = str(raw_cmd or "").strip()
        if not cmd_str:
            continue
        try:
            proc = subprocess.run(
                cmd_str,
                shell=True,
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            results.append(
                {
                    "command": cmd_str,
                    "returncode": int(proc.returncode),
                    "ok": proc.returncode == 0,
                    "stdout": proc.stdout.strip(),
                    "stderr": proc.stderr.strip(),
                }
            )
        except subprocess.TimeoutExpired:
            results.append(
                {
                    "command": cmd_str,
                    "returncode": 124,
                    "ok": False,
                    "stdout": "",
                    "stderr": f"Timed out after {timeout}s",
                }
            )
        except OSError as exc:
            results.append(
                {
                    "command": cmd_str,
                    "returncode": 1,
                    "ok": False,
                    "stdout": "",
                    "stderr": str(exc),
                }
            )
    return results
