"""Git repository, worktree common-dir, hooks path, and version helpers."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=False,
    )


def find_repo_root(start: Path) -> Path:
    """Walk upward logically (preserving junctions) to find .git or .agsync root."""
    cur = Path(os.path.abspath(os.path.normpath(str(start))))
    home = Path.home().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
        if candidate.resolve() != home and (candidate / ".agsync").exists():
            return candidate
    return cur


def git_toplevel(repo: Path) -> Path | None:
    proc = _run_git(["rev-parse", "--show-toplevel"], repo)
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    return Path(os.path.abspath(os.path.normpath(raw)))


def git_common_dir(repo: Path) -> Path:
    """Resolve git common-dir (shared across worktrees) using git rev-parse --git-common-dir."""
    proc = _run_git(["rev-parse", "--git-common-dir"], repo)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"'{repo}' is not a Git repository: {err or 'git rev-parse failed'}")
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        raise RuntimeError(f"Unable to resolve --git-common-dir for '{repo}'")
    p = Path(raw)
    if not p.is_absolute():
        p = repo / p
    return Path(os.path.abspath(os.path.normpath(str(p))))


def git_hooks_dir(repo: Path) -> Path:
    """Resolve active git hooks directory (supports worktrees and core.hooksPath)."""
    proc = _run_git(["rev-parse", "--git-path", "hooks"], repo)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Unable to resolve git hooks path for '{repo}': {err}")
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return git_common_dir(repo) / "hooks"
    p = Path(raw)
    if not p.is_absolute():
        p = repo / p
    return Path(os.path.abspath(os.path.normpath(str(p))))


def git_exclude_file(repo: Path) -> Path:
    """Return <git-common-dir>/info/exclude path (correct for both main repos and worktrees)."""
    return git_common_dir(repo) / "info" / "exclude"


def git_staged_files(repo: Path) -> list[str]:
    proc = _run_git(
        [
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACMRDT",
            "-z",
        ],
        repo,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git diff --cached failed: {err}")
    raw = proc.stdout.decode("utf-8", errors="replace")
    return [item for item in raw.split("\0") if item.strip()]


def is_path_tracked(repo: Path, rel_path: str) -> bool:
    proc = _run_git(["ls-files", "--error-unmatch", rel_path], repo)
    return proc.returncode == 0


def parse_git_version(version_output: str) -> tuple[int, int, int] | None:
    m = _GIT_VERSION_RE.search(version_output)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def detect_git_version() -> tuple[tuple[int, int, int] | None, str]:
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    except OSError as exc:
        return None, str(exc)
    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return None, out or f"exit {proc.returncode}"
    return parse_git_version(out), out
