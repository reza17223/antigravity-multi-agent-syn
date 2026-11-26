"""IDE config helpers: BOM/UTF-16 decoding, atomic writes, and non-destructive JSON merging."""

from __future__ import annotations

import codecs
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


def detect_bom_encoding(raw: bytes) -> str | None:
    """Detect BOM encoding name if present."""
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    return None


def decode_bytes_auto(raw: bytes) -> str:
    """Decode bytes handling UTF-8, UTF-8 BOM, and PowerShell UTF-16LE/BE cleanly."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        if b"\x00" in raw:
            try:
                return raw.decode("utf-16")
            except UnicodeDecodeError:
                pass
        return raw.decode("utf-8", errors="replace")


def read_text_auto(path: Path) -> str:
    """Read a text file handling UTF-8, UTF-8 BOM, and UTF-16 BOM cleanly."""
    return decode_bytes_auto(path.read_bytes())


def atomic_write_text(
    target: Path,
    content: str,
    *,
    create_backup: bool = True,
    retries: int = 5,
) -> str | None:
    """Atomically write text via temp file + os.replace with retries and optional .bak."""
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path: str | None = None
    if create_backup and target.is_file():
        bak = target.with_name(target.name + ".bak")
        shutil.copy2(target, bak)
        backup_path = str(bak)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

        last_err: OSError | None = None
        for attempt in range(max(1, retries)):
            try:
                os.replace(tmp_path, target)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.05 * (attempt + 1))
        if last_err is not None:
            raise last_err
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return backup_path


def merge_json_file(
    target: Path,
    updater: dict[str, Any] | None = None,
    *,
    merge_fn: Any = None,
    create_backup: bool = True,
) -> tuple[dict[str, Any], str | None]:
    """Load an existing JSON object file (BOM/UTF-16 safe), merge changes, and write atomically."""
    existing: dict[str, Any] = {}
    if target.is_file():
        raw = read_text_auto(target).strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"{target} must contain a JSON object")
            existing = parsed

    if merge_fn is not None:
        merged = merge_fn(existing)
    else:
        merged = dict(existing)
        if updater:
            merged.update(updater)

    backup_path = atomic_write_text(
        target,
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        create_backup=create_backup,
    )
    return merged, backup_path


def inspect_mcp_entry(entry: Any) -> dict[str, Any]:
    """Inspect a single mcpServers entry for url vs serverUrl compatibility issues."""
    if not isinstance(entry, dict):
        return {"transport": "invalid", "issues": ["Entry is not a JSON object"]}
    if "command" in entry:
        return {"transport": "stdio", "issues": []}

    url = entry.get("url")
    server_url = entry.get("serverUrl")
    issues: list[str] = []
    effective_url = url or server_url

    if server_url and not url:
        issues.append("Uses legacy 'serverUrl' instead of canonical 'url' key")
    elif server_url and url and str(server_url).strip() != str(url).strip():
        issues.append(f"Conflicting 'url' ({url}) and 'serverUrl' ({server_url})")

    if not effective_url or not isinstance(effective_url, str):
        issues.append("Missing 'url' (or 'command') in MCP server config")
    elif "/sse" in effective_url:
        issues.append("Points to legacy '/sse' endpoint; Antigravity requires Streamable HTTP '/mcp'")

    return {
        "transport": "http",
        "url": effective_url,
        "has_url": url is not None,
        "has_server_url": server_url is not None,
        "issues": issues,
    }
