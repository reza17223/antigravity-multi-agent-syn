"""Non-destructive Git merge conflict pre-check via `git merge-tree --write-tree`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agsync.core.ext_hooks import run_external_hooks
from agsync.core.git_ops import detect_git_version, find_repo_root


def check_merge_tree(
    repo: Path,
    *,
    base: str = "main",
    target: str = "HEAD",
) -> dict[str, Any]:
    """Run `git merge-tree --write-tree <base> <target>` without touching the working tree."""
    gver, graw = detect_git_version()
    if gver is None or gver < (2, 38, 0):
        raise RuntimeError(
            f"git merge-tree --write-tree requires Git >= 2.38 (found: {graw})"
        )

    proc = subprocess.run(
        ["git", "merge-tree", "--write-tree", base, target],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    tree_oid = lines[0] if lines else ""
    conflicts: list[str] = []
    if proc.returncode != 0:
        for ln in lines[1:]:
            if ln.startswith("CONFLICT ") or "CONFLICT" in ln:
                conflicts.append(ln)
        if not conflicts and proc.stderr.strip():
            conflicts.append(proc.stderr.strip())

    return {
        "clean": proc.returncode == 0,
        "base": base,
        "target": target,
        "merge_tree_oid": tree_oid,
        "conflicts": conflicts,
    }


def cmd_merge_check(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    repo = find_repo_root(Path(os.path.abspath(os.path.normpath(str(args.repo)))))
    try:
        res = check_merge_tree(repo, base=args.base, target=args.target)
        hook_results = run_external_hooks(
            "pre_merge",
            env_extra={"AGSYNC_BASE": args.base, "AGSYNC_TARGET": args.target},
            cwd=repo,
        )
        if hook_results:
            res["external_hooks"] = hook_results
            if any(not h["ok"] for h in hook_results):
                res["clean"] = False

        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            if res["clean"]:
                print(f"MERGE CHECK OK: '{args.target}' merges cleanly into '{args.base}' (tree={res['merge_tree_oid'][:10]})")
            else:
                print(f"MERGE CONFLICTS between '{args.base}' and '{args.target}':", file=sys.stderr)
                for c in res["conflicts"]:
                    print(f"  - {c}", file=sys.stderr)
        return 0 if res["clean"] else 1
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
