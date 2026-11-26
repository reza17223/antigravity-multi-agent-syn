"""Diagnostic suite (`agsync doctor`) checking environment, configs, network, and hooks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agsync.adapters.ide import detect_bom_encoding, inspect_mcp_entry, read_text_auto
from agsync.adapters.network import (
    inspect_pid_lockfile,
    inspect_system_proxy,
    inspect_windows_network_profiles,
    is_local_or_private_host,
    tcp_check,
)
from agsync.commands.hooks import resolve_conn
from agsync.config import default_mcp_config_path, load_config
from agsync.core.git_ops import (
    detect_git_version,
    find_repo_root,
    git_common_dir,
    git_hooks_dir,
)
from agsync.db import default_db_path
from agsync.hook_guard import evaluate_hook_payload


def _check_result(
    name: str, status: str, detail: str, fix: str = ""
) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def run_doctor(
    *,
    repo: Path | None = None,
    server_url: str | None = None,
    db_path: Path | None = None,
    mcp_config_path: Path | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    repo_root = find_repo_root(repo or Path.cwd())
    cfg = load_config()
    eff_db = db_path or default_db_path()
    eff_mcp = mcp_config_path or default_mcp_config_path()

    # 1. Python version
    py_ver = sys.version_info[:3]
    if sys.version_info >= (3, 12):
        checks.append(_check_result("python", "ok", f"Python {py_ver[0]}.{py_ver[1]}.{py_ver[2]}"))
    else:
        checks.append(
            _check_result(
                "python",
                "fail",
                f"Python {py_ver[0]}.{py_ver[1]}.{py_ver[2]} < 3.12",
                "Upgrade to Python 3.12+.",
            )
        )

    # 2. Git version (>= 2.38 for future merge-tree)
    gver, graw = detect_git_version()
    if gver is None:
        checks.append(_check_result("git", "fail", f"Git unavailable: {graw}", "Install Git >= 2.38."))
    elif gver < (2, 38, 0):
        checks.append(
            _check_result(
                "git",
                "warn",
                f"{graw} (< 2.38; git merge-tree --write-tree requires >= 2.38)",
                "Upgrade Git to >= 2.38.",
            )
        )
    else:
        checks.append(_check_result("git", "ok", graw))

    # 3. Git worktree common-dir & hooks path
    if (repo_root / ".git").exists():
        try:
            cdir = git_common_dir(repo_root)
            hdir = git_hooks_dir(repo_root)
            is_wt = (repo_root / ".git").is_file()
            checks.append(
                _check_result(
                    "git_common_dir",
                    "ok",
                    f"common_dir={cdir}, hooks_dir={hdir}, worktree={is_wt}",
                )
            )
        except RuntimeError as exc:
            checks.append(
                _check_result(
                    "git_common_dir",
                    "fail",
                    str(exc),
                    "Ensure target --repo is a valid Git repository or worktree.",
                )
            )
    else:
        checks.append(
            _check_result(
                "git_common_dir",
                "warn",
                f"'{repo_root}' is not a Git repository",
                "Run inside a Git repo or pass --repo <path>.",
            )
        )

    # 4. BOM / UTF-16 & JSON config encoding check
    cfg_files = [
        repo_root / ".agsync-agent",
        repo_root / ".agsync" / "server_url",
        repo_root / ".agsync" / "agent_id",
        repo_root / ".agsync" / "token",
        repo_root / ".agents" / "hooks.json",
        eff_mcp,
    ]
    bom_warnings: list[str] = []
    json_errors: list[str] = []
    for cf in cfg_files:
        if not cf.is_file():
            continue
        try:
            raw_bytes = cf.read_bytes()
            bom = detect_bom_encoding(raw_bytes)
            if bom:
                bom_warnings.append(f"{cf} ({bom})")
            if cf.suffix == ".json":
                text = read_text_auto(cf).strip()
                if text:
                    json.loads(text)
        except Exception as exc:
            json_errors.append(f"{cf}: {exc}")

    if json_errors:
        checks.append(
            _check_result(
                "config_encoding",
                "fail",
                "Invalid JSON config: " + "; ".join(json_errors),
                "Fix malformed JSON or re-run setup-mcp / install-hooks.",
            )
        )
    elif bom_warnings:
        checks.append(
            _check_result(
                "config_encoding",
                "warn",
                "BOM/UTF-16 detected (agsync handles it, other tools may fail): "
                + ", ".join(bom_warnings),
                "Re-save affected files as UTF-8 without BOM.",
            )
        )
    else:
        checks.append(_check_result("config_encoding", "ok", "No BOM/UTF-16 or JSON errors found"))

    # 5. MCP config serverUrl vs url compatibility
    if eff_mcp.is_file():
        try:
            mcp_data = json.loads(read_text_auto(eff_mcp))
            servers = (mcp_data.get("mcpServers") or {}) if isinstance(mcp_data, dict) else {}
            ag_entry = servers.get("agsync") if isinstance(servers, dict) else None
            if ag_entry is None:
                checks.append(
                    _check_result(
                        "mcp_config",
                        "warn",
                        f"'agsync' entry not yet configured in {eff_mcp}",
                        "Run `agsync setup-mcp` or click 'Connect Antigravity MCP'.",
                    )
                )
            else:
                info = inspect_mcp_entry(ag_entry)
                if info["issues"]:
                    checks.append(
                        _check_result(
                            "mcp_config",
                            "warn",
                            "; ".join(info["issues"]),
                            "Run `agsync setup-mcp` to normalize to canonical 'url' (/mcp).",
                        )
                    )
                else:
                    checks.append(
                        _check_result(
                            "mcp_config",
                            "ok",
                            f"transport={info['transport']} ({info.get('url', 'stdio')})",
                        )
                    )
        except Exception as exc:
            checks.append(
                _check_result(
                    "mcp_config",
                    "fail",
                    f"Cannot parse {eff_mcp}: {exc}",
                    "Fix JSON syntax or restore mcp_config.json.bak.",
                )
            )
    else:
        checks.append(
            _check_result(
                "mcp_config",
                "warn",
                f"{eff_mcp} not found (not yet configured on this user profile)",
                "Run `agsync setup-mcp` when ready to register Antigravity MCP.",
            )
        )

    # 6. PID lockfile & port check
    lock_info = inspect_pid_lockfile(eff_db)
    eff_url = server_url or f"http://{cfg.get('host_ip') or '127.0.0.1'}:{cfg.get('port') or 8765}"
    parsed = urlparse(eff_url)
    target_host = parsed.hostname or "127.0.0.1"
    target_port = parsed.port or 8765

    if lock_info["exists"] and not lock_info["alive"]:
        checks.append(
            _check_result(
                "pid_lock",
                "warn",
                f"Stale PID lockfile at {lock_info['path']} ({lock_info['data']})",
                "Remove stale .pid file or restart agsync server.",
            )
        )
    elif lock_info["exists"] and lock_info["alive"]:
        checks.append(
            _check_result(
                "pid_lock",
                "ok",
                f"Active hub lockfile held by PID {lock_info['data'].get('pid')}",
            )
        )
    else:
        checks.append(_check_result("pid_lock", "ok", "No conflicting hub PID lockfile"))

    tcp_ok, tcp_msg = tcp_check(target_host, target_port, timeout=1.5)
    checks.append(
        _check_result(
            "hub_tcp",
            "ok" if tcp_ok else "warn",
            tcp_msg if tcp_ok else f"Hub not listening on {target_host}:{target_port} ({tcp_msg})",
            "" if tcp_ok else "Start the hub on Host (`agsync serve`) if you expect it to be running.",
        )
    )

    # 7. System proxy interference
    proxy_info = inspect_system_proxy()
    env_p = proxy_info["env"]
    win_p = proxy_info["windows"]
    proxy_flags: list[str] = []
    if any(k.lower() != "no_proxy" for k in env_p):
        proxy_flags.append(f"env={list(env_p.keys())}")
    if win_p.get("ProxyEnable"):
        proxy_flags.append(f"win_proxy={win_p.get('ProxyServer')}")
    if proxy_flags and is_local_or_private_host(target_host):
        checks.append(
            _check_result(
                "system_proxy",
                "warn",
                "System proxy active (" + ", ".join(proxy_flags) + "); agsync bypasses it for LAN/loopback",
                "Ensure Antigravity/browser excludes 127.0.0.1 and RFC1918 LAN CIDRs if IDE HTTP MCP fails.",
            )
        )
    else:
        checks.append(_check_result("system_proxy", "ok", "No interfering proxy detected"))

    # 8. Windows network profile
    if os.name == "nt":
        profiles = inspect_windows_network_profiles()
        public_profiles = [
            p.get("Name") or p.get("InterfaceAlias") or "unknown"
            for p in profiles
            if str(p.get("NetworkCategory", "")).lower() == "public"
        ]
        if public_profiles:
            checks.append(
                _check_result(
                    "windows_network_profile",
                    "warn",
                    f"Active Public network profile(s): {', '.join(public_profiles)}",
                    "Set network profile to Private in Windows Settings so LAN firewall rules apply.",
                )
            )
        elif profiles:
            checks.append(
                _check_result(
                    "windows_network_profile",
                    "ok",
                    f"Windows network profile(s) OK ({len(profiles)} interface(s))",
                )
            )
        else:
            checks.append(
                _check_result(
                    "windows_network_profile",
                    "warn",
                    "Unable to query Get-NetConnectionProfile",
                )
            )
    else:
        checks.append(
            _check_result("windows_network_profile", "ok", "Skipped (non-Windows OS)")
        )

    # 9. Hook canary liveness
    try:
        canary = evaluate_hook_payload(
            {
                "toolCall": {"name": "read_file", "args": {"Path": "README.md"}},
                "cwd": str(repo_root),
            },
            server_url=eff_url,
            timeout=1.0,
        )
        if canary.get("decision") == "allow":
            checks.append(_check_result("hook_canary", "ok", "PreToolUse hook evaluator responded allow"))
        else:
            checks.append(
                _check_result(
                    "hook_canary",
                    "fail",
                    f"Unexpected canary response: {canary}",
                )
            )
    except Exception as exc:
        checks.append(_check_result("hook_canary", "fail", f"Hook canary crashed: {exc}"))

    has_fail = any(c["status"] == "fail" for c in checks)
    return {
        "ok": not has_fail,
        "repo": str(repo_root),
        "db_path": str(eff_db),
        "checks": checks,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    repo_root = find_repo_root(Path(os.path.abspath(os.path.normpath(str(args.repo)))))
    explicit_server = getattr(args, "server", None)
    if explicit_server:
        server = explicit_server
    else:
        server, _, _ = resolve_conn(args, repo_root=repo_root)
    db_path = Path(args.db).expanduser() if getattr(args, "db", None) else default_db_path()
    mcp_path = Path(args.mcp_config).expanduser() if getattr(args, "mcp_config", None) else None

    report = run_doctor(
        repo=repo_root,
        server_url=server,
        db_path=db_path,
        mcp_config_path=mcp_path,
    )
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"=== agsync doctor ({'OK' if report['ok'] else 'ISSUES FOUND'}) ===")
        for chk in report["checks"]:
            tag = chk["status"].upper()
            print(f"[{tag}] {chk['name']}: {chk['detail']}")
            if chk.get("fix"):
                print(f"       fix: {chk['fix']}")
    return 0 if report["ok"] else 1
