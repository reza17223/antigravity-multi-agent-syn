"""Phase 2 Milestone 1 tests: Schema v2, 1000-row events ring buffer, Condition sync_wait, GET /events, and /dashboard."""

from __future__ import annotations

import json
import os
import re
import shutil
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


def _srgb_to_linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def _contrast_ratio(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    l1 = _relative_luminance(c1)
    l2 = _relative_luminance(c2)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _parse_css_color(raw: str, bg: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    val = raw.strip()
    if val.startswith("#"):
        h = val.lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.fullmatch(
        r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)",
        val,
    )
    if m:
        assert bg is not None, f"Background required to composite {val}"
        fg = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        alpha = float(m.group(4))
        return tuple(round(fg[i] * alpha + bg[i] * (1.0 - alpha)) for i in range(3))  # type: ignore[return-value]
    raise AssertionError(f"Unsupported CSS color format: {raw!r}")


def _extract_css_vars(block: str) -> dict[str, str]:
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


class TestM1RegressionFixes(unittest.TestCase):
    def test_wait_for_events_close_race_returns_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Deterministic close() race between event_cond exit and initial list_events()
            store1 = SyncStore(Path(tmp) / "race1.db")
            entered = threading.Event()
            release = threading.Event()
            orig_list = store1.events.list_events

            def paused_first_list(*args, **kwargs):
                entered.set()
                release.wait(timeout=3.0)
                return orig_list(*args, **kwargs)

            out1: dict = {}
            exc1: list[BaseException] = []

            def run_wait1() -> None:
                try:
                    out1.update(store1.wait_for_events(since_id=5, timeout_sec=5.0))
                except BaseException as exc:  # pragma: no cover - regression failure capture
                    exc1.append(exc)

            with mock.patch.object(store1.events, "list_events", side_effect=paused_first_list):
                t = threading.Thread(target=run_wait1, daemon=True)
                t.start()
                self.assertTrue(entered.wait(timeout=2.0))
                store1.close()
                release.set()
                t.join(timeout=3.0)

            self.assertFalse(t.is_alive())
            self.assertEqual(exc1, [])
            self.assertEqual(
                out1,
                {
                    "boot_id": store1.boot_id,
                    "latest_id": 5,
                    "reset": False,
                    "timed_out": True,
                    "events": [],
                },
            )
            # Normal read_tx/write_tx callers still raise ProgrammingError cleanly on closed DB
            with self.assertRaises(sqlite3.ProgrammingError):
                with store1.db.read_tx():
                    pass
            with self.assertRaises(sqlite3.ProgrammingError):
                with store1.db.write_tx():
                    pass

            # 2. Deterministic close() race on loop list_events() after event_cond wakeup
            store2 = SyncStore(Path(tmp) / "race2.db")
            call_count = 0
            loop_entered = threading.Event()
            loop_release = threading.Event()
            orig_list2 = store2.events.list_events

            def paused_second_list(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    loop_entered.set()
                    loop_release.wait(timeout=3.0)
                return orig_list2(*args, **kwargs)

            out2: dict = {}
            exc2: list[BaseException] = []

            def run_wait2() -> None:
                try:
                    out2.update(store2.wait_for_events(since_id=0, timeout_sec=5.0))
                except BaseException as exc:  # pragma: no cover - regression failure capture
                    exc2.append(exc)

            with mock.patch.object(store2.events, "list_events", side_effect=paused_second_list):
                t2 = threading.Thread(target=run_wait2, daemon=True)
                t2.start()
                time.sleep(0.08)
                store2.db.notify_events()
                self.assertTrue(loop_entered.wait(timeout=2.0))
                store2.close()
                loop_release.set()
                t2.join(timeout=3.0)

            self.assertFalse(t2.is_alive())
            self.assertEqual(exc2, [])
            self.assertTrue(out2["timed_out"])
            self.assertEqual(out2["events"], [])
            self.assertEqual(out2["boot_id"], store2.boot_id)

    def test_dashboard_wcag_aa_contrast_tokens(self) -> None:
        from agsync.dashboard import DASHBOARD_HTML

        dark_match = re.search(
            r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{(.*?)\}\s*\}",
            DASHBOARD_HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(dark_match, "Missing dark mode :root token block")
        assert dark_match is not None
        html_without_dark = DASHBOARD_HTML.replace(dark_match.group(0), "")
        light_match = re.search(r":root\s*\{(.*?)\}", html_without_dark, re.DOTALL)
        self.assertIsNotNone(light_match, "Missing light mode :root token block")
        assert light_match is not None

        light_vars = _extract_css_vars(light_match.group(1))
        dark_vars = _extract_css_vars(dark_match.group(1))

        # Verify interactive controls use --control-border
        ctrl_rule = re.search(r"button,\s*input,\s*select\s*\{(.*?)\}", DASHBOARD_HTML, re.DOTALL)
        self.assertIsNotNone(ctrl_rule)
        assert ctrl_rule is not None
        self.assertIn("var(--control-border)", ctrl_rule.group(1))

        for mode_name, tokens in (("light", light_vars), ("dark", dark_vars)):
            surface = _parse_css_color(tokens["--surface"])
            bg = _parse_css_color(tokens["--bg"])
            text = _parse_css_color(tokens["--text"])
            muted = _parse_css_color(tokens["--muted"])
            accent = _parse_css_color(tokens["--accent"])
            accent_fg = _parse_css_color(tokens["--accent-fg"])
            ctrl_border = _parse_css_color(tokens["--control-border"])

            # Text contrast >= 4.5:1
            self.assertGreaterEqual(_contrast_ratio(text, surface), 4.5, f"{mode_name} text/surface")
            self.assertGreaterEqual(_contrast_ratio(text, bg), 4.5, f"{mode_name} text/bg")
            self.assertGreaterEqual(_contrast_ratio(muted, surface), 4.5, f"{mode_name} muted/surface")
            self.assertGreaterEqual(_contrast_ratio(muted, bg), 4.5, f"{mode_name} muted/bg")
            self.assertGreaterEqual(_contrast_ratio(accent_fg, accent), 4.5, f"{mode_name} accent")

            for sem in ("ok", "warn", "danger", "info"):
                fg = _parse_css_color(tokens[f"--{sem}"])
                sem_bg = _parse_css_color(tokens[f"--{sem}-bg"], bg=surface)
                self.assertGreaterEqual(
                    _contrast_ratio(fg, sem_bg),
                    4.5,
                    f"{mode_name} --{sem} on --{sem}-bg",
                )

            # Non-text UI control border contrast >= 3.0:1 against both surface and header bg
            self.assertGreaterEqual(
                _contrast_ratio(ctrl_border, surface),
                3.0,
                f"{mode_name} control-border/surface",
            )
            self.assertGreaterEqual(
                _contrast_ratio(ctrl_border, bg),
                3.0,
                f"{mode_name} control-border/bg",
            )

    def test_dashboard_load_snapshot_preserves_since_id_and_bounds_seen_ids(self) -> None:
        from agsync.dashboard import DASHBOARD_HTML

        # Pure-Python static verification of loadSnapshot & bounded seenEventIds
        snap_fn_match = re.search(
            r"async function loadSnapshot\b.*?\n    \}",
            DASHBOARD_HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(snap_fn_match)
        assert snap_fn_match is not None
        snap_fn = snap_fn_match.group(0)
        idx_if_seed = snap_fn.index("if (seedEvents)")
        idx_since = snap_fn.index("sinceId = Math.max(sinceId, snap.latest_event_id)")
        self.assertGreater(
            idx_since,
            idx_if_seed,
            "loadSnapshot must only advance sinceId when seedEvents is true",
        )
        self.assertIn("MAX_SEEN_EVENT_IDS", DASHBOARD_HTML)
        self.assertIn("seenEventIds.delete(", DASHBOARD_HTML)

        node_bin = shutil.which("node")
        if not node_bin:
            return

        # Live Node.js vm execution verifying zero dropped intervening events & bounded seenEventIds
        script_match = re.search(r"<script>(.*)</script>", DASHBOARD_HTML, re.DOTALL)
        self.assertIsNotNone(script_match)
        assert script_match is not None
        js_src = script_match.group(1)

        with tempfile.TemporaryDirectory() as tmp:
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=Path(tmp) / "dash_js.db",
                token="dash-secret",
            )
            port = srv.server_address[1]
            base_url = f"http://127.0.0.1:{port}"
            th = threading.Thread(
                target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            th.start()
            try:
                node_harness = """
const vm = require("vm");
const baseUrl = process.argv[1];
const jsSrc = fs_read();
function fs_read() {
  return require("fs").readFileSync(0, "utf8");
}

function makeEl(id) {
  return {
    id,
    value: id === "feed-filter" ? "all" : (id === "token-input" ? "dash-secret" : ""),
    textContent: "",
    innerHTML: "",
    disabled: false,
    style: {},
    _listeners: {},
    _attrs: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, f) { if (f) this._set.add(c); else this._set.delete(c); },
    },
    addEventListener(ev, fn) { this._listeners[ev] = fn; },
    setAttribute(k, v) { this._attrs[k] = v; },
  };
}

const els = {};
const getEl = id => (els[id] || (els[id] = makeEl(id)));
const notifications = [];
class FakeNotification {
  constructor(title, opts) { notifications.push({ title, opts }); }
  static permission = "granted";
  static async requestPermission() { return "granted"; }
}

let snapshotCalls = 0;
let notifiedMidSnapshot = false;
async function wrappedFetch(url, opts) {
  const full = url.startsWith("http") ? url : (baseUrl + url);
  if (url.startsWith("/snapshot")) {
    snapshotCalls += 1;
    if (snapshotCalls === 2 && !notifiedMidSnapshot) {
      notifiedMidSnapshot = true;
      await fetch(baseUrl + "/context/ev2_during_snapshot", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer dash-secret",
        },
        body: JSON.stringify({ value: 2, updated_by: "agent-a" }),
      });
    }
  }
  return fetch(full, opts);
}

const ctx = {
  document: { getElementById: getEl },
  window: { addEventListener() {} },
  sessionStorage: { getItem() { return ""; }, setItem() {} },
  Notification: FakeNotification,
  fetch: wrappedFetch,
  AbortController,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  Date,
  Math,
  Number,
  String,
  Array,
  Set,
};

vm.runInNewContext(
  jsSrc + "\\nthis.__state = () => ({ sinceId, feedItems, seenSize: seenEventIds.size, MAX_SEEN_EVENT_IDS, appendFeedEvents, seedFeed });",
  ctx
);

(async () => {
  await els["notify-btn"]._listeners.click();
  await new Promise(r => setTimeout(r, 150));
  await fetch(baseUrl + "/context/ev1_wake", {
    method: "PUT",
    headers: { "Content-Type": "application/json", "Authorization": "Bearer dash-secret" },
    body: JSON.stringify({ value: 1, updated_by: "agent-a" }),
  });
  await new Promise(r => setTimeout(r, 200));
  await fetch(baseUrl + "/context/ev3_after", {
    method: "PUT",
    headers: { "Content-Type": "application/json", "Authorization": "Bearer dash-secret" },
    body: JSON.stringify({ value: 3, updated_by: "agent-a" }),
  });
  await new Promise(r => setTimeout(r, 200));
  vm.runInContext("abortLongPoll(); liveEnabled = false;", ctx);
  const st = ctx.__state();
  const ids = st.feedItems.map(e => e.id);
  const targets = st.feedItems.map(e => e.target_id);

  // Verify seenEventIds stays bounded under sustained stream
  const batch = [];
  for (let i = 100; i < 800; i++) {
    batch.push({ id: i, event_type: "context_updated", actor_id: "a", target_id: "k" + i, summary: "s", created_at: 1 });
  }
  st.appendFeedEvents(batch);
  const afterSt = ctx.__state();

  console.log(JSON.stringify({
    snapshotCalls,
    ids,
    targets,
    notifCount: notifications.length,
    seenSize: afterSt.seenSize,
    maxSeen: afterSt.MAX_SEEN_EVENT_IDS,
    feedLen: afterSt.feedItems.length,
  }));
})();
"""
                proc = subprocess.run(
                    [node_bin, "-e", node_harness, base_url],
                    input=js_src,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=15.0,
                )
                self.assertEqual(proc.returncode, 0, f"Node harness failed: {proc.stderr}")
                res = json.loads(proc.stdout.strip().splitlines()[-1])
                self.assertEqual(res["ids"], [1, 2, 3])
                self.assertEqual(
                    res["targets"],
                    ["ev1_wake", "ev2_during_snapshot", "ev3_after"],
                )
                self.assertEqual(res["notifCount"], 3)
                self.assertLessEqual(res["seenSize"], res["maxSeen"])
                self.assertEqual(res["feedLen"], 100)
            finally:
                srv.shutdown()
                srv.server_close()
                srv.store.close()
                th.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
