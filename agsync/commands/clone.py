"""Clone/pull project repository from host hub with automated MCP and hook setup."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from agsync.client import SyncClient, SyncClientError
from agsync.commands.hooks import cmd_install_hooks
from agsync.commands.sync_cmds import cmd_setup_mcp


def clone_from_host(
    server_url: str,
    dest_dir: Path | str,
    *,
    token: str | None = None,
    agent_id: str = "peer",
    ide: str = "antigravity",
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    dest = Path(os.path.abspath(os.path.normpath(str(dest_dir))))
    dest.mkdir(parents=True, exist_ok=True)

    client = SyncClient(server_url, token=token, timeout=30.0)
    _log(f"Checking repository status on host ({server_url})...")
    status = client.get_repo_status()
    if not status.get("attached"):
        raise RuntimeError("Host has not attached a project repository yet. Ask host to attach repo in panel.")

    temp_dir = Path(tempfile.mkdtemp(prefix="agsync_dl_"))
    try:
        temp_file = temp_dir / "downloaded_archive"
        _log("Downloading repository archive / bundle from host...")
        dl_info = client.download_repo_bundle(temp_file)
        fmt = dl_info.get("format", "git-bundle")
        size_kb = round(dl_info.get("bytes", 0) / 1024, 1)
        _log(f"Downloaded {size_kb} KB ({fmt}). Setting up project in {dest}...")

        if fmt == "git-bundle":
            if (dest / ".git").exists():
                _log("Existing Git repository found in destination; fetching from host bundle...")
                subprocess.run(
                    ["git", "fetch", str(temp_file), "main:host-main"],
                    cwd=str(dest),
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "merge", "host-main", "--allow-unrelated-histories", "-m", "Merge changes from host"],
                    cwd=str(dest),
                    capture_output=True,
                    text=True,
                )
            else:
                is_empty = not any(dest.iterdir())
                if is_empty:
                    res_clone = subprocess.run(
                        ["git", "clone", str(temp_file), str(dest)],
                        capture_output=True,
                        text=True,
                    )
                    if res_clone.returncode != 0:
                        raise RuntimeError(f"Git clone failed: {res_clone.stderr or res_clone.stdout}")
                else:
                    stg_dir = temp_dir / "stg"
                    res_clone = subprocess.run(
                        ["git", "clone", str(temp_file), str(stg_dir)],
                        capture_output=True,
                        text=True,
                    )
                    if res_clone.returncode != 0:
                        raise RuntimeError(f"Git clone failed: {res_clone.stderr or res_clone.stdout}")
                    if (stg_dir / ".git").exists():
                        shutil.copytree(stg_dir / ".git", dest / ".git", dirs_exist_ok=True)
                        subprocess.run(["git", "checkout", "-f", "main"], cwd=str(dest), capture_output=True)
        else:
            with zipfile.ZipFile(temp_file, "r") as zf:
                zf.extractall(dest)
            if not (dest / ".git").exists():
                _log("Initializing Git repository in destination...")
                subprocess.run(["git", "init", "-b", "main"], cwd=str(dest), check=False)
                subprocess.run(["git", "add", "."], cwd=str(dest), check=False)
                subprocess.run(["git", "commit", "-m", "Initial clone from host"], cwd=str(dest), check=False)

        # Configure MCP
        _log(f"Configuring {ide} MCP connection...")
        mcp_args = argparse.Namespace(
            server=server_url,
            agent=agent_id,
            token=token,
            ide=ide,
            mode="http",
            mcp_config=None,
            force=True,
            repo=str(dest),
        )
        cmd_setup_mcp(mcp_args, as_json=False)

        # Install Hooks
        _log("Installing safety hooks and project templates...")
        hook_args = argparse.Namespace(
            repo=str(dest),
            server=server_url,
            agent=agent_id,
            token=token,
            with_antigravity_hook=True,
            with_templates=True,
            init_if_needed=True,
            allow_offline=True,
            require_lease=False,
            fail_closed=False,
            force=True,
            ide=ide,
            json=False,
        )
        cmd_install_hooks(hook_args)

        _log(f"Project clone & setup complete in {dest}!")
        return {"status": "ok", "dest": str(dest), "format": fmt}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def pull_latest_changes(
    repo_dir: Path | str,
    server_url: str,
    *,
    token: str | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    repo = Path(os.path.abspath(os.path.normpath(str(repo_dir))))
    if not (repo / ".git").exists():
        raise RuntimeError(f"'{repo}' is not a Git repository.")

    # 1. Check if git status has tracked uncommitted changes
    st = subprocess.run(["git", "status", "--porcelain", "-uno"], cwd=str(repo), capture_output=True, text=True)
    if st.returncode == 0 and st.stdout.strip():
        _log("Local working tree has uncommitted changes; sync postponed.")
        return {"status": "busy", "message": "Local repository has uncommitted changes. Save or commit before pulling."}

    # 2. Try git pull if a remote exists
    remotes = subprocess.run(["git", "remote"], cwd=str(repo), capture_output=True, text=True)
    if remotes.returncode == 0 and remotes.stdout.strip():
        res = subprocess.run(["git", "pull", "--rebase=false"], cwd=str(repo), capture_output=True, text=True)
        if res.returncode == 0:
            out = res.stdout.strip()
            _log(f"Git pull OK: {out}")
            return {"status": "ok", "output": out}

    # 3. Pull bundle from host hub
    try:
        client = SyncClient(server_url, token=token, timeout=30.0)
        temp_dir = Path(tempfile.mkdtemp(prefix="agsync_pull_"))
        try:
            temp_bundle = temp_dir / "latest.bundle"
            dl_info = client.download_repo_bundle(temp_bundle)
            if dl_info.get("format") == "git-bundle":
                subprocess.run(["git", "fetch", str(temp_bundle), "main:host-latest"], cwd=str(repo), capture_output=True)
                m_res = subprocess.run(
                    ["git", "merge", "host-latest", "-m", "Auto-sync from host"],
                    cwd=str(repo),
                    capture_output=True,
                    text=True,
                )
                msg = m_res.stdout.strip() or m_res.stderr.strip()
                _log(f"Synced from host bundle: {msg}")
                return {"status": "ok", "output": msg}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as exc:
        _log(f"Pull warning: {exc}")

    return {"status": "error", "message": "Could not pull changes from remote or host bundle."}


def cmd_clone(args: argparse.Namespace) -> int:
    try:
        res = clone_from_host(
            server_url=args.server,
            dest_dir=args.dest,
            token=getattr(args, "token", None),
            agent_id=getattr(args, "agent", "peer"),
            ide=getattr(args, "ide", "antigravity"),
            log_fn=lambda m: sys.stdout.write(f"{m}\n"),
        )
        return 0 if res.get("status") == "ok" else 1
    except Exception as exc:
        sys.stderr.write(f"Error during clone: {exc}\n")
        return 1


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        res = pull_latest_changes(
            repo_dir=args.repo,
            server_url=args.server,
            token=getattr(args, "token", None),
            log_fn=lambda m: sys.stdout.write(f"{m}\n"),
        )
        return 0 if res.get("status") == "ok" else 1
    except Exception as exc:
        sys.stderr.write(f"Error during sync: {exc}\n")
        return 1
