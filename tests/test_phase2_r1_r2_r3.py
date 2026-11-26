"""Phase 2 tests for multi-IDE hook-proxy, git worktree, and remote invite/join + tunnel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agsync.adapters.ide_hooks import evaluate_proxy
from agsync.adapters.tunnel import build_cloudflared_command, format_join_link
from agsync.client import SyncClient, SyncClientError
from agsync.commands.worktree import create_worktree, list_worktrees, remove_worktree
from agsync.server import create_server


class TestPhase2HookProxyWorktreeAndRemote(unittest.TestCase):
    def test_hook_proxy_antigravity_claude_cursor_and_record_and_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            srv = create_server(host="127.0.0.1", port=0, db_path=tmp_path / "hp.db")
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            try:
                base_url = f"http://127.0.0.1:{srv.server_address[1]}"
                c = SyncClient(base_url)
                c.acquire_lease("src/locked.py", "peer-b", scope="file", reason="busy")

                repo = tmp_path / "repo"
                (repo / ".git").mkdir(parents=True)

                # 1. Antigravity ask/deny
                rc_ag, out_ag, _ = evaluate_proxy(
                    "antigravity",
                    {"toolCall": {"name": "edit_file", "args": {"Path": "src/locked.py"}}, "cwd": str(repo)},
                    server_url=base_url,
                    agent_id="agent-a",
                    ask_mode=True,
                    record=True,
                )
                self.assertEqual(rc_ag, 0)
                self.assertEqual(json.loads(out_ag)["decision"], "ask")
                self.assertTrue(list((repo / ".agsync" / "hook-samples").glob("antigravity-*.json")))

                # 2. Claude Code snake_case -> exit 2 + stderr
                rc_cl, out_cl, err_cl = evaluate_proxy(
                    "claude",
                    {"tool_name": "Edit", "tool_input": {"file_path": "src/locked.py"}, "cwd": str(repo)},
                    server_url=base_url,
                    agent_id="agent-a",
                )
                self.assertEqual(rc_cl, 2)
                self.assertEqual(out_cl, "")
                self.assertIn("peer-b", err_cl)

                # 3. Cursor deduplication when AGSYNC_HOOK_PROXY_ACTIVE=1
                with mock.patch.dict(os.environ, {"AGSYNC_HOOK_PROXY_ACTIVE": "1"}):
                    rc_cur, out_cur, _ = evaluate_proxy(
                        "cursor",
                        {"tool_name": "edit", "tool_input": {"file_path": "src/locked.py"}, "cwd": str(repo)},
                        server_url=base_url,
                        agent_id="agent-a",
                    )
                    self.assertEqual(rc_cur, 0)
                    self.assertTrue(json.loads(out_cur)["continue"])

                # 4. Chained user hook failure blocks allowed edit
                rc_ch, out_ch, _ = evaluate_proxy(
                    "antigravity",
                    {"toolCall": {"name": "edit_file", "args": {"Path": "src/free.py"}}, "cwd": str(repo)},
                    server_url=base_url,
                    agent_id="agent-a",
                    chain_cmd="exit 3",
                )
                self.assertEqual(rc_ch, 0)
                self.assertEqual(json.loads(out_ch)["decision"], "deny")
            finally:
                srv.shutdown()
                srv.server_close()
                srv.store.close()
                t.join(timeout=2.0)

    def test_worktree_create_list_remove_with_agent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "myproj"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "Agent A"], cwd=str(repo), check=True)
            (repo / "README.md").write_text("hi\n", encoding="utf-8")
            (repo / "node_modules").mkdir()
            (repo / "node_modules" / "pkg.txt").write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            created = create_worktree(repo, "wt-agent-1")
            wt_path = Path(created["worktree"])
            self.assertTrue(wt_path.is_dir())
            self.assertEqual(wt_path.name, "myproj-wt-1")
            self.assertEqual((wt_path / ".agsync-agent").read_text(encoding="utf-8").strip(), "wt-agent-1")
            self.assertIn("node_modules", created["linked_deps"])

            wts = list_worktrees(repo)
            self.assertEqual(len(wts), 2)

            removed = remove_worktree(repo, str(wt_path), force=True)
            self.assertEqual(removed["removed"], str(wt_path))
            self.assertEqual(len(list_worktrees(repo)), 1)

    def test_remote_invite_join_single_use_and_tunnel_command(self) -> None:
        cmd = build_cloudflared_command("http://127.0.0.1:8765", protocol="http2")
        self.assertEqual(cmd, ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8765", "--protocol", "http2"])
        self.assertIn("nonce=n1", format_join_link("https://hub.example.com/", "n1", "s1", "peer-x"))

        with tempfile.TemporaryDirectory() as tmp:
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=Path(tmp) / "inv.db",
                token="admin-secret",
            )
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            try:
                base_url = f"http://127.0.0.1:{srv.server_address[1]}"
                admin = SyncClient(base_url, token="admin-secret")
                inv = admin.create_invite("remote-peer-1", ttl_sec=600)

                # Consume invite without prior token -> returns per-agent token
                anon = SyncClient(base_url)
                joined = anon.join_invite(inv["nonce"], inv["signature"])
                self.assertEqual(joined["agent_id"], "remote-peer-1")
                self.assertTrue(joined["token"].startswith("agt_"))

                # Replay must fail (409 invite_already_used)
                with self.assertRaises(SyncClientError) as replay_err:
                    anon.join_invite(inv["nonce"], inv["signature"])
                self.assertEqual(replay_err.exception.status_code, 409)

                # Per-agent token works on coordination routes, but NOT on /state/export when admin_token differs
                peer = SyncClient(base_url, token=joined["token"])
                reg = peer.register("remote-peer-1", branch="feat/remote")
                self.assertEqual(reg["agent_id"], "remote-peer-1")
            finally:
                srv.shutdown()
                srv.server_close()
                srv.store.close()
                t.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
