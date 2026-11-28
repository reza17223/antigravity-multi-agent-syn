"""CLI subprocess tests, Git pre-commit guard integration, template validation, and smoke_check.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agsync.client import SyncClient
from agsync.server import create_server

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "agsync.cli", *args],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class TestCLISmokeAndTemplates(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = create_server(
            host="127.0.0.1",
            port=0,
            db_path=Path(self.tmp.name) / "cli.db",
            case_insensitive=True,
        )
        self.port = self.srv.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.srv.store.close()
        self.thread.join(timeout=2.0)
        self.tmp.cleanup()

    def test_cli_full_workflow(self) -> None:
        r = _run_cli(
            "--server", self.base_url, "--agent", "machine-a", "register", "--branch", "agent-a/cli"
        )
        self.assertEqual(r.returncode, 0, r.stderr)

        r_send = _run_cli(
            "--server", self.base_url, "--agent", "machine-a", "send", "--to", "machine-b", "hello via cli"
        )
        self.assertEqual(r_send.returncode, 0, r_send.stderr)

        r_inbox = _run_cli(
            "--server", self.base_url, "--agent", "machine-b", "inbox", "--unread", "--ack"
        )
        self.assertEqual(r_inbox.returncode, 0, r_inbox.stderr)
        self.assertIn("hello via cli", r_inbox.stdout)

        r_lock = _run_cli(
            "--server", self.base_url, "--agent", "machine-a", "lock", "src/main.py", "--reason", "cli edit"
        )
        self.assertEqual(r_lock.returncode, 0, r_lock.stderr)

        # Peer check exits non-zero
        r_chk_b = _run_cli(
            "--server", self.base_url, "--agent", "machine-b", "check", "src/main.py"
        )
        self.assertEqual(r_chk_b.returncode, 1)

        r_ctx = _run_cli(
            "--server", self.base_url, "--agent", "machine-a", "context", "set", "env:port", "8080"
        )
        self.assertEqual(r_ctx.returncode, 0, r_ctx.stderr)
        r_ctx_get = _run_cli(
            "--server", self.base_url, "--agent", "machine-b", "context", "get", "env:port"
        )
        self.assertEqual(r_ctx_get.returncode, 0, r_ctx_get.stderr)
        self.assertEqual(json.loads(r_ctx_get.stdout)["value"], 8080)

        r_unlock = _run_cli(
            "--server", self.base_url, "--agent", "machine-a", "unlock", "--all"
        )
        self.assertEqual(r_unlock.returncode, 0, r_unlock.stderr)

        r_status = _run_cli("--server", self.base_url, "--agent", "machine-a", "status")
        self.assertEqual(r_status.returncode, 0, r_status.stderr)
        self.assertIn("=== agsync Status ===", r_status.stdout)

    def test_install_hooks_and_git_pre_commit_guard(self) -> None:
        repo = Path(self.tmp.name) / "git_proj"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "Agent A"], cwd=str(repo), check=True)

        # Install git pre-commit + Antigravity hooks.json
        inst = _run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            self.base_url,
            "--agent",
            "machine-a",
            "--with-antigravity-hook",
        )
        self.assertEqual(inst.returncode, 0, inst.stderr)
        self.assertTrue((repo / ".git" / "hooks" / "pre-commit").is_file())
        self.assertTrue((repo / ".agents" / "hooks.json").is_file())

        # Peer machine-b locks app.py
        client_b = SyncClient(self.base_url)
        client_b.acquire_lease("app.py", "machine-b", scope="file", reason="peer edit")

        (repo / "app.py").write_text("print('conflict')\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True)

        commit_blocked = subprocess.run(
            ["git", "commit", "-m", "try blocked commit"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(commit_blocked.returncode, 0)
        self.assertIn("machine-b", commit_blocked.stderr)

        # Release peer lock -> commit now succeeds
        client_b.release_lease("machine-b", release_all=True)
        commit_ok = subprocess.run(
            ["git", "commit", "-m", "commit after release"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(commit_ok.returncode, 0, commit_ok.stdout + commit_ok.stderr)

        # Verify .agents/hooks.json was added to .git/info/exclude (and not duplicated on re-run)
        _run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            self.base_url,
            "--agent",
            "machine-a",
            "--with-antigravity-hook",
        )
        excl_text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        excl_lines = [ln.strip() for ln in excl_text.splitlines() if ln.strip()]
        self.assertIn(".agents/hooks.json", excl_lines)
        self.assertEqual(excl_lines.count(".agents/hooks.json"), 1)

        # Verify git mv and git rm on peer-locked files are BOTH blocked by pre-commit guard
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "keep.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "src" / "del_me.py").write_text("y = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/keep.py", "src/del_me.py"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-m", "add files for mv/rm guard test"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

        client_b.acquire_lease("src/keep.py", "machine-b", scope="file", reason="editing keep")
        client_b.acquire_lease("src/del_me.py", "machine-b", scope="file", reason="editing del_me")

        subprocess.run(["git", "mv", "src/keep.py", "src/moved.py"], cwd=str(repo), check=True)
        mv_blocked = subprocess.run(
            ["git", "commit", "-m", "try mv locked source"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(mv_blocked.returncode, 0)
        self.assertIn("machine-b", mv_blocked.stderr)

        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "rm", "src/del_me.py"], cwd=str(repo), check=True, capture_output=True)
        rm_blocked = subprocess.run(
            ["git", "commit", "-m", "try rm locked file"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(rm_blocked.returncode, 0)
        self.assertIn("machine-b", rm_blocked.stderr)

    def test_installed_antigravity_hook_executes_via_shell_unspaced_and_spaced(self) -> None:
        client_b = SyncClient(self.base_url)
        client_b.acquire_lease("src/locked.py", "machine-b", scope="file", reason="shell hook test")

        for folder_name in ("unspaced_repo", "repo with spaces"):
            repo = Path(self.tmp.name) / folder_name
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
            inst = _run_cli(
                "install-hooks",
                "--repo",
                str(repo),
                "--server",
                self.base_url,
                "--agent",
                "machine-a",
                "--with-antigravity-hook",
            )
            self.assertEqual(inst.returncode, 0, inst.stderr)
            hook_cfg = json.loads((repo / ".agents" / "hooks.json").read_text(encoding="utf-8"))
            hook_cmd = hook_cfg["agsync-lease-guard"]["PreToolUse"][0]["hooks"][0]["command"]
            self.assertIn("python", hook_cmd)

            payload_allow = json.dumps(
                {
                    "toolCall": {"name": "read_file", "args": {"Path": "src/locked.py"}},
                    "cwd": str(repo),
                }
            )
            payload_deny = json.dumps(
                {
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "locked.py")}},
                    "cwd": str(repo),
                }
            )
            if os.name == "nt":
                proc_allow = subprocess.run(
                    f"cmd /c {hook_cmd}",
                    cwd=str(repo / ".agents"),
                    input=payload_allow,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                proc_deny = subprocess.run(
                    f"cmd /c {hook_cmd}",
                    cwd=str(repo / ".agents"),
                    input=payload_deny,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            else:
                proc_allow = subprocess.run(
                    ["sh", "-c", hook_cmd],
                    cwd=str(repo / ".agents"),
                    input=payload_allow,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                proc_deny = subprocess.run(
                    ["sh", "-c", hook_cmd],
                    cwd=str(repo / ".agents"),
                    input=payload_deny,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(proc_allow.returncode, 0, f"{folder_name}: {proc_allow.stderr}")
            self.assertEqual(json.loads(proc_allow.stdout)["decision"], "allow")
            self.assertEqual(proc_deny.returncode, 0, f"{folder_name}: {proc_deny.stderr}")
            self.assertEqual(json.loads(proc_deny.stdout)["decision"], "deny")

    def test_smoke_check_script_spawn_local(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "smoke_check.py"), "--spawn-local"],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SMOKE CHECK PASSED", proc.stdout)

    def test_templates_valid_schemas(self) -> None:
        stdio_cfg = json.loads(
            (REPO_ROOT / "templates" / "mcp_config.stdio.json").read_text(encoding="utf-8")
        )
        self.assertIn("agsync", stdio_cfg["mcpServers"])
        self.assertEqual(stdio_cfg["mcpServers"]["agsync"]["command"], "python")

        http_cfg = json.loads(
            (REPO_ROOT / "templates" / "mcp_config.http.json").read_text(encoding="utf-8")
        )
        self.assertTrue(http_cfg["mcpServers"]["agsync"]["url"].endswith("/mcp?agent=machine-b"))
        self.assertNotIn("/sse", http_cfg["mcpServers"]["agsync"]["url"])

        hooks_cfg = json.loads(
            (REPO_ROOT / "templates" / ".agents" / "hooks.json").read_text(encoding="utf-8")
        )
        self.assertIn("PreToolUse", hooks_cfg["agsync-lease-guard"])

        for skill_name in ("sync-start", "sync-check", "sync-handoff"):
            skill_md = (
                REPO_ROOT / "templates" / ".agents" / "skills" / skill_name / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertTrue(skill_md.startswith("---\n"))
            self.assertIn(f"name: {skill_name}", skill_md)
            self.assertIn("description:", skill_md)


if __name__ == "__main__":
    unittest.main()
