"""CLI handlers for `agsync guard` and `agsync install-hooks`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import stat
import sys
from pathlib import Path

from agsync.adapters.ide import atomic_write_text, merge_json_file, read_text_auto
from agsync.client import SyncClient, SyncClientError
from agsync.config import copy_project_templates
from agsync.core.git_ops import (
    find_repo_root,
    git_common_dir,
    git_exclude_file,
    git_hooks_dir,
    git_staged_files,
    is_path_tracked,
)
from agsync.protocol import DEFAULT_PORT

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_GITIGNORE_TEMPLATE = """# ==========================================
# agsync Multi-Agent Safe Coordination Rules
# ==========================================

# 1. agsync Machine Identity & Tokens (Never share machine identities)
.agsync/
.agsync-agent

# 2. Local Databases & Binary Storage (Prevents multi-agent binary merge conflicts)
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.db
*.db-journal

# 3. Deployment & Execution Caches
.deploy_cache.json
*.log
*.bak
*.tmp
*.temp

# 4. Large Archives & Backups
*.zip
*.tar
*.gz
*.rar

# 5. IDE, Editors & OS Volatile Files
.idea/
.vscode/
Thumbs.db
Desktop.ini
.DS_Store

# 6. Common Language Caches & Environments
__pycache__/
*.pyc
.venv/
node_modules/
"""


def ensure_repo_gitignore(repo: Path) -> tuple[bool, str]:
    """Ensure a multi-agent safe .gitignore exists in repo, appending essential rules if missing."""
    gitignore_path = repo / ".gitignore"
    if not gitignore_path.exists():
        atomic_write_text(gitignore_path, DEFAULT_GITIGNORE_TEMPLATE.strip() + "\n", create_backup=False)
        return True, "created"

    existing = read_text_auto(gitignore_path)
    lines = {
        line.strip()
        for line in existing.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    essential_rules = [
        ".agsync/",
        ".agsync-agent",
        "*.sqlite",
        "*.sqlite-wal",
        "*.sqlite-shm",
        ".deploy_cache.json",
    ]
    missing = [r for r in essential_rules if r not in lines]
    if missing:
        append_block = (
            "\n# --- agsync Multi-Agent Safe Coordination Rules ---\n"
            + "\n".join(missing)
            + "\n"
        )
        new_content = existing
        if not new_content.endswith("\n"):
            new_content += "\n"
        new_content += append_block
        atomic_write_text(gitignore_path, new_content, create_backup=False)
        return True, f"appended {len(missing)} missing rules"
    return False, "already configured"


def read_local_setting(repo_root: Path, filenames: tuple[str, ...]) -> str | None:
    for rel in filenames:
        p = repo_root / rel
        try:
            if p.is_file():
                lines = read_text_auto(p).strip().splitlines()
                if lines and lines[0].strip():
                    return lines[0].strip()
        except OSError:
            continue
    return None


def resolve_conn(
    args: argparse.Namespace, repo_root: Path | None = None
) -> tuple[str, str, str]:
    root = repo_root or find_repo_root(Path.cwd())
    server = (
        getattr(args, "server", None)
        or os.environ.get("AGSYNC_SERVER")
        or read_local_setting(root, (".agsync/server_url",))
        or f"http://127.0.0.1:{DEFAULT_PORT}"
    )
    agent = (
        getattr(args, "agent", None)
        or os.environ.get("AGSYNC_AGENT")
        or read_local_setting(root, (".agsync-agent", ".agsync/agent_id"))
        or socket.gethostname()
    )
    token = (
        getattr(args, "token", None)
        or os.environ.get("AGSYNC_TOKEN")
        or read_local_setting(root, (".agsync/token",))
        or ""
    )
    return server, agent, token


def cmd_guard(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    repo_root = find_repo_root(Path(os.path.abspath(os.path.normpath(str(args.repo)))))
    server, agent, token = resolve_conn(args, repo_root=repo_root)
    allow_offline = bool(args.fail_open or args.allow_offline)

    paths: list[str] = list(args.paths or [])
    if args.staged:
        try:
            staged = git_staged_files(repo_root)
        except RuntimeError as exc:
            sys.stderr.write(f"[agsync guard] {exc}\n")
            return 2
        paths.extend(staged)

    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    if not unique_paths:
        if as_json:
            print(json.dumps({"all_clear": True, "checked": []}, indent=2))
        return 0

    client = SyncClient(server, token=token or None, timeout=3.0)
    try:
        res = client.check_paths(unique_paths, agent_id=agent, scope=args.scope)
    except SyncClientError as exc:
        if exc.unreachable and allow_offline:
            sys.stderr.write(
                f"[agsync guard] WARNING: {exc.message} (--allow-offline/--fail-open active, allowing)\n"
            )
            return 0
        sys.stderr.write(f"[agsync guard] ERROR: {exc.message}\n")
        return 2

    blocked = res.get("blocked") or []
    free_paths = res.get("free") or []

    if blocked:
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(f"[agsync guard] BLOCKED for agent '{agent}':\n")
            for item in blocked:
                for c in item["conflicts"]:
                    sys.stderr.write(
                        f"  - {item['path']} conflicts with {c['scope']}='{c['resource_norm']}' "
                        f"held by '{c['owner_id']}' (reason: {c.get('reason') or '-'})\n"
                    )
        return 1

    if args.require_lease and free_paths:
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(
                f"[agsync guard] BLOCKED (--require-lease): agent '{agent}' holds no lease for: "
                f"{', '.join(free_paths)}\n"
            )
        return 1

    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"[agsync guard] OK ({len(unique_paths)} path(s) clear for '{agent}').")
    return 0


def _build_pre_commit_script(*, allow_offline: bool, require_lease: bool) -> str:
    extra_flags = ["--staged", "--repo", '"$TOPLEVEL"']
    if allow_offline:
        extra_flags.append("--allow-offline")
    if require_lease:
        extra_flags.append("--require-lease")
    flags_str = " ".join(extra_flags)
    pkg_root = PACKAGE_ROOT.as_posix()

    return (
        "#!/bin/sh\n"
        "# Installed by agsync install-hooks\n"
        'TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"\n'
        f'PKG_ROOT="{pkg_root}"\n'
        'if [ -d "$PKG_ROOT/agsync" ]; then\n'
        '  export PYTHONPATH="${PKG_ROOT}${PYTHONPATH:+:$PYTHONPATH}"\n'
        "fi\n"
        'PY=""\n'
        'for cand in "$AGSYNC_PYTHON" python python3; do\n'
        '  [ -n "$cand" ] || continue\n'
        '  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys" >/dev/null 2>&1; then\n'
        '    PY="$cand"\n'
        "    break\n"
        "  fi\n"
        "done\n"
        'if [ -z "$PY" ] && command -v py >/dev/null 2>&1 && py -3 -c "import sys" >/dev/null 2>&1; then\n'
        '  PY="py -3"\n'
        "fi\n"
        'if [ -z "$PY" ]; then\n'
        '  echo "[agsync guard] Python interpreter not found in PATH" >&2\n'
        "  exit 2\n"
        "fi\n"
        f"exec $PY -m agsync.cli guard {flags_str}\n"
    )


def _resolve_hook_python_command() -> str:
    """Resolve a stable python command for .agents/hooks.json without transient venv paths."""
    if os.name == "nt":
        return "python" if shutil.which("python") else Path(sys.executable).as_posix()
    if shutil.which("python3"):
        return "python3"
    if shutil.which("python"):
        return "python"
    return Path(sys.executable).as_posix()


def cmd_install_hooks(args: argparse.Namespace) -> int:
    repo = Path(os.path.abspath(os.path.normpath(str(args.repo))))
    if getattr(args, "init_if_needed", False) and not (repo / ".git").exists():
        import subprocess

        try:
            init_res = subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if init_res.returncode == 0:
                sys.stdout.write(f"Initialized Git repository in '{repo}' (branch: main).\n")
        except Exception as exc:
            sys.stderr.write(f"Warning: Failed to auto-initialize git repository: {exc}\n")
    try:
        common_dir = git_common_dir(repo)
        hooks_dir = git_hooks_dir(repo)
        exclude_file = git_exclude_file(repo)
    except RuntimeError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    hooks_dir.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks_dir / "pre-commit"
    if pre_commit.exists() and not args.force:
        existing = read_text_auto(pre_commit)
        if "agsync" not in existing:
            sys.stderr.write(
                f"Error: '{pre_commit}' already exists. Re-run with --force to overwrite.\n"
            )
            return 2

    agsync_dir = repo / ".agsync"
    agsync_dir.mkdir(parents=True, exist_ok=True)
    if args.server:
        atomic_write_text(
            agsync_dir / "server_url",
            args.server.strip() + "\n",
            create_backup=False,
        )
    if args.agent:
        atomic_write_text(
            agsync_dir / "agent_id",
            args.agent.strip() + "\n",
            create_backup=False,
        )
    if args.token:
        atomic_write_text(
            agsync_dir / "token",
            args.token.strip() + "\n",
            create_backup=False,
        )

    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing_excl = read_text_auto(exclude_file) if exclude_file.exists() else ""
    existing_lines = {ln.strip() for ln in existing_excl.splitlines() if ln.strip()}
    exclude_candidates = [".agsync/", ".agsync-agent"]
    if args.with_antigravity_hook and not is_path_tracked(repo, ".agents/hooks.json"):
        exclude_candidates.append(".agents/hooks.json")
    to_add = [p for p in exclude_candidates if p not in existing_lines]
    if to_add:
        new_excl = existing_excl
        if new_excl and not new_excl.endswith("\n"):
            new_excl += "\n"
        new_excl += "".join(p + "\n" for p in to_add)
        atomic_write_text(exclude_file, new_excl, create_backup=False)

    script = _build_pre_commit_script(
        allow_offline=bool(args.allow_offline),
        require_lease=bool(args.require_lease),
    )
    atomic_write_text(pre_commit, script, create_backup=False)
    try:
        pre_commit.chmod(pre_commit.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass

    installed: list[str] = [str(pre_commit)]

    if args.with_antigravity_hook:
        hook_guard_py = (PACKAGE_ROOT / "agsync" / "hook_guard.py").as_posix()
        py_cmd = _resolve_hook_python_command()
        cmd_parts = [f'"{py_cmd}"' if " " in py_cmd else py_cmd, f'"{hook_guard_py}"']
        if args.fail_closed:
            cmd_parts.append("--fail-closed")
        if args.require_lease:
            cmd_parts.append("--require-lease")
        hook_command = " ".join(cmd_parts)

        stop_command = f'"{py_cmd}" "{hook_guard_py}" --release-all' if " " in py_cmd else f'{py_cmd} "{hook_guard_py}" --release-all'
        ag_hooks_path = repo / ".agents" / "hooks.json"
        lease_guard_entry = {
            "PreToolUse": [
                {
                    "matcher": "edit_file|write_file|write_to_file|replace_file_content|multi_replace_file_content",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                            "timeout": 5,
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "type": "command",
                    "command": stop_command,
                    "timeout": 5,
                }
            ],
        }

        def _merge_hooks(existing: dict[str, object]) -> dict[str, object]:
            merged = dict(existing)
            merged["agsync-lease-guard"] = lease_guard_entry
            return merged

        merge_json_file(ag_hooks_path, merge_fn=_merge_hooks, create_backup=True)
        installed.append(str(ag_hooks_path))

    ide = getattr(args, "ide", "antigravity")
    if ide == "claude":
        py_cmd = _resolve_hook_python_command()
        proxy_cmd = f'{py_cmd} -m agsync.cli hook-proxy --host claude'
        claude_cfg_path = repo / ".claude" / "settings.json"
        def _merge_claude(existing: dict[str, object]) -> dict[str, object]:
            merged = dict(existing)
            hooks = dict(merged.get("hooks") or {}) if isinstance(merged.get("hooks"), dict) else {}
            pre = list(hooks.get("PreToolUse") or []) if isinstance(hooks.get("PreToolUse"), list) else []
            pre = [item for item in pre if not (isinstance(item, dict) and "agsync" in json.dumps(item))]
            pre.append({"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": proxy_cmd}]})
            hooks["PreToolUse"] = pre
            merged["hooks"] = hooks
            return merged
        merge_json_file(claude_cfg_path, merge_fn=_merge_claude, create_backup=True)
        installed.append(str(claude_cfg_path))
    elif ide == "cursor":
        py_cmd = _resolve_hook_python_command()
        proxy_cmd = f'{py_cmd} -m agsync.cli hook-proxy --host cursor'
        cursor_cfg_path = repo / ".cursor" / "hooks.json"
        def _merge_cursor(existing: dict[str, object]) -> dict[str, object]:
            merged = dict(existing)
            merged.setdefault("version", 1)
            hooks = dict(merged.get("hooks") or {}) if isinstance(merged.get("hooks"), dict) else {}
            pre = list(hooks.get("preToolUse") or []) if isinstance(hooks.get("preToolUse"), list) else []
            pre = [item for item in pre if not (isinstance(item, dict) and "agsync" in json.dumps(item))]
            pre.append({"command": proxy_cmd})
            hooks["preToolUse"] = pre
            merged["hooks"] = hooks
            return merged
        merge_json_file(cursor_cfg_path, merge_fn=_merge_cursor, create_backup=True)
        installed.append(str(cursor_cfg_path))

    if getattr(args, "with_templates", False):
        copied = copy_project_templates(repo)
        installed.extend(copied)

    changed_gi, gi_note = ensure_repo_gitignore(repo)
    if (repo / ".gitignore").exists():
        installed.append(str(repo / ".gitignore"))

    result = {
        "installed": installed,
        "repo": str(repo),
        "git_common_dir": str(common_dir),
        "hooks_dir": str(hooks_dir),
        "server": args.server or os.environ.get("AGSYNC_SERVER", f"http://127.0.0.1:{DEFAULT_PORT}"),
        "agent": args.agent or os.environ.get("AGSYNC_AGENT", socket.gethostname()),
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for path_str in installed:
            print(f"Installed hook: {path_str}")
    return 0
