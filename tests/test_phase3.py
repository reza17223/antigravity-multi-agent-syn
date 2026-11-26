"""Phase 3 tests: git merge-tree merge-check, External Shell Hooks, explicit Offline mode, and packaging."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agsync.client import SyncClient, SyncClientError
from agsync.commands.merge_check import check_merge_tree
from agsync.core.ext_hooks import run_external_hooks
from scripts.build_exe import build_binaries


class TestPhase3PolishAndPackaging(unittest.TestCase):
    def test_merge_check_clean_and_conflict_without_touching_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "mrepo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "Agent A"], cwd=str(repo), check=True)
            (repo / "app.py").write_text("line1 = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=str(repo), check=True, capture_output=True)

            # Clean branch
            subprocess.run(["git", "checkout", "-b", "feat-clean"], cwd=str(repo), check=True, capture_output=True)
            (repo / "extra.py").write_text("ok = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "extra.py"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "clean"], cwd=str(repo), check=True, capture_output=True)

            clean_res = check_merge_tree(repo, base="main", target="feat-clean")
            self.assertTrue(clean_res["clean"])
            self.assertEqual(clean_res["conflicts"], [])

            # Conflicting branch
            subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)
            (repo / "app.py").write_text("line1 = 'main'\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "main edit"], cwd=str(repo), check=True, capture_output=True)

            subprocess.run(["git", "checkout", "-b", "feat-conflict", "HEAD~1"], cwd=str(repo), check=True, capture_output=True)
            (repo / "app.py").write_text("line1 = 'branch'\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "branch edit"], cwd=str(repo), check=True, capture_output=True)

            conflict_res = check_merge_tree(repo, base="main", target="feat-conflict")
            self.assertFalse(conflict_res["clean"])
            self.assertTrue(conflict_res["conflicts"])

    def test_external_shell_hooks_execution_and_failure(self) -> None:
        cfg = {
            "hooks": {
                "on_lock": [f'"{sys.executable}" -c "import os; print(os.environ[\'AGSYNC_EVENT\'])"'],
                "pre_merge": [f'"{sys.executable}" -c "import sys; sys.exit(2)"'],
            }
        }
        ok_res = run_external_hooks("on_lock", config=cfg)
        self.assertEqual(len(ok_res), 1)
        self.assertTrue(ok_res[0]["ok"])
        self.assertEqual(ok_res[0]["stdout"], "on_lock")

        fail_res = run_external_hooks("pre_merge", config=cfg)
        self.assertEqual(len(fail_res), 1)
        self.assertFalse(fail_res[0]["ok"])
        self.assertEqual(fail_res[0]["returncode"], 2)

    def test_explicit_offline_read_only_mode(self) -> None:
        c = SyncClient("http://127.0.0.1:1", timeout=0.5)
        self.assertFalse(c.ping_or_offline())
        self.assertTrue(c.offline_read_only)
        with self.assertRaises(SyncClientError) as ctx:
            c.acquire_lease("src/a.py", "agent-a")
        self.assertEqual(ctx.exception.error_code, "offline_read_only")

    def test_build_exe_dry_run_commands(self) -> None:
        cmds = build_binaries(dry_run=True)
        self.assertEqual(len(cmds), 2)
        self.assertIn("agsync", cmds[0])
        self.assertIn("agsync-panel", cmds[1])


if __name__ == "__main__":
    unittest.main()
