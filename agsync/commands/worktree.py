"""CLI handlers for `agsync worktree create|list|remove`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agsync.adapters.ide import atomic_write_text
from agsync.core.git_ops import find_repo_root, git_toplevel
from agsync.protocol import validate_agent_id


def parse_worktree_porcelain(output: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` output into structured dicts."""
    entries: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in output.splitlines():
        s = line.strip()
        if not s:
            if cur:
                entries.append(cur)
                cur = {}
            continue
        if s.startswith("worktree "):
            cur["path"] = s[len("worktree ") :].strip()
        elif s.startswith("HEAD "):
            cur["head"] = s[len("HEAD ") :].strip()
        elif s.startswith("branch "):
            cur["branch"] = s[len("branch ") :].strip()
        elif s == "bare":
            cur["bare"] = "true"
        elif s == "detached":
            cur["detached"] = "true"
    if cur:
        entries.append(cur)
    return entries


def list_worktrees(repo: Path) -> list[dict[str, str]]:
    proc = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git worktree list failed")
    return parse_worktree_porcelain(proc.stdout)


def _create_dir_link(link_path: Path, target_path: Path) -> bool:
    """Create a Windows Directory Junction (no Admin) or POSIX symlink if target exists."""
    if not target_path.is_dir() or link_path.exists():
        return False
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0
    os.symlink(target_path, link_path)
    return True


def create_worktree(
    repo: Path,
    agent_id: str,
    *,
    branch: str | None = None,
    link_deps: bool = True,
) -> dict[str, Any]:
    aid = validate_agent_id(agent_id)
    top = git_toplevel(repo) or find_repo_root(repo)
    existing = list_worktrees(top)
    existing_paths = {os.path.normcase(os.path.abspath(e["path"])) for e in existing if "path" in e}

    idx = 1
    while True:
        cand = top.parent / f"{top.name}-wt-{idx}"
        if not cand.exists() and os.path.normcase(os.path.abspath(str(cand))) not in existing_paths:
            break
        idx += 1

    eff_branch = branch or f"agent/{aid}"
    proc = subprocess.run(
        ["git", "worktree", "add", "-b", eff_branch, str(cand)],
        cwd=str(top),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Branch may already exist; try attaching without -b
        proc2 = subprocess.run(
            ["git", "worktree", "add", str(cand), eff_branch],
            cwd=str(top),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc2.returncode != 0:
            raise RuntimeError(proc2.stderr.strip() or proc.stderr.strip() or "git worktree add failed")

    # Explicit per-worktree identity (never fall back to shared hostname)
    atomic_write_text(cand / ".agsync-agent", aid + "\n", create_backup=False)

    linked: list[str] = []
    if link_deps:
        for dep in ("node_modules", ".venv"):
            if _create_dir_link(cand / dep, top / dep):
                linked.append(dep)

    return {
        "worktree": str(cand),
        "branch": eff_branch,
        "agent_id": aid,
        "linked_deps": linked,
    }


def remove_worktree(repo: Path, target: str, *, force: bool = False) -> dict[str, Any]:
    top = git_toplevel(repo) or find_repo_root(repo)
    wt_path = Path(os.path.abspath(os.path.normpath(target)))
    # Unlink junctions/symlinks first so git worktree remove doesn't traverse them
    for dep in ("node_modules", ".venv"):
        p = wt_path / dep
        if p.is_symlink() or (os.name == "nt" and p.exists() and os.path.isjunction(str(p))):
            try:
                p.rmdir()
            except OSError:
                pass

    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(wt_path))
    proc = subprocess.run(cmd, cwd=str(top), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git worktree remove failed")
    return {"removed": str(wt_path)}


def cmd_worktree(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    repo = find_repo_root(Path(os.path.abspath(os.path.normpath(str(args.repo)))))
    try:
        if args.wt_cmd == "list":
            res: Any = {"worktrees": list_worktrees(repo)}
        elif args.wt_cmd == "create":
            res = create_worktree(
                repo,
                args.agent,
                branch=getattr(args, "branch", None),
                link_deps=not bool(getattr(args, "no_link_deps", False)),
            )
        elif args.wt_cmd == "remove":
            res = remove_worktree(repo, args.path, force=bool(getattr(args, "force", False)))
        else:
            return 2
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
