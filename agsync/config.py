"""Centralized configuration, Antigravity MCP registration, and repo setup helpers."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from agsync.adapters.ide import atomic_write_text, merge_json_file, read_text_auto
from agsync.db import default_data_dir
from agsync.protocol import DEFAULT_PORT, validate_agent_id

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
DEFAULT_CONFIG_PATH = default_data_dir() / "config.json"
LEGACY_CONFIG_PATH = PACKAGE_ROOT / "agsync.config.json"


def detect_lan_ip() -> str:
    """Best-effort detection of primary LAN IPv4 address (skipping VPN/loopback)."""
    if os.name == "nt":
        try:
            ps_cmd = (
                "Get-NetIPAddress -AddressFamily IPv4 | "
                "Where-Object { $_.InterfaceAlias -notmatch "
                "'Loopback|happ|vEthernet|WSL|Windscribe|Tailscale|Tun|TAP' "
                "-and $_.IPAddress -notlike '169.254.*' "
                "-and $_.IPAddress -notlike '172.19.*' } | "
                "Select-Object -First 1 -ExpandProperty IPAddress"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=4,
            )
            ip = res.stdout.strip()
            if ip:
                return ip
        except Exception:
            pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def default_agent_id() -> str:
    """Derive a valid default agent_id from the current hostname."""
    raw = socket.gethostname().lower()
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw).strip("-_")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"agent-{cleaned}" if cleaned else "agent-1"
    return cleaned[:48]


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load agsync config with sensible defaults (BOM/UTF-16 safe)."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.is_file() and path is None and LEGACY_CONFIG_PATH.is_file():
        cfg_path = LEGACY_CONFIG_PATH

    defaults: dict[str, Any] = {
        "mode": "host",
        "agent_id": default_agent_id(),
        "host_ip": detect_lan_ip(),
        "port": DEFAULT_PORT,
        "token": "",
        "repo_path": "",
        "mcp_mode": "http",
        "discover": True,
    }
    if cfg_path.is_file():
        try:
            loaded = json.loads(read_text_auto(cfg_path))
            if isinstance(loaded, dict):
                defaults.update({k: v for k, v in loaded.items() if v is not None})
        except Exception:
            pass
    return defaults


def save_config(data: dict[str, Any], path: Path | None = None) -> Path:
    """Persist agsync config atomically to ~/.agsync/config.json (or explicit path)."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    atomic_write_text(
        cfg_path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        create_backup=False,
    )
    return cfg_path


def default_mcp_config_path(ide: str = "antigravity") -> Path:
    """Return global/workspace MCP config path for the target IDE."""
    name = ide.strip().lower()
    if name == "claude":
        return Path.cwd() / ".mcp.json"
    if name == "cursor":
        return Path.home() / ".cursor" / "mcp.json"
    return Path.home() / ".gemini" / "config" / "mcp_config.json"


def configure_antigravity_mcp(
    *,
    server_url: str,
    agent_id: str,
    mode: str = "http",
    token: str = "",
    mcp_config_path: Path | None = None,
    ide: str = "antigravity",
) -> dict[str, Any]:
    """Atomically merge/update the 'agsync' entry in the target IDE's MCP config."""
    aid = validate_agent_id(agent_id)
    base_url = server_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid server URL: {server_url}")
    if mode not in ("http", "stdio"):
        raise ValueError("MCP mode must be 'http' or 'stdio'")

    target = mcp_config_path or default_mcp_config_path(ide)

    if mode == "http":
        headers: dict[str, str] = {"X-Agent-Id": aid}
        if token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"
        entry: dict[str, Any] = {
            "url": f"{base_url}/mcp?agent={aid}",
            "headers": headers,
        }
    else:
        entry = {
            "command": sys.executable,
            "args": [
                "-m",
                "agsync.mcp_bridge",
                "--server",
                base_url,
                "--agent",
                aid,
            ],
            "env": {
                "PYTHONPATH": PACKAGE_ROOT.as_posix(),
                "AGSYNC_TOKEN": token.strip(),
            },
        }

    def _merge(existing: dict[str, Any]) -> dict[str, Any]:
        servers = existing.get("mcpServers")
        if servers is None:
            servers = {}
        if not isinstance(servers, dict):
            raise ValueError("'mcpServers' in mcp_config.json must be a JSON object")
        merged_servers = dict(servers)
        # Replace 'agsync' entry cleanly (also eliminates stale 'serverUrl' vs 'url' conflicts)
        merged_servers["agsync"] = entry
        out = dict(existing)
        out["mcpServers"] = merged_servers
        return out

    _, backup_path = merge_json_file(target, merge_fn=_merge, create_backup=True)

    return {
        "mcp_config_path": str(target),
        "backup_path": backup_path,
        "mode": mode,
        "agent_id": aid,
        "entry": entry,
    }


def copy_project_templates(repo: Path, *, overwrite_agents_md: bool = False) -> list[str]:
    """Copy AGENTS.md and .agents/skills/sync-* templates into a target repo without clobbering."""
    target_repo = Path(os.path.abspath(os.path.normpath(str(repo))))
    if not target_repo.is_dir():
        raise FileNotFoundError(f"Repository directory does not exist: {target_repo}")

    copied: list[str] = []
    agents_src = TEMPLATES_DIR / "AGENTS.md"
    agents_dst = target_repo / "AGENTS.md"
    if agents_src.is_file():
        if overwrite_agents_md or not agents_dst.exists():
            shutil.copy2(agents_src, agents_dst)
            copied.append(str(agents_dst))
        elif agents_dst.exists():
            existing_content = agents_dst.read_text(encoding="utf-8", errors="replace")
            if "Multi-Agent & Multi-IDE Coordination Rules" not in existing_content and "agsync" not in existing_content:
                template_content = agents_src.read_text(encoding="utf-8")
                updated_content = existing_content.rstrip() + "\n\n" + template_content.strip() + "\n"
                agents_dst.write_text(updated_content, encoding="utf-8")
                copied.append(str(agents_dst))

    skills_src = TEMPLATES_DIR / ".agents" / "skills"
    if skills_src.is_dir():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            dst_dir = target_repo / ".agents" / "skills" / skill_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for item in skill_dir.iterdir():
                if item.is_file():
                    dst_file = dst_dir / item.name
                    if not dst_file.exists():
                        shutil.copy2(item, dst_file)
                        copied.append(str(dst_file))
    return copied
