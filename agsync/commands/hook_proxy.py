"""CLI handler for `agsync hook-proxy --host <ide>`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agsync.adapters.ide import decode_bytes_auto
from agsync.adapters.ide_hooks import evaluate_proxy


def _read_stdin_text() -> str:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return decode_bytes_auto(buf.read())
    return sys.stdin.read()


def cmd_hook_proxy(args: argparse.Namespace) -> int:
    host = str(args.host).strip().lower()
    try:
        raw_text = _read_stdin_text().lstrip("\ufeff").strip()
        payload: dict[str, Any] = json.loads(raw_text) if raw_text else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception as exc:
        if args.fail_closed:
            if host == "claude":
                sys.stderr.write(f"Invalid hook JSON: {exc}\n")
                return 2
            sys.stdout.write(json.dumps({"decision": "deny", "reason": str(exc)}) + "\n")
            return 0
        payload = {}

    rc, out, err = evaluate_proxy(
        host,
        payload,
        server_url=getattr(args, "server", None),
        agent_id=getattr(args, "agent", None),
        token=getattr(args, "token", None),
        fail_closed=bool(args.fail_closed),
        require_lease=bool(args.require_lease),
        record=bool(args.record),
        chain_cmd=getattr(args, "chain", None),
        chain_timeout=float(getattr(args, "chain_timeout", 5.0)),
        ask_mode=bool(getattr(args, "ask", False)),
        timeout=float(getattr(args, "timeout", 2.0)),
    )
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
        sys.stderr.flush()
    return rc
