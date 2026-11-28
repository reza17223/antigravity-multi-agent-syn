"""Tests for repo bundle export, 1-click clone, auto-sync, and lease auto-release on task completion."""

from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agsync.client import SyncClient
from agsync.commands.clone import clone_from_host, pull_latest_changes
from agsync.server import create_server
from agsync.store import SyncStore


class TestCloneAndSync(unittest.TestCase):
    def test_update_task_done_auto_releases_leases(self) -> None:
        store = SyncStore(":memory:")
        try:
            store.events.register_agent("agent-1", role="dev")
            store.acquire_lease("src/feature.py", "agent-1", scope="file", reason="working")
            store.acquire_lease("src/utils.py", "agent-1", scope="file", reason="working")
            store.events.create_task("Build feature", "host", task_id="t1")
            store.claim_task("t1", "agent-1")

            # Check leases exist
            leases_before = store.locks.list_leases(owner_id="agent-1")
            self.assertEqual(len(leases_before), 2)

            # Update task to done
            res = store.update_task("t1", "agent-1", status="done", result_summary="Feature complete")
            self.assertEqual(res["status"], "done")

            # Leases should now be automatically released!
            leases_after = store.locks.list_leases(owner_id="agent-1")
            self.assertEqual(len(leases_after), 0)
        finally:
            store.close()

    def test_repo_bundle_and_clone_from_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            host_repo = tmp / "host_project"
            host_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=str(host_repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "host@example.com"], cwd=str(host_repo), check=True)
            subprocess.run(["git", "config", "user.name", "Host Agent"], cwd=str(host_repo), check=True)
            (host_repo / "server.py").write_text("print('hello host')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(host_repo), check=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(host_repo), check=True, capture_output=True)

            server = create_server(
                host="127.0.0.1",
                port=0,
                db_path=":memory:",
                token="test-token-123",
                repo_dir=host_repo,
            )
            port = server.server_address[1]
            import threading
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()
            try:
                base_url = f"http://127.0.0.1:{port}"
                client = SyncClient(base_url, token="test-token-123", timeout=5.0)

                # Check repo status
                st = client.get_repo_status()
                self.assertTrue(st["attached"])
                self.assertTrue(st["has_git"])

                # Clone into peer destination
                peer_dest = tmp / "peer_project"
                clone_res = clone_from_host(
                    server_url=base_url,
                    dest_dir=peer_dest,
                    token="test-token-123",
                    agent_id="peer-agent",
                )
                self.assertEqual(clone_res["status"], "ok")
                self.assertTrue((peer_dest / ".git").exists())
                self.assertTrue((peer_dest / "server.py").exists())
                self.assertEqual((peer_dest / "server.py").read_text(encoding="utf-8").strip(), "print('hello host')")

                # Verify hooks, templates & .gitignore were installed
                self.assertTrue((peer_dest / ".git" / "hooks" / "pre-commit").exists())
                self.assertTrue((peer_dest / "AGENTS.md").exists())
                self.assertTrue((peer_dest / ".gitignore").exists())
                self.assertIn(".agsync/", (peer_dest / ".gitignore").read_text(encoding="utf-8"))

                # Test pulling updates from host
                (host_repo / "api.py").write_text("API_V2 = True\n", encoding="utf-8")
                subprocess.run(["git", "add", "api.py"], cwd=str(host_repo), check=True)
                subprocess.run(["git", "commit", "-m", "Add api"], cwd=str(host_repo), check=True, capture_output=True)

                pull_res = pull_latest_changes(peer_dest, base_url, token="test-token-123")
                self.assertEqual(pull_res["status"], "ok")
                self.assertTrue((peer_dest / "api.py").exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_ensure_repo_gitignore_creation_and_append(self) -> None:
        from agsync.commands.hooks import ensure_repo_gitignore
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # 1. Fresh creation
            changed, note = ensure_repo_gitignore(repo)
            self.assertTrue(changed)
            self.assertEqual(note, "created")
            self.assertTrue((repo / ".gitignore").exists())
            content = (repo / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".agsync/", content)
            self.assertIn("*.sqlite", content)
            self.assertIn(".deploy_cache.json", content)

            # 2. Already up to date
            changed2, note2 = ensure_repo_gitignore(repo)
            self.assertFalse(changed2)
            self.assertEqual(note2, "already configured")

            # 3. Partial existing .gitignore missing rules
            partial_repo = repo / "partial"
            partial_repo.mkdir()
            (partial_repo / ".gitignore").write_text("my_custom_folder/\n*.tmp\n", encoding="utf-8")
            changed3, note3 = ensure_repo_gitignore(partial_repo)
            self.assertTrue(changed3)
            self.assertIn("appended", note3)
            updated_content = (partial_repo / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("my_custom_folder/", updated_content)
            self.assertIn(".agsync/", updated_content)
            self.assertIn("*.sqlite", updated_content)
