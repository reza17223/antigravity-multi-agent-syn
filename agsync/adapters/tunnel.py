"""Cloudflare Tunnel adapter (--protocol http2) and remote base_url helpers."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any


def build_cloudflared_command(
    local_url: str,
    *,
    protocol: str = "http2",
    binary: str = "cloudflared",
) -> list[str]:
    """Build the cloudflared quick-tunnel command with --protocol http2."""
    return [
        binary,
        "tunnel",
        "--url",
        local_url.rstrip("/"),
        "--protocol",
        protocol,
    ]


def start_cloudflared_tunnel(
    local_url: str,
    *,
    protocol: str = "http2",
) -> subprocess.Popen[str]:
    """Spawn a background cloudflared tunnel process."""
    exe = shutil.which("cloudflared")
    if not exe:
        raise FileNotFoundError(
            "cloudflared binary not found in PATH. Install Cloudflare Tunnel or pass --base-url."
        )
    cmd = build_cloudflared_command(local_url, protocol=protocol, binary=exe)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def format_join_link(base_url: str, nonce: str, signature: str, agent_id: str) -> str:
    """Format a shareable join URL for remote onboarding."""
    clean_base = base_url.strip().rstrip("/")
    return f"{clean_base}/join?nonce={nonce}&sig={signature}&agent={agent_id}"
