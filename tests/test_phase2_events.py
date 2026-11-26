"""Phase 2 Milestone 1 tests: Schema v2, 1000-row events ring buffer, Condition sync_wait, GET /events, and /dashboard."""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from agsync.client import SyncClient, SyncClientError
from agsync.db import SCHEMA_VERSION, _BASE_SCHEMA_SQL
from agsync.protocol import MCP_TOOLS
from agsync.server import CoordinationHandler, create_server
from agsync.store import SyncStore, ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent

_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def _rpc_send(proc: subprocess.Popen[str], payload: dict) -> dict | None:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    if payload.get("id") is None:
        return None
    line = proc.stdout.readline()
    if not line:
        raise AssertionError("Unexpected EOF from mcp_bridge")
    return json.loads(line)


def _http_mcp(
    url: str, payload: dict, *, headers: dict[str, str] | None = None
) -> tuple[int, dict | None]:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=hdrs,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, (json.loads(raw) if raw else None)


class TestSchemaV2AndEventsRingBuffer(unittest.TestCase):
    def test_fresh_schema_v2_and_idempotent_v1_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # 1. Fresh DB -> SCHEMA_VERSION == 2
            fresh_db = tmp_path / "fresh.db"
            store = SyncStore(fresh_db)
            try:
                ver = store.db.conn.execute("PRAGMA user_version;").fetchone()[0]
                self.assertEqual(ver, SCHEMA_VERSION)
                self.assertEqual(SCHEMA_VERSION, 2)
                tables = {
                    r[0]
                    for r in store.db.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table';"
                    ).fetchall()
                }
                self.assertTrue({"events", "invites", "agent_tokens"}.issubset(tables))
                indexes = {
                    r[0]
                    for r in store.db.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index';"
                    ).fetchall()
                }
                self.assertTrue(
                    {"idx_events_created_at", "idx_invites_expires"}.issubset(indexes)
                )
            finally:
                store.close()

            # 2. Pre-populated Phase 1 DB (user_version = 1) migrates cleanly 1 -> 2
            v1_db = tmp_path / "v1.db"
            conn = sqlite3.connect(str(v1_db))
            conn.executescript(_BASE_SCHEMA_SQL)
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, machine_name, role, branch, status,
                    capabilities_json, registered_at, last_heartbeat, ttl_sec
                ) VALUES ('legacy-a', 'pc1', 'dev', 'main', 'online', '[]', 100.0, 100.0, 120);
                """
            )
            conn.execute("PRAGMA user_version = 1;")
            conn.commit()
            conn.close()

            migrated = SyncStore(v1_db)
            try:
                ver_m = migrated.db.conn.execute("PRAGMA user_version;").fetchone()[0]
                self.assertEqual(ver_m, 2)
                agents = migrated.list_agents()
                self.assertEqual([a["agent_id"] for a in agents], ["legacy-a"])
                ev_cols = {
                    r["name"]
                    for r in migrated.db.conn.execute("PRAGMA table_info(events);").fetchall()
                }
                self.assertEqual(
                    ev_cols,
                    {
                        "id",
                        "boot_id",
                        "event_type",
                        "actor_id",
                        "target_id",
                        "summary",
                        "payload_json",
                        "created_at",
                    },
                )
            finally:
                migrated.close()

            # Re-opening is idempotent and yields a new boot_id
            reopened = SyncStore(v1_db)
            try:
                self.assertNotEqual(migrated.boot_id, reopened.boot_id)
                self.assertEqual(
                    reopened.db.conn.execute("PRAGMA user_version;").fetchone()[0], 2
                )
            finally:
                reopened.close()

    def test_all_mutations_emit_events_and_idempotent_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000.0
            store = SyncStore(Path(tmp) / "events.db", time_fn=lambda: now)
            try:
                store.register_agent("a1", branch="feat/x")
                store.heartbeat_agent("a1", status="busy")
                store.send_message("a1", "a2", "hello")
                # Read-only poll and empty auto_ack poll must NOT emit events
                before_poll_id = store.list_events()["latest_id"]
                store.poll_messages("a2", unread_only=True, auto_ack=False)
                self.assertEqual(store.list_events()["latest_id"], before_poll_id)
                store.poll_messages("a1", unread_only=True, auto_ack=True)
                self.assertEqual(store.list_events()["latest_id"], before_poll_id)
                # Non-empty auto_ack poll DOES emit messages_acked
                store.poll_messages("a2", unread_only=True, auto_ack=True)
                store.ack_messages("a2", 99)

                t = store.create_task("Task 1", "a1", task_id="t1")
                store.claim_task(t["task_id"], "a1")
                count_before_idempotent = store.list_events()["latest_id"]
                # Idempotent re-claim must NOT emit a duplicate event
                store.claim_task(t["task_id"], "a1")
                self.assertEqual(store.list_events()["latest_id"], count_before_idempotent)

                store.update_task(t["task_id"], "a1", status="done", result_summary="ok")
                store.put_context("k1", {"v": 1}, "a1")

                store.acquire_lease("src/a.py", "a1", ttl_sec=30)
                store.acquire_lease("src/a.py", "a1", ttl_sec=30)  # exact own renewal
                store.renew_lease("a1", resource="src/a.py", ttl_sec=60)
                store.release_lease("a1", resource="src/a.py")

                store.acquire_lease("src/b.py", "a2", ttl_sec=5)
                now += 10.0
                expired = store.purge_expired_leases()
                self.assertEqual(expired, 1)

                store.acquire_lease("src/c.py", "a1", ttl_sec=30)
                store.release_lease("a1", release_all=True)

                bundle = store.export_state()
                self.assertEqual(bundle["format_version"], 1)
                self.assertEqual(
                    set(bundle["tables"].keys()),
                    {"agents", "messages", "agent_cursors", "tasks", "leases", "context_kv", "counters"},
                )
                store.import_state(bundle, replace=True)

                ev_res = store.list_events(since_id=0, limit=100)
                ev_types = [e["event_type"] for e in ev_res["events"]]
                for expected in (
                    "agent_registered",
                    "agent_heartbeat",
                    "message_sent",
                    "messages_acked",
                    "task_created",
                    "task_claimed",
                    "task_updated",
                    "context_updated",
                    "lease_acquired",
                    "lease_renewed",
                    "lease_released",
                    "lease_expired",
                    "state_imported",
                ):
                    self.assertIn(expected, ev_types, f"Missing {expected} in {ev_types}")

                # snapshot() must stay strictly read_tx()-only and expose boot_id & latest_event_id
                with mock.patch.object(
                    store.db, "write_tx", side_effect=AssertionError("snapshot took write_tx")
                ):
                    snap = store.snapshot()
                self.assertEqual(snap["boot_id"], store.boot_id)
                self.assertEqual(snap["latest_event_id"], ev_res["latest_id"])
                self.assertTrue(snap["latest_events"])
            finally:
                store.close()

    def test_ring_buffer_strict_1000_cap_and_purge_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000.0
            store = SyncStore(Path(tmp) / "ring.db", time_fn=lambda: now)
            try:
                for i in range(1150):
                    store.put_context(f"k-{i % 10}", i, "agent-a")
                cnt = store.db.conn.execute("SELECT COUNT(*) FROM events;").fetchone()[0]
                bounds = store.db.conn.execute(
                    "SELECT MIN(id) AS min_id, MAX(id) AS max_id FROM events;"
                ).fetchone()
                self.assertEqual(cnt, 1000)
                self.assertEqual(int(bounds["min_id"]), 151)
                self.assertEqual(int(bounds["max_id"]), 1150)

                # Reset boundary checks: since_id == min_id - 1 (150) -> reset=False
                no_reset = store.list_events(since_id=150, limit=10)
                self.assertFalse(no_reset["reset"])
                self.assertEqual(no_reset["events"][0]["id"], 151)

                # since_id == min_id - 2 (149) -> reset=True
                yes_reset = store.list_events(since_id=149, limit=10)
                self.assertTrue(yes_reset["reset"])
                self.assertEqual(len(yes_reset["events"]), 10)
                self.assertEqual(yes_reset["events"][-1]["id"], 1150)

                # Age-based purge removes old events, keeps fresh events
                now += 90000.0
                store.put_context("fresh", 1, "agent-a")
                deleted = store.purge_events(max_age_sec=86400.0, max_rows=1000)
                self.assertEqual(deleted, 999)
                remaining = store.list_events(since_id=0)
                self.assertEqual(len(remaining["events"]), 1)
                self.assertEqual(remaining["events"][0]["target_id"], "fresh")
            finally:
                store.close()


class TestConditionWaitAndHTTPEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "events_http.db"
        self.srv = create_server(
            host="127.0.0.1",
            port=0,
            db_path=self.db_path,
            token="phase2-secret",
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

    def test_wait_does_not_hold_db_lock_and_wakes_immediately(self) -> None:
        store = self.srv.store
        wait_result: dict = {}

        def waiter() -> None:
            wait_result.update(store.wait_for_events(since_id=0, timeout_sec=5.0))

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.15)

        # While waiter is blocked, concurrent read_tx and write_tx must NOT block!
        t0 = time.monotonic()
        snap = store.snapshot()
        self.assertIn("boot_id", snap)
        store.send_message("agent-a", "agent-b", "wake up")
        t.join(timeout=2.0)
        elapsed = time.monotonic() - t0

        self.assertFalse(t.is_alive())
        self.assertLess(elapsed, 1.5)
        self.assertFalse(wait_result["timed_out"])
        self.assertFalse(wait_result["reset"])
        self.assertTrue(wait_result["events"])
        self.assertEqual(wait_result["events"][-1]["event_type"], "message_sent")

        # Short timeout returns cleanly with timed_out=True
        latest = wait_result["latest_id"]
        t_out = store.wait_for_events(since_id=latest, timeout_sec=0.12)
        self.assertTrue(t_out["timed_out"])
        self.assertEqual(t_out["events"], [])

        # Timeout > 15s is clamped (verify validation & clamping)
        with self.assertRaises(ValidationError):
            store.wait_for_events(timeout_sec=-1.0)
        with self.assertRaises(ValidationError):
            store.wait_for_events(timeout_sec=True)  # type: ignore[arg-type]

        # Verify SyncClient.wait_events clamps wait=99 -> 15.0 and overrides HTTP timeout >= 20.0
        c_check = SyncClient(self.base_url, token="phase2-secret", timeout=5.0)
        recorded_timeouts: list[float] = []
        orig_request = c_check._request

        def spy_request(method, path, *, body=None, query=None, timeout=None):
            if timeout is not None:
                recorded_timeouts.append(float(timeout))
            return orig_request(method, path, body=body, query=query, timeout=timeout)

        with mock.patch.object(c_check, "_request", side_effect=spy_request):
            # Trigger immediate reset with stale boot_id so call returns instantly while passing wait=99
            res_clamp = c_check.wait_events(since_id=0, boot_id="stale", timeout_sec=99.0)
            self.assertTrue(res_clamp["reset"])
            self.assertFalse(res_clamp["timed_out"])
        self.assertEqual(len(recorded_timeouts), 1)
        self.assertGreaterEqual(recorded_timeouts[0], 20.0)

    def test_http_events_endpoint_auth_reset_and_long_poll(self) -> None:
        self.assertEqual(CoordinationHandler.timeout, 30.0)
        no_auth = SyncClient(self.base_url)
        health = no_auth.health()
        self.assertEqual(health["boot_id"], self.srv.store.boot_id)
        self.assertEqual(health["latest_event_id"], 0)

        with self.assertRaises(SyncClientError) as unauth_err:
            no_auth.get_events()
        self.assertEqual(unauth_err.exception.status_code, 401)

        client = SyncClient(self.base_url, token="phase2-secret", timeout=0.2)
        client.register("agent-a")
        ev0 = client.get_events(since_id=0)
        self.assertEqual(ev0["boot_id"], health["boot_id"])
        self.assertFalse(ev0["reset"])
        self.assertEqual(len(ev0["events"]), 1)
        cur_id = ev0["latest_id"]

        # boot_id mismatch -> reset=True immediately
        mismatch = client.get_events(since_id=cur_id, boot_id="stale-boot-id", wait=10.0)
        self.assertTrue(mismatch["reset"])
        self.assertEqual(len(mismatch["events"]), 1)

        # since_id > latest_id -> reset=True immediately
        ahead = client.get_events(since_id=cur_id + 50, boot_id=health["boot_id"], wait=10.0)
        self.assertTrue(ahead["reset"])

        # types filter still returns global latest_id
        client.put_context("cfg", {"a": 1}, "agent-a")
        filtered = client.get_events(since_id=cur_id, types=["lease_acquired"])
        self.assertEqual(filtered["events"], [])
        self.assertGreater(filtered["latest_id"], cur_id)

        # wait_events with client.timeout=0.2 and wait=1.5 succeeds via >= wait + 5.0 override
        cur_id = filtered["latest_id"]
        wait_res: dict = {}

        def do_wait() -> None:
            wait_res.update(
                client.wait_events(since_id=cur_id, boot_id=health["boot_id"], timeout_sec=1.5)
            )

        wt = threading.Thread(target=do_wait, daemon=True)
        wt.start()
        time.sleep(0.35)  # Longer than client.timeout (0.2s) to prove override works!
        writer = SyncClient(self.base_url, token="phase2-secret")
        writer.acquire_lease("src/ui.tsx", "agent-b")
        wt.join(timeout=3.0)
        self.assertFalse(wait_res["timed_out"])
        self.assertEqual(wait_res["events"][-1]["event_type"], "lease_acquired")

        # Validation errors on /events -> 400
        for bad_q in ("since_id=-1", "limit=0", "limit=1001", "wait=-0.5", "wait=nan"):
            req = urllib.request.Request(
                f"{self.base_url}/events?{bad_q}",
                headers={"Authorization": "Bearer phase2-secret"},
            )
            with self.assertRaises(urllib.error.HTTPError) as http_err:
                urllib.request.urlopen(req, timeout=3.0)
            self.assertEqual(http_err.exception.code, 400, f"Expected 400 for {bad_q}")

        # Early client disconnect during long-poll must not crash server
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2.0)
        sock.sendall(
            (
                f"GET /events?since_id={wait_res['latest_id']}&wait=10 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Authorization: Bearer phase2-secret\r\n\r\n"
            ).encode("ascii")
        )
        time.sleep(0.1)
        sock.close()
        time.sleep(0.1)
        self.assertEqual(no_auth.health()["status"], "ok")

    def test_mcp_http_and_stdio_sync_wait_and_no_default_agent(self) -> None:
        self.assertEqual(len(MCP_TOOLS), 14)
        self.assertIn("sync_wait", [t["name"] for t in MCP_TOOLS])

        mcp_url = f"{self.base_url}/mcp?agent=agent-a"
        hdrs = {"Authorization": "Bearer phase2-secret"}
        _, tools_resp = _http_mcp(
            mcp_url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=hdrs
        )
        assert tools_resp is not None
        self.assertEqual(len(tools_resp["result"]["tools"]), 14)

        # Peer agent-b sends message to agent-c; agent-a calling sync_wait WITHOUT agent_id must still see it!
        c = SyncClient(self.base_url, token="phase2-secret")
        c.send_message("agent-b", "agent-c", "peer-to-peer")
        _, wait_all_resp = _http_mcp(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "sync_wait", "arguments": {"since_id": 0, "timeout_sec": 1.0}},
            },
            headers=hdrs,
        )
        assert wait_all_resp is not None
        sc = wait_all_resp["result"]["structuredContent"]
        self.assertFalse(wait_all_resp["result"]["isError"])
        self.assertTrue(any(e["actor_id"] == "agent-b" and e["target_id"] == "agent-c" for e in sc["events"]))

        # When agent_id='agent-a' IS explicitly given, private message_sent b->c is filtered out
        _, wait_a_resp = _http_mcp(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sync_wait",
                    "arguments": {"since_id": 0, "timeout_sec": 0.1, "agent_id": "agent-a"},
                },
            },
            headers=hdrs,
        )
        assert wait_a_resp is not None
        self.assertEqual(wait_a_resp["result"]["structuredContent"]["events"], [])

        # Malformed sync_wait args return isError=True
        _, bad_resp = _http_mcp(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "sync_wait", "arguments": {"since_id": True}},
            },
            headers=hdrs,
        )
        assert bad_resp is not None
        self.assertTrue(bad_resp["result"]["isError"])

        # Stdio mcp_bridge with --timeout 0.1 and sync_wait wait > 0.1
        latest = sc["latest_id"]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agsync.mcp_bridge",
                "--server",
                self.base_url,
                "--agent",
                "agent-a",
                "--token",
                "phase2-secret",
                "--timeout",
                "0.1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(REPO_ROOT),
            env=env,
        )
        try:
            def delayed_write() -> None:
                time.sleep(0.3)
                c.put_context("bridge_wake", True, "agent-b")

            wt = threading.Thread(target=delayed_write, daemon=True)
            wt.start()
            resp = _rpc_send(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_wait",
                        "arguments": {"since_id": latest, "timeout_sec": 2.0},
                    },
                },
            )
            wt.join(timeout=2.0)
            assert resp is not None
            self.assertFalse(resp["result"]["isError"])
            evs = resp["result"]["structuredContent"]["events"]
            self.assertEqual(evs[-1]["event_type"], "context_updated")
        finally:
            if proc.stdin:
                proc.stdin.close()
            rc = proc.wait(timeout=5.0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        self.assertEqual(rc, 0)

    def test_dashboard_live_ui_and_uiux_compliance(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/dashboard", timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")

        self.assertIn("<h1>agsync Coordination Hub</h1>", html)
        self.assertIn("/snapshot", html)
        self.assertIn("/events", html)
        self.assertIn("since_id", html)
        self.assertIn("boot_id", html)
        self.assertIn("AbortController", html)
        self.assertIn("Live Activity Feed", html)
        self.assertIn("feed-filter", html)
        self.assertIn("typeof Notification", html)
        self.assertIn("Notification.requestPermission()", html)
        self.assertIn("table-wrap", html)
        self.assertIn("overflow-x: auto", html)
        self.assertIn("prefers-reduced-motion: reduce", html)
        self.assertIn("minmax(min(100%, 320px), 1fr)", html)
        self.assertIn("<svg", html)
        self.assertIsNone(_EMOJI_RE.search(html), "Dashboard HTML/JS must contain zero emojis")

        # Verify requestPermission is inside notifyBtn click handler, not top-level auto-prompt
        idx_click = html.index('notifyBtn.addEventListener("click"')
        idx_req = html.index("Notification.requestPermission()")
        self.assertGreater(idx_req, idx_click)


if __name__ == "__main__":
    unittest.main()
