"""Phase 1 hardening tests: security, CSRF, SQLite concurrency, BOM/UTF-16, Junctions, worktrees, doctor."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agsync.adapters.ide import atomic_write_text, inspect_mcp_entry
from agsync.adapters.network import (
    build_opener_for_url,
    is_local_or_private_url,
    validate_port,
)
from agsync.client import SyncClient
from agsync.commands.doctor import run_doctor
from agsync.config import configure_antigravity_mcp
from agsync.core.git_ops import git_common_dir, git_hooks_dir
from agsync.db import SCHEMA_VERSION, default_db_path
from agsync.hook_guard import evaluate_hook_payload
from agsync.server import CoordinationHandler, create_server
from agsync.store import SyncStore

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


class TestPhase1HTTPAndSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = create_server(
            host="127.0.0.1",
            port=0,
            db_path=Path(self.tmp.name) / "sec.db",
            token="tökën-non-ascii-123",
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

    def _raw_req(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_handler_timeout_and_non_ascii_token(self) -> None:
        self.assertEqual(CoordinationHandler.timeout, 30.0)
        c = SyncClient(self.base_url, token="tökën-non-ascii-123")
        snap = c.snapshot()
        self.assertIn("agents", snap)

    def test_require_json_content_type_on_mutations(self) -> None:
        st, payload = self._raw_req(
            "POST",
            "/leases/acquire",
            body=b"resource=a.py&owner_id=m1",
            headers={
                "Authorization": "Bearer tökën-non-ascii-123",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self.assertEqual(st, 415)
        self.assertEqual(payload["error"], "unsupported_media_type")

    def test_host_and_origin_csrf_dns_rebinding_blocked(self) -> None:
        st_host, p_host = self._raw_req(
            "GET",
            "/health",
            headers={"Host": f"evil.example.com:{self.port}"},
        )
        self.assertEqual(st_host, 403)
        self.assertEqual(p_host["error"], "forbidden_host")

        st_origin, p_origin = self._raw_req(
            "GET",
            "/health",
            headers={"Origin": "http://evil.example.com"},
        )
        self.assertEqual(st_origin, 403)
        self.assertEqual(p_origin["error"], "forbidden_origin")

        st_null, _ = self._raw_req("GET", "/health", headers={"Origin": "null"})
        self.assertEqual(st_null, 403)

        st_same, p_same = self._raw_req(
            "GET",
            "/health",
            headers={"Origin": self.base_url},
        )
        self.assertEqual(st_same, 200)
        self.assertEqual(p_same["status"], "ok")

    def test_get_messages_is_read_only_and_post_ack_works(self) -> None:
        c = SyncClient(self.base_url, token="tökën-non-ascii-123")
        c.send_message("m-a", "m-b", "msg-1")

        # Direct GET with auto_ack=1 must be rejected and NOT mutate cursor
        st, payload = self._raw_req(
            "GET",
            "/messages?agent_id=m-b&unread=1&auto_ack=1",
            headers={"Authorization": "Bearer tökën-non-ascii-123"},
        )
        self.assertEqual(st, 400)
        self.assertEqual(payload["error"], "validation_error")

        # Read-only GET returns the message without acking
        res_read = c.poll_messages("m-b", unread_only=True, auto_ack=False)
        self.assertEqual(len(res_read["messages"]), 1)
        self.assertEqual(res_read["cursor"], 0)

        # POST /messages/ack advances cursor
        ack = c.ack_messages("m-b", res_read["messages"][0]["id"])
        self.assertEqual(ack["last_read_msg_id"], res_read["messages"][0]["id"])
        res_after = c.poll_messages("m-b", unread_only=True, auto_ack=False)
        self.assertEqual(res_after["messages"], [])

    def test_unhandled_server_exception_returns_500_json_and_hook_denies(self) -> None:
        with mock.patch.object(self.srv.store, "check_paths", side_effect=RuntimeError("boom")):
            st, payload = self._raw_req(
                "POST",
                "/leases/check",
                body=json.dumps({"paths": ["a.py"], "agent_id": "m-a"}).encode("utf-8"),
                headers={
                    "Authorization": "Bearer tökën-non-ascii-123",
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(st, 500)
            self.assertEqual(payload["error"], "internal_error")

            # hook_guard must treat 500 as non-unreachable error and deny (not fail-open)
            repo_dir = Path(self.tmp.name) / "hook_repo"
            (repo_dir / ".git").mkdir(parents=True)
            res = evaluate_hook_payload(
                {"toolCall": {"name": "edit_file", "args": {"Path": "a.py"}}, "cwd": str(repo_dir)},
                server_url=self.base_url,
                agent_id="m-a",
                token="tökën-non-ascii-123",
                fail_closed=False,
            )
            self.assertEqual(res["decision"], "deny")

    def test_non_loopback_requires_token_unless_opted_in(self) -> None:
        db_file = Path(self.tmp.name) / "lan.db"
        with self.assertRaises(ValueError):
            create_server(host="0.0.0.0", port=0, db_path=db_file, token=None)

        srv = create_server(
            host="0.0.0.0",
            port=0,
            db_path=db_file,
            token=None,
            allow_insecure_lan=True,
        )
        srv.server_close()
        srv.store.close()


class TestPhase1SQLiteAndConcurrency(unittest.TestCase):
    def test_default_db_path_is_in_user_home(self) -> None:
        p = default_db_path()
        self.assertEqual(p.name, "state.db")
        self.assertEqual(p.parent.name, ".agsync")
        self.assertEqual(p.parent.parent, Path.home())

    def test_schema_user_version_and_read_routes_do_not_take_write_tx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000.0
            store = SyncStore(Path(tmp) / "wal.db", time_fn=lambda: now)
            try:
                ver = store.db.conn.execute("PRAGMA user_version;").fetchone()[0]
                self.assertEqual(ver, SCHEMA_VERSION)

                store.acquire_lease("src/a.py", "m-a", ttl_sec=10)
                now += 20.0  # Expired!

                with mock.patch.object(
                    store.db, "write_tx", side_effect=AssertionError("read route took write_tx")
                ):
                    self.assertEqual(store.list_leases(), [])
                    chk = store.check_paths(["src/a.py"], agent_id="m-b")
                    self.assertTrue(chk["all_clear"])
                    snap = store.snapshot()
                    self.assertEqual(snap["active_leases"], [])
                    exp = store.export_state()
                    self.assertEqual(exp["tables"]["leases"], [])
            finally:
                store.close()

    def test_write_tx_commit_failure_rolls_back_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(Path(tmp) / "tx.db")
            try:
                orig_conn = store.db.conn

                class FailingCommitConn:
                    def __init__(self, real: sqlite3.Connection) -> None:
                        self._real = real
                        self.fail_next_commit = True

                    def __getattr__(self, name: str):
                        return getattr(self._real, name)

                    def execute(self, sql: str, *args, **kwargs):
                        if sql.strip().upper() == "COMMIT;" and self.fail_next_commit:
                            self.fail_next_commit = False
                            raise sqlite3.OperationalError("simulated commit failure")
                        return self._real.execute(sql, *args, **kwargs)

                store.db.conn = FailingCommitConn(orig_conn)  # type: ignore[assignment]
                with self.assertRaises(sqlite3.OperationalError):
                    store.register_agent("m-fail")

                # Connection must not remain stuck in_transaction
                self.assertFalse(orig_conn.in_transaction)
                store.db.conn = orig_conn
                ok = store.register_agent("m-ok")
                self.assertEqual(ok["agent_id"], "m-ok")
            finally:
                store.close()

    def test_import_state_preserves_max_counters_and_creates_backup_and_task_id_no_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db1 = Path(tmp) / "s1.db"
            db2 = Path(tmp) / "s2.db"
            s1 = SyncStore(db1)
            s2 = SyncStore(db2)
            try:
                # Advance s2 fencing_seq & task_seq beyond s1
                for i in range(5):
                    s2.acquire_lease(f"f{i}.py", "m-a")
                s2.create_task("Explicit 1", "m-a", task_id="task-001")
                t_auto = s2.create_task("Auto after explicit", "m-a")
                self.assertEqual(t_auto["task_id"], "task-002")

                s1.acquire_lease("only_one.py", "m-b")
                bundle = s1.export_state()
                res = s2.import_state(bundle, replace=True)
                self.assertTrue(Path(res["backup_path"]).is_file())

                # Fencing token on next lease in s2 must stay > 5 (not rolled back to 1)
                next_lease = s2.acquire_lease("fresh.py", "m-a")
                self.assertGreater(next_lease["lease"]["fencing_token"], 5)
            finally:
                s1.close()
                s2.close()

    def test_pid_lockfile_prevents_dual_server_on_same_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "dup.db"
            srv1 = create_server(host="127.0.0.1", port=0, db_path=db_path, pid_lock=True)
            try:
                with self.assertRaises(RuntimeError):
                    create_server(host="127.0.0.1", port=0, db_path=db_path, pid_lock=True)
            finally:
                srv1.server_close()
                srv1.store.close()
            # Lockfile removed on close -> can start again
            srv2 = create_server(host="127.0.0.1", port=0, db_path=db_path, pid_lock=True)
            srv2.server_close()
            srv2.store.close()


class TestPhase1NetworkHookWorktreeDoctor(unittest.TestCase):
    def test_proxy_bypass_and_port_validation(self) -> None:
        self.assertTrue(is_local_or_private_url("http://127.0.0.1:8765/health"))
        self.assertTrue(is_local_or_private_url("http://192.168.1.50:8765/health"))
        self.assertTrue(is_local_or_private_url("http://10.0.0.8:8765/health"))
        self.assertFalse(is_local_or_private_url("https://example.com/api"))

        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1"}):
            local_opener = build_opener_for_url("http://192.168.1.10:8765")
            public_opener = build_opener_for_url("https://example.com/api")
            local_proxies = [
                h for h in local_opener.handlers if isinstance(h, urllib.request.ProxyHandler)
            ]
            public_proxies = [
                h for h in public_opener.handlers if isinstance(h, urllib.request.ProxyHandler)
            ]
            self.assertEqual(local_proxies, [])
            self.assertTrue(public_proxies)

            # End-to-end verification: SyncClient bypasses dead HTTP_PROXY on loopback
            with tempfile.TemporaryDirectory() as tmp:
                srv = create_server(host="127.0.0.1", port=0, db_path=Path(tmp) / "px.db")
                t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                t.start()
                try:
                    c = SyncClient(f"http://127.0.0.1:{srv.server_address[1]}")
                    self.assertEqual(c.health()["status"], "ok")
                finally:
                    srv.shutdown()
                    srv.server_close()
                    srv.store.close()
                    t.join(timeout=2.0)

        self.assertEqual(validate_port("8765"), 8765)
        for bad in ("0", "65536", "-1", "8765; calc", "abc", True):
            with self.assertRaises(ValueError):
                validate_port(bad)

    def test_bom_utf16_settings_protected_paths_and_junction_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            srv = create_server(host="127.0.0.1", port=0, db_path=tmp_path / "hk.db")
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            try:
                base_url = f"http://127.0.0.1:{srv.server_address[1]}"
                client = SyncClient(base_url)
                client.acquire_lease("shared/secret.py", "machine-b", scope="file")

                repo = tmp_path / "repo"
                (repo / ".git").mkdir(parents=True)
                (repo / ".agsync").mkdir(parents=True)
                # Write UTF-16LE with BOM (like PowerShell Out-File)
                (repo / ".agsync" / "server_url").write_bytes((base_url + "\n").encode("utf-16"))
                (repo / ".agsync-agent").write_bytes("machine-a\n".encode("utf-16"))

                # Protected paths must be denied even if server is offline
                for prot in (".agsync/token", ".agsync/server_url", ".agsync-agent", ".agents/hooks.json"):
                    res = evaluate_hook_payload(
                        {"toolCall": {"name": "edit_file", "args": {"Path": prot}}, "cwd": str(repo)},
                        server_url="http://127.0.0.1:1",
                        agent_id="machine-a",
                    )
                    self.assertEqual(res["decision"], "deny", f"Expected deny for {prot}")
                    self.assertIn("PROTECTED", res["reason"])

                # Junction / symlink directory inside repo pointing outside
                outside_shared = tmp_path / "outside_shared"
                outside_shared.mkdir()
                (outside_shared / "secret.py").write_text("x = 1\n", encoding="utf-8")
                link_dir = repo / "shared"
                linked = False
                if os.name == "nt":
                    proc = subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(link_dir), str(outside_shared)],
                        capture_output=True,
                        check=False,
                    )
                    linked = proc.returncode == 0
                else:
                    os.symlink(outside_shared, link_dir)
                    linked = True

                if linked:
                    res_j = evaluate_hook_payload(
                        {
                            "toolCall": {
                                "name": "edit_file",
                                "args": {"Path": str(repo / "shared" / "secret.py")},
                            },
                            "cwd": str(repo),
                        }
                    )
                    self.assertEqual(res_j["decision"], "deny")
                    self.assertIn("machine-b", res_j["reason"])

                # Malformed stdin in hook_guard.main() always returns valid JSON
                bad_proc = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "agsync" / "hook_guard.py")],
                    input=b"\xff\xfe{not-valid-json",
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(json.loads(bad_proc.stdout.decode("utf-8"))["decision"], "allow")
            finally:
                srv.shutdown()
                srv.server_close()
                srv.store.close()
                t.join(timeout=2.0)

    def test_worktree_common_dir_non_destructive_hook_merge_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main_repo = tmp_path / "main_repo"
            main_repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(main_repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=str(main_repo), check=True)
            subprocess.run(["git", "config", "user.name", "Agent A"], cwd=str(main_repo), check=True)
            (main_repo / "README.md").write_text("init\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(main_repo), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(main_repo), check=True, capture_output=True)

            wt_repo = tmp_path / "wt_repo"
            subprocess.run(
                ["git", "worktree", "add", "-b", "wt-branch", str(wt_repo)],
                cwd=str(main_repo),
                check=True,
                capture_output=True,
            )

            # Pre-existing custom hook in .agents/hooks.json (UTF-8 BOM) must be preserved
            hooks_file = wt_repo / ".agents" / "hooks.json"
            hooks_file.parent.mkdir(parents=True, exist_ok=True)
            hooks_file.write_bytes(
                (b"\xef\xbb\xbf" + json.dumps({"custom-user-hook": {"PreToolUse": []}}).encode("utf-8"))
            )

            inst = _run_cli(
                "install-hooks",
                "--repo",
                str(wt_repo),
                "--server",
                "http://127.0.0.1:8765",
                "--agent",
                "wt-agent",
                "--with-antigravity-hook",
            )
            self.assertEqual(inst.returncode, 0, inst.stderr)

            cdir = git_common_dir(wt_repo)
            hdir = git_hooks_dir(wt_repo)
            pre_commit = hdir / "pre-commit"
            self.assertTrue(pre_commit.is_file())
            pre_text = pre_commit.read_text(encoding="utf-8")
            # Must not hardcode sys.executable venv path
            self.assertNotIn(sys.executable, pre_text)
            self.assertTrue((cdir / "info" / "exclude").is_file())

            merged_hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertIn("custom-user-hook", merged_hooks)
            self.assertIn("agsync-lease-guard", merged_hooks)
            self.assertTrue((wt_repo / ".agents" / "hooks.json.bak").is_file())

            # Test serverUrl vs url detection in doctor & mcp_config normalization
            mcp_path = tmp_path / "mcp_config.json"
            atomic_write_text(
                mcp_path,
                json.dumps(
                    {
                        "mcpServers": {
                            "agsync": {"serverUrl": "http://127.0.0.1:8765/sse"},
                            "other": {"command": "npx"},
                        }
                    }
                ),
                create_backup=False,
            )
            entry_info = inspect_mcp_entry({"serverUrl": "http://127.0.0.1:8765/sse"})
            self.assertTrue(any("serverUrl" in i for i in entry_info["issues"]))
            self.assertTrue(any("/sse" in i for i in entry_info["issues"]))

            doc = run_doctor(
                repo=wt_repo,
                server_url="http://127.0.0.1:8765",
                db_path=tmp_path / "doc.db",
                mcp_config_path=mcp_path,
            )
            self.assertTrue(doc["ok"])
            mcp_chk = [c for c in doc["checks"] if c["name"] == "mcp_config"][0]
            self.assertEqual(mcp_chk["status"], "warn")

            # Normalize via configure_antigravity_mcp -> preserves 'other', fixes 'agsync'
            configure_antigravity_mcp(
                server_url="http://127.0.0.1:8765",
                agent_id="wt-agent",
                mode="http",
                mcp_config_path=mcp_path,
            )
            normalized = json.loads(mcp_path.read_text(encoding="utf-8"))
            self.assertIn("other", normalized["mcpServers"])
            self.assertNotIn("serverUrl", normalized["mcpServers"]["agsync"])
            self.assertEqual(
                normalized["mcpServers"]["agsync"]["url"],
                "http://127.0.0.1:8765/mcp?agent=wt-agent",
            )


if __name__ == "__main__":
    unittest.main()
