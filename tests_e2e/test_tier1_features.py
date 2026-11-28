"""Tier 1 Opaque-Box Happy-Path E2E Tests covering Phase 2 Features F2-F16.

Zero third-party dependencies (Python 3.12 stdlib only).
Zero top-level imports of unimplemented Phase 2 symbols.
Zero skipTest on unimplemented Phase 2 features.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from tests_e2e.helpers import (
    REPO_ROOT,
    TempWorkspace,
    auth_headers,
    create_v1_sqlite_db,
    drain_http_events,
    http_json,
    http_request,
    inspect_sqlite,
    inspect_sqlite_schema,
    is_pid_alive,
    mcp_call,
    mcp_initialize,
    mcp_list_tools,
    send_persistent_put_batch,
)


def _register_agent_http(
    base_url: str,
    agent_id: str,
    *,
    token: str | None = None,
    role: str = "agent",
    branch: str = "main",
) -> dict[str, Any]:
    status, _, body = http_json(
        "POST",
        f"{base_url}/agents/register",
        body={"agent_id": agent_id, "role": role, "branch": branch, "status": "online"},
        headers=auth_headers(token=token, agent_id=agent_id),
    )
    if status != 200 or not isinstance(body, dict):
        raise AssertionError(f"Register failed ({status}): {body}")
    return body


def _acquire_lease_http(
    base_url: str,
    owner_id: str,
    resource: str,
    *,
    token: str | None = None,
    scope: str = "file",
    reason: str = "e2e test",
) -> dict[str, Any]:
    status, _, body = http_json(
        "POST",
        f"{base_url}/leases/acquire",
        body={"owner_id": owner_id, "resource": resource, "scope": scope, "reason": reason},
        headers=auth_headers(token=token, agent_id=owner_id),
    )
    if status != 200 or not isinstance(body, dict):
        raise AssertionError(f"Acquire lease failed ({status}): {body}")
    return body


class TestF02SchemaV2(unittest.TestCase):
    """F2: SQLite Schema v2 (`events`, `invites`, `agent_tokens`, v1->v2 migration)."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f2_fresh_db_schema_v2_tables_and_index(self) -> None:
        db_path = self.ws.db_dir / "fresh.db"
        hub = self.ws.start_hub(db_path=db_path)
        status, _, health = http_json("GET", f"{hub.base_url}/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        hub.stop()

        schema = inspect_sqlite_schema(db_path)
        self.assertEqual(schema["user_version"], 2)
        for table in ("events", "invites", "agent_tokens"):
            self.assertIn(table, schema["tables"], f"Missing v2 table {table}")
        events_idx_sql = " ".join(
            idx["sql"].lower()
            for idx in schema["indexes"].values()
            if idx["table"] == "events"
        )
        self.assertIn("created_at", events_idx_sql)

    def test_f2_v1_to_v2_migration_preserves_existing_data(self) -> None:
        db_path = self.ws.db_dir / "v1_upgrade.db"
        create_v1_sqlite_db(db_path, seed_sample_data=True)
        pre = inspect_sqlite_schema(db_path)
        self.assertEqual(pre["user_version"], 1)

        hub = self.ws.start_hub(db_path=db_path)
        status, _, snap = http_json("GET", f"{hub.base_url}/snapshot")
        self.assertEqual(status, 200)
        self.assertIn("legacy-agent", snap["online_agents"])
        self.assertTrue(any(a["agent_id"] == "legacy-agent" for a in snap["agents"]))
        self.assertTrue(any(t["task_id"] == "T-0001" for t in snap["tasks"]))
        self.assertTrue(any(l["resource_norm"] == "src/app.py" for l in snap["active_leases"]))
        status_ctx, _, ctx = http_json("GET", f"{hub.base_url}/context/v1_key")
        self.assertEqual(status_ctx, 200)
        self.assertEqual(ctx["value"], {"migrated": True})
        hub.stop()

        post = inspect_sqlite_schema(db_path)
        self.assertEqual(post["user_version"], 2)
        for table in ("events", "invites", "agent_tokens"):
            self.assertIn(table, post["tables"])

    def test_f2_events_table_schema_and_payload_json(self) -> None:
        db_path = self.ws.db_dir / "events_cols.db"
        hub = self.ws.start_hub(db_path=db_path)
        _register_agent_http(hub.base_url, "schema-agent")
        hub.stop()

        schema = inspect_sqlite_schema(db_path)
        cols = {c["name"] for c in schema["tables"]["events"]}
        expected = {
            "id",
            "boot_id",
            "event_type",
            "actor_id",
            "target_id",
            "summary",
            "payload_json",
            "created_at",
        }
        self.assertTrue(expected.issubset(cols), f"Missing event columns: {expected - cols}")

        rows = inspect_sqlite(db_path, "SELECT * FROM events ORDER BY id ASC;")
        self.assertGreaterEqual(len(rows), 1)
        parsed_payload = json.loads(rows[0]["payload_json"])
        self.assertIsInstance(parsed_payload, dict)

    def test_f2_invites_table_persistence_lifecycle(self) -> None:
        db_path = self.ws.db_dir / "invites_persist.db"
        hub = self.ws.start_hub(db_path=db_path, token="hub-admin-secret")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "peer-db", "ttl_sec": 300, "role": "agent"},
            headers=auth_headers(token="hub-admin-secret"),
        )
        self.assertIn(st, (200, 201), f"POST /invites failed: {st} {inv}")
        st_j, _, joined = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertEqual(st_j, 200, f"POST /invites/join failed: {st_j} {joined}")
        hub.stop()

        rows = inspect_sqlite(
            db_path, "SELECT * FROM invites WHERE nonce = ?;", (inv["nonce"],)
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["agent_id"], "peer-db")
        self.assertEqual(row["used"], 1)
        self.assertGreater(float(row["used_at"]), 0.0)
        self.assertEqual(row["used_by_agent"], "peer-db")

    def test_f2_agent_tokens_stores_sha256_hash_not_plaintext(self) -> None:
        db_path = self.ws.db_dir / "tokens_hash.db"
        hub = self.ws.start_hub(db_path=db_path, token="hub-admin-secret")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "hashed-peer"},
            headers=auth_headers(token="hub-admin-secret"),
        )
        self.assertIn(st, (200, 201), f"POST /invites failed: {st} {inv}")
        st_j, _, joined = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertEqual(st_j, 200)
        issued_token = joined["token"]
        hub.stop()

        rows = inspect_sqlite(
            db_path, "SELECT * FROM agent_tokens WHERE agent_id = ?;", ("hashed-peer",)
        )
        self.assertEqual(len(rows), 1)
        expected_hash = hashlib.sha256(issued_token.encode("utf-8")).hexdigest()
        self.assertEqual(rows[0]["token_hash"], expected_hash)
        self.assertNotEqual(rows[0]["token_hash"], issued_token)
        self.assertEqual(rows[0]["revoked"], 0)

    def test_f2_restart_on_v2_db_is_idempotent(self) -> None:
        db_path = self.ws.db_dir / "v2_restart.db"
        hub = self.ws.start_hub(db_path=db_path, token="hub-admin-secret")
        _register_agent_http(hub.base_url, "persist-agent", token="hub-admin-secret")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "invited-peer"},
            headers=auth_headers(token="hub-admin-secret"),
        )
        self.assertIn(st, (200, 201))

        hub.restart()
        st_h, _, health = http_json("GET", f"{hub.base_url}/health")
        self.assertEqual(st_h, 200)
        self.assertEqual(health["status"], "ok")
        st_s, _, snap = http_json(
            "GET", f"{hub.base_url}/snapshot", headers=auth_headers(token="hub-admin-secret")
        )
        self.assertEqual(st_s, 200)
        self.assertIn("persist-agent", snap["online_agents"])
        self.assertTrue(any(a["agent_id"] == "persist-agent" for a in snap["agents"]))
        hub.stop()

        schema = inspect_sqlite_schema(db_path)
        self.assertEqual(schema["user_version"], 2)
        rows = inspect_sqlite(db_path, "SELECT nonce FROM invites WHERE nonce = ?;", (inv["nonce"],))
        self.assertEqual(len(rows), 1)


class TestF03EventsRingBufferAndBootId(unittest.TestCase):
    """F3: `events` ring buffer (1000-row cap) & per-process `boot_id`."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f3_boot_id_consistent_within_single_process(self) -> None:
        hub = self.ws.start_hub()
        st_h, _, health = http_json("GET", f"{hub.base_url}/health")
        st_s, _, snap = http_json("GET", f"{hub.base_url}/snapshot")
        st_e, _, ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        self.assertEqual((st_h, st_s, st_e), (200, 200, 200))
        self.assertTrue(isinstance(health.get("boot_id"), str) and len(health["boot_id"]) > 0)
        self.assertEqual(health["boot_id"], snap.get("boot_id"))
        self.assertEqual(health["boot_id"], ev.get("boot_id"))

    def test_f3_boot_id_changes_across_server_restart(self) -> None:
        hub = self.ws.start_hub()
        _, _, h1 = http_json("GET", f"{hub.base_url}/health")
        boot_1 = h1["boot_id"]
        _register_agent_http(hub.base_url, "boot-agent")

        hub.restart()
        _, _, h2 = http_json("GET", f"{hub.base_url}/health")
        boot_2 = h2["boot_id"]
        self.assertNotEqual(boot_1, boot_2)
        hub.stop()

        rows = inspect_sqlite(hub.db_path, "SELECT boot_id FROM events ORDER BY id ASC;")
        self.assertTrue(any(r["boot_id"] == boot_1 for r in rows))

    def test_f3_mutations_emit_structured_events(self) -> None:
        hub = self.ws.start_hub()
        _register_agent_http(hub.base_url, "actor-1")
        st_m, _, _ = http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "actor-1", "recipient_id": "*", "body": "ping"},
        )
        self.assertEqual(st_m, 201)
        st_t, _, task = http_json(
            "POST",
            f"{hub.base_url}/tasks",
            body={"title": "E2E Task", "created_by": "actor-1"},
        )
        self.assertEqual(st_t, 201)
        tid = task["task_id"]
        http_json("POST", f"{hub.base_url}/tasks/{tid}/claim", body={"agent_id": "actor-1"})
        http_json(
            "PATCH",
            f"{hub.base_url}/tasks/{tid}",
            body={"agent_id": "actor-1", "status": "done", "result_summary": "ok"},
        )
        _acquire_lease_http(hub.base_url, "actor-1", "src/a.py")
        http_json(
            "POST",
            f"{hub.base_url}/leases/release",
            body={"owner_id": "actor-1", "resource": "src/a.py"},
        )
        http_json(
            "PUT",
            f"{hub.base_url}/context/build_status",
            body={"value": "green", "updated_by": "actor-1"},
        )

        events = drain_http_events(hub.base_url, since_id=0)
        self.assertGreaterEqual(len(events), 8)
        for ev in events:
            self.assertTrue(ev.get("event_type"))
            self.assertTrue(ev.get("actor_id"))
            self.assertTrue(ev.get("summary"))
            self.assertGreater(float(ev.get("created_at", 0)), 0.0)

    def test_f3_ring_buffer_caps_at_1000_rows(self) -> None:
        hub = self.ws.start_hub()
        statuses = send_persistent_put_batch("127.0.0.1", hub.port, 1050, key_prefix="cap_")
        self.assertTrue(all(s == 200 for s in statuses), "Expected all 1050 PUTs to return 200")
        hub.stop()

        counts = inspect_sqlite(hub.db_path, "SELECT COUNT(*) AS cnt, MIN(id) AS min_id, MAX(id) AS max_id FROM events;")
        self.assertEqual(counts[0]["cnt"], 1000)
        self.assertGreaterEqual(counts[0]["max_id"], 1050)
        self.assertGreater(counts[0]["min_id"], 50)

    def test_f3_latest_id_monotonic_after_ring_trim(self) -> None:
        hub = self.ws.start_hub()
        statuses = send_persistent_put_batch("127.0.0.1", hub.port, 1050, key_prefix="mono_")
        self.assertTrue(all(s == 200 for s in statuses))

        st, _, ev_all = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0&limit=1")
        self.assertEqual(st, 200)
        latest_id = int(ev_all["latest_id"])
        self.assertGreaterEqual(latest_id, 1050)

        st2, _, tail = http_json(
            "GET", f"{hub.base_url}/events?since_id={latest_id - 5}&wait=0&limit=50"
        )
        self.assertEqual(st2, 200)
        self.assertEqual(len(tail["events"]), 5)
        ids = [int(e["id"]) for e in tail["events"]]
        self.assertEqual(ids, list(range(latest_id - 4, latest_id + 1)))


class TestF04SyncWaitAndCondition(unittest.TestCase):
    """F4: `sync_wait` MCP tool & `Condition` signaling."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f4_mcp_tools_list_advertises_sync_wait(self) -> None:
        hub = self.ws.start_hub()
        mcp_initialize(hub.base_url)
        tools = mcp_list_tools(hub.base_url)
        by_name = {t["name"]: t for t in tools}
        self.assertIn("sync_wait", by_name)
        props = by_name["sync_wait"]["inputSchema"].get("properties", {})
        for param in ("since_id", "boot_id", "timeout_sec", "event_types", "agent_id"):
            self.assertIn(param, props, f"sync_wait schema missing param {param}")

    def test_f4_sync_wait_returns_immediately_when_events_exist(self) -> None:
        hub = self.ws.start_hub()
        http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "a1", "recipient_id": "*", "body": "already here"},
        )
        t0 = time.monotonic()
        st, rpc = mcp_call(
            hub.base_url,
            "sync_wait",
            {"since_id": 0, "timeout_sec": 5},
            timeout=10.0,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(st, 200)
        self.assertLess(elapsed, 2.0)
        result = rpc["result"]
        self.assertFalse(result["isError"])
        sc = result["structuredContent"]
        self.assertFalse(sc["timed_out"])
        self.assertGreaterEqual(len(sc["events"]), 1)

    def test_f4_sync_wait_wakes_on_concurrent_mutation(self) -> None:
        hub = self.ws.start_hub()
        _, _, init_ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init_ev["latest_id"])

        holder: dict[str, Any] = {}

        def _waiter() -> None:
            t0 = time.monotonic()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {"since_id": latest, "timeout_sec": 10},
                timeout=20.0,
            )
            holder["elapsed"] = time.monotonic() - t0
            holder["status"] = st
            holder["rpc"] = rpc

        th = threading.Thread(target=_waiter, daemon=True)
        th.start()
        time.sleep(0.3)
        http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "sender", "recipient_id": "*", "body": "wake up"},
        )
        th.join(timeout=6.0)
        self.assertFalse(th.is_alive(), "sync_wait thread did not wake promptly")
        self.assertEqual(holder["status"], 200)
        self.assertLess(holder["elapsed"], 3.0)
        sc = holder["rpc"]["result"]["structuredContent"]
        self.assertFalse(sc["timed_out"])
        self.assertGreaterEqual(len(sc["events"]), 1)

    def test_f4_sync_wait_times_out_cleanly(self) -> None:
        hub = self.ws.start_hub()
        _, _, init_ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init_ev["latest_id"])

        t0 = time.monotonic()
        st, rpc = mcp_call(
            hub.base_url,
            "sync_wait",
            {"since_id": latest, "timeout_sec": 0.5},
            timeout=10.0,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(st, 200)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 3.0)
        sc = rpc["result"]["structuredContent"]
        self.assertTrue(sc["timed_out"])
        self.assertFalse(sc["reset"])
        self.assertEqual(sc["events"], [])

    def test_f4_sync_wait_boot_id_mismatch_triggers_reset(self) -> None:
        hub = self.ws.start_hub()
        _, _, health = http_json("GET", f"{hub.base_url}/health")

        t0 = time.monotonic()
        st, rpc = mcp_call(
            hub.base_url,
            "sync_wait",
            {"since_id": 999999, "boot_id": "stale-boot-id", "timeout_sec": 5},
            timeout=10.0,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(st, 200)
        self.assertLess(elapsed, 2.0)
        sc = rpc["result"]["structuredContent"]
        self.assertTrue(sc["reset"])
        self.assertFalse(sc["timed_out"])
        self.assertEqual(sc["boot_id"], health["boot_id"])

    def test_f4_stdio_mcp_bridge_sync_wait_timeout_exceeds_5s_default(self) -> None:
        hub = self.ws.start_hub()
        _, _, init_ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init_ev["latest_id"])

        requests = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "sync_wait",
                            "arguments": {"since_id": latest, "timeout_sec": 6.0},
                        },
                    }
                ),
            ]
        ) + "\n"

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "agsync.mcp_bridge",
                "--server",
                hub.base_url,
                "--agent",
                "bridge-agent",
            ],
            cwd=str(REPO_ROOT),
            env=self.ws.isolated_env(),
            input=requests,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20.0,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        wait_resp = next(r for r in lines if r.get("id") == 2)
        result = wait_resp["result"]
        self.assertFalse(result["isError"], f"mcp_bridge timed out prematurely: {result}")
        self.assertTrue(result["structuredContent"]["timed_out"])


class TestF05GetEventsLongPolling(unittest.TestCase):
    """F5: `GET /events` long-polling & `/health`/`/snapshot` `boot_id`."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f5_health_and_snapshot_include_boot_id_and_latest_event_id(self) -> None:
        hub = self.ws.start_hub()
        _register_agent_http(hub.base_url, "ev-agent")

        st_h, _, health = http_json("GET", f"{hub.base_url}/health")
        st_s, _, snap = http_json("GET", f"{hub.base_url}/snapshot")
        self.assertEqual((st_h, st_s), (200, 200))
        self.assertTrue(health.get("boot_id"))
        self.assertEqual(health["boot_id"], snap.get("boot_id"))
        self.assertGreaterEqual(int(health["latest_event_id"]), 1)
        self.assertGreaterEqual(int(snap["latest_event_id"]), 1)

    def test_f5_get_events_incremental_since_id_and_limit(self) -> None:
        hub = self.ws.start_hub()
        for i in range(5):
            http_json(
                "PUT",
                f"{hub.base_url}/context/item_{i}",
                body={"value": i, "updated_by": "p1"},
            )

        st1, _, page1 = http_json("GET", f"{hub.base_url}/events?since_id=0&limit=2&wait=0")
        self.assertEqual(st1, 200)
        self.assertEqual(len(page1["events"]), 2)
        second_id = int(page1["events"][1]["id"])

        st2, _, page2 = http_json("GET", f"{hub.base_url}/events?since_id={second_id}&limit=20&wait=0")
        self.assertEqual(st2, 200)
        self.assertEqual(len(page2["events"]), 3)
        self.assertTrue(all(int(e["id"]) > second_id for e in page2["events"]))

    def test_f5_get_events_types_filter(self) -> None:
        hub = self.ws.start_hub()
        http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "a1", "recipient_id": "*", "body": "hello"},
        )
        _acquire_lease_http(hub.base_url, "a1", "src/filtered.py")

        st_all, _, ev_all = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        self.assertEqual(st_all, 200)
        lease_types = sorted({e["event_type"] for e in ev_all["events"] if "lease" in e["event_type"].lower()})
        self.assertGreaterEqual(len(lease_types), 1, f"Expected a lease event_type in {ev_all['events']}")

        target_type = lease_types[0]
        qs = urllib.parse.urlencode({"since_id": "0", "wait": "0", "types": target_type})
        st_f, _, ev_f = http_json("GET", f"{hub.base_url}/events?{qs}")
        self.assertEqual(st_f, 200)
        self.assertGreaterEqual(len(ev_f["events"]), 1)
        self.assertTrue(all(e["event_type"] == target_type for e in ev_f["events"]))

    def test_f5_get_events_long_poll_wakes_immediately(self) -> None:
        hub = self.ws.start_hub()
        _, _, init_ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init_ev["latest_id"])
        holder: dict[str, Any] = {}

        def _poll() -> None:
            t0 = time.monotonic()
            st, _, body = http_json(
                "GET",
                f"{hub.base_url}/events?since_id={latest}&wait=10",
                timeout=20.0,
            )
            holder["elapsed"] = time.monotonic() - t0
            holder["status"] = st
            holder["body"] = body

        th = threading.Thread(target=_poll, daemon=True)
        th.start()
        time.sleep(0.3)
        _acquire_lease_http(hub.base_url, "a1", "src/wake.py")
        th.join(timeout=6.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(holder["status"], 200)
        self.assertLess(holder["elapsed"], 3.0)
        self.assertGreaterEqual(len(holder["body"]["events"]), 1)

    def test_f5_get_events_long_poll_timeout_empty(self) -> None:
        hub = self.ws.start_hub()
        _, _, init_ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init_ev["latest_id"])

        t0 = time.monotonic()
        st, _, body = http_json(
            "GET", f"{hub.base_url}/events?since_id={latest}&wait=0.5", timeout=10.0
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(st, 200)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(body["events"], [])
        self.assertFalse(body["reset"])
        self.assertEqual(int(body["latest_id"]), latest)

    def test_f5_get_events_detects_server_restart_via_boot_id(self) -> None:
        hub = self.ws.start_hub()
        _register_agent_http(hub.base_url, "r1")
        _, _, ev1 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        boot_1 = ev1["boot_id"]
        latest_1 = int(ev1["latest_id"])

        hub.restart()
        st, _, ev2 = http_json(
            "GET",
            f"{hub.base_url}/events?since_id={latest_1}&boot_id={boot_1}&wait=0",
        )
        self.assertEqual(st, 200)
        self.assertTrue(ev2["reset"])
        self.assertNotEqual(ev2["boot_id"], boot_1)


class TestF06UpgradedDashboardUI(unittest.TestCase):
    """F6: Upgraded `/dashboard` UI (long-poll, Live Activity Feed, Notifications, UI/UX Pro Max)."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def _fetch_dashboard(self, path: str = "/dashboard") -> tuple[int, dict[str, str], str]:
        hub = self.ws.start_hub()
        st, hdrs, raw = http_request("GET", f"{hub.base_url}{path}")
        return st, hdrs, raw.decode("utf-8", errors="replace")

    def test_f6_dashboard_http_headers_and_routes(self) -> None:
        hub = self.ws.start_hub()
        for route in ("/", "/dashboard"):
            st, hdrs, raw = http_request("GET", f"{hub.base_url}{route}")
            self.assertEqual(st, 200)
            self.assertIn("text/html", hdrs.get("content-type", ""))
            self.assertEqual(hdrs.get("x-content-type-options", "").lower(), "nosniff")
            self.assertEqual(hdrs.get("cache-control", "").lower(), "no-store")
            self.assertIn("<html", raw.decode("utf-8", errors="replace").lower())

    def test_f6_dashboard_long_polling_and_live_activity_feed(self) -> None:
        st, _, html = self._fetch_dashboard("/dashboard")
        self.assertEqual(st, 200)
        self.assertIn("/events", html)
        self.assertIn("since_id", html)
        self.assertIn("boot_id", html)
        self.assertRegex(html.lower(), r"live activity|activity feed")

    def test_f6_dashboard_browser_notification_opt_in(self) -> None:
        st, _, html = self._fetch_dashboard("/dashboard")
        self.assertEqual(st, 200)
        self.assertIn("Notification", html)
        self.assertIn("requestPermission", html)

    def test_f6_dashboard_uiux_pro_max_accessibility_and_dark_mode(self) -> None:
        st, _, html = self._fetch_dashboard("/dashboard")
        self.assertEqual(st, 200)
        self.assertIn('name="viewport"', html)
        self.assertIn("width=device-width, initial-scale=1", html)
        self.assertIn("--", html)
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn(":focus-visible", html)
        self.assertIn("overflow-x: auto", html)

    def test_f6_dashboard_svg_only_no_structural_emojis(self) -> None:
        st, _, html = self._fetch_dashboard("/dashboard")
        self.assertEqual(st, 200)
        self.assertIn("<svg", html.lower())
        forbidden_emojis = ("🎨", "⚙️", "🚀", "🔔", "🔒", "✅", "❌", "🟢", "🔴")
        for emoji in forbidden_emojis:
            self.assertNotIn(emoji, html, f"Forbidden structural UI emoji found: {emoji}")


class TestF07InviteAndJoinOnboarding(unittest.TestCase):
    """F7: Invite/Join onboarding (`agsync invite`/`join`, HMAC, single-use nonce)."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f7_http_create_and_list_invites(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "peer-1", "ttl_sec": 600, "role": "agent"},
            headers=auth_headers(token="hub-admin"),
        )
        self.assertIn(st, (200, 201), f"POST /invites failed: {st} {inv}")
        for key in ("nonce", "token", "join_url", "expires_at", "agent_id"):
            self.assertIn(key, inv)
        self.assertEqual(inv["agent_id"], "peer-1")

        st_l, _, listed = http_json(
            "GET", f"{hub.base_url}/invites", headers=auth_headers(token="hub-admin")
        )
        self.assertEqual(st_l, 200)
        items = listed.get("invites", listed) if isinstance(listed, dict) else listed
        self.assertTrue(any(i["nonce"] == inv["nonce"] for i in items))

    def test_f7_http_join_issues_working_agent_token(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "peer-http"},
            headers=auth_headers(token="hub-admin"),
        )
        self.assertIn(st, (200, 201))

        st_j, _, joined = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertEqual(st_j, 200, f"POST /invites/join failed: {st_j} {joined}")
        for key in ("server_url", "agent_id", "token", "mcp_url"):
            self.assertIn(key, joined)
        self.assertEqual(joined["agent_id"], "peer-http")

        st_s, _, _ = http_json(
            "GET",
            f"{hub.base_url}/snapshot",
            headers=auth_headers(token=joined["token"], agent_id="peer-http"),
        )
        self.assertEqual(st_s, 200)

    def test_f7_cli_invite_outputs_json_and_join_url(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        res = self.ws.run_cli(
            "invite",
            "--server",
            hub.base_url,
            "--token",
            "hub-admin",
            "--agent",
            "cli-peer",
            "--json",
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        data = json.loads(res.stdout)
        self.assertEqual(data["agent_id"], "cli-peer")
        self.assertTrue(data.get("token"))
        self.assertTrue(data.get("join_url"))

    def test_f7_cli_join_writes_repo_config_and_authenticates(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        repo = self.ws.create_git_repo("join_repo")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "repo-peer"},
            headers=auth_headers(token="hub-admin"),
        )
        self.assertIn(st, (200, 201))

        res = self.ws.run_cli(
            "join",
            inv["token"],
            "--server",
            hub.base_url,
            "--repo",
            str(repo),
            "--json",
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        self.assertTrue((repo / ".agsync" / "token").is_file())
        self.assertTrue((repo / ".agsync" / "server_url").is_file())
        has_agent_file = (repo / ".agsync-agent").is_file() or (repo / ".agsync" / "agent_id").is_file()
        self.assertTrue(has_agent_file)

        st_res = self.ws.run_cli("status", "--json", cwd=repo)
        self.assertEqual(st_res.returncode, 0, st_res.stderr or st_res.stdout)
        snap = json.loads(st_res.stdout)
        self.assertIn("online_agents", snap)

    def test_f7_invite_uses_configured_base_url(self) -> None:
        hub = self.ws.start_hub(token="hub-admin", base_url="https://hub.example.com")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "remote-peer"},
            headers=auth_headers(token="hub-admin"),
        )
        self.assertIn(st, (200, 201))
        self.assertTrue(
            str(inv["join_url"]).startswith("https://hub.example.com"),
            f"Unexpected join_url: {inv['join_url']}",
        )

    def test_f7_get_invites_reflects_consumed_status(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        st, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "once-peer"},
            headers=auth_headers(token="hub-admin"),
        )
        self.assertIn(st, (200, 201))
        st_j, _, _ = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertEqual(st_j, 200)

        st_l, _, listed = http_json(
            "GET", f"{hub.base_url}/invites", headers=auth_headers(token="hub-admin")
        )
        self.assertEqual(st_l, 200)
        items = listed.get("invites", listed) if isinstance(listed, dict) else listed
        match = next(i for i in items if i["nonce"] == inv["nonce"])
        self.assertTrue(bool(match["used"]))
        self.assertEqual(match["used_by_agent"], "once-peer")
        self.assertNotIn("secret", json.dumps(match).lower())


class TestF08AgentTokensAndScoping(unittest.TestCase):
    """F8: Per-`agent_id` Bearer tokens & scoping (`403` admin/impersonation)."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def _onboard(self, hub_url: str, admin_tok: str, agent_id: str) -> str:
        st, _, inv = http_json(
            "POST",
            f"{hub_url}/invites",
            body={"agent_id": agent_id},
            headers=auth_headers(token=admin_tok),
        )
        self.assertIn(st, (200, 201), f"Invite failed: {st} {inv}")
        st_j, _, joined = http_json(
            "POST",
            f"{hub_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertEqual(st_j, 200, f"Join failed: {st_j} {joined}")
        return str(joined["token"])

    def test_f8_agent_token_can_execute_own_coordination_workflow(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        tok = self._onboard(hub.base_url, "hub-admin", "worker-a")
        hdrs = auth_headers(token=tok, agent_id="worker-a")

        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/agents/register",
                body={"agent_id": "worker-a", "role": "worker"},
                headers=hdrs,
            )[0],
            200,
        )
        self.assertEqual(
            http_json("POST", f"{hub.base_url}/agents/worker-a/heartbeat", body={}, headers=hdrs)[0],
            200,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/messages",
                body={"sender_id": "worker-a", "recipient_id": "*", "body": "hi"},
                headers=hdrs,
            )[0],
            201,
        )
        self.assertEqual(
            http_json("GET", f"{hub.base_url}/messages?agent_id=worker-a", headers=hdrs)[0],
            200,
        )
        st_t, _, task = http_json(
            "POST",
            f"{hub.base_url}/tasks",
            body={"title": "W-Task", "created_by": "worker-a"},
            headers=hdrs,
        )
        self.assertEqual(st_t, 201)
        tid = task["task_id"]
        self.assertEqual(
            http_json(
                "POST", f"{hub.base_url}/tasks/{tid}/claim", body={"agent_id": "worker-a"}, headers=hdrs
            )[0],
            200,
        )
        self.assertEqual(
            http_json(
                "PATCH",
                f"{hub.base_url}/tasks/{tid}",
                body={"agent_id": "worker-a", "status": "done"},
                headers=hdrs,
            )[0],
            200,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/leases/acquire",
                body={"owner_id": "worker-a", "resource": "src/w.py"},
                headers=hdrs,
            )[0],
            200,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/leases/renew",
                body={"owner_id": "worker-a", "resource": "src/w.py"},
                headers=hdrs,
            )[0],
            200,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/leases/release",
                body={"owner_id": "worker-a", "resource": "src/w.py"},
                headers=hdrs,
            )[0],
            200,
        )
        self.assertEqual(
            http_json(
                "PUT",
                f"{hub.base_url}/context/worker_a_state",
                body={"value": "done", "updated_by": "worker-a"},
                headers=hdrs,
            )[0],
            200,
        )

    def test_f8_hub_admin_token_retains_full_state_and_invite_access(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        hdrs = auth_headers(token="hub-admin")
        st_exp, _, exported = http_json("GET", f"{hub.base_url}/state/export", headers=hdrs)
        self.assertEqual(st_exp, 200)
        st_imp, _, _ = http_json("POST", f"{hub.base_url}/state/import", body=exported, headers=hdrs)
        self.assertEqual(st_imp, 200)
        st_inv, _, _ = http_json(
            "POST", f"{hub.base_url}/invites", body={"agent_id": "adm-peer"}, headers=hdrs
        )
        self.assertIn(st_inv, (200, 201))
        st_list, _, _ = http_json("GET", f"{hub.base_url}/invites", headers=hdrs)
        self.assertEqual(st_list, 200)

    def test_f8_agent_token_allowed_with_matching_mcp_headers_and_query(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        tok = self._onboard(hub.base_url, "hub-admin", "worker-mcp")
        st, rpc = mcp_call(
            hub.base_url,
            "sync_acquire_lease",
            {"resource": "src/mcp_scoped.py"},
            token=tok,
            agent_id="worker-mcp",
            query="agent=worker-mcp",
        )
        self.assertEqual(st, 200)
        self.assertFalse(rpc["result"]["isError"], f"MCP scoped call failed: {rpc}")

    def test_f8_multiple_onboarded_agents_collaborate_with_isolated_tokens(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        tok_a = self._onboard(hub.base_url, "hub-admin", "worker-a")
        tok_b = self._onboard(hub.base_url, "hub-admin", "worker-b")
        self.assertNotEqual(tok_a, tok_b)

        _register_agent_http(hub.base_url, "worker-a", token=tok_a)
        _register_agent_http(hub.base_url, "worker-b", token=tok_b)
        _acquire_lease_http(hub.base_url, "worker-a", "src/a.py", token=tok_a)
        _acquire_lease_http(hub.base_url, "worker-b", "src/b.py", token=tok_b)

        st_msg, _, _ = http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "worker-a", "recipient_id": "worker-b", "body": "take b"},
            headers=auth_headers(token=tok_a, agent_id="worker-a"),
        )
        self.assertEqual(st_msg, 201)
        st_poll, _, inbox = http_json(
            "GET",
            f"{hub.base_url}/messages?agent_id=worker-b&unread=1",
            headers=auth_headers(token=tok_b, agent_id="worker-b"),
        )
        self.assertEqual(st_poll, 200)
        self.assertTrue(any(m["body"] == "take b" for m in inbox["messages"]))

    def test_f8_agent_token_restricted_from_admin_routes_and_impersonation(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        tok_a = self._onboard(hub.base_url, "hub-admin", "worker-a")
        hdrs_a = auth_headers(token=tok_a, agent_id="worker-a")

        self.assertEqual(http_json("GET", f"{hub.base_url}/state/export", headers=hdrs_a)[0], 403)
        self.assertEqual(http_json("GET", f"{hub.base_url}/invites", headers=hdrs_a)[0], 403)
        st_imp, _, _ = http_json(
            "POST",
            f"{hub.base_url}/leases/acquire",
            body={"owner_id": "worker-b", "resource": "src/stolen.py"},
            headers=hdrs_a,
        )
        self.assertEqual(st_imp, 403)


class TestF09TunnelBaseUrlAndProxy(unittest.TestCase):
    """F9: Cloudflare Tunnel (`--protocol http2`), `--base-url`, `--allowed-host`, TLS proxy `Origin`/`Host`, proxy-bypass."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f9_serve_base_url_allows_custom_host_header(self) -> None:
        hub = self.ws.start_hub(base_url="https://my-hub.trycloudflare.com")
        st, _, body = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={"Host": "my-hub.trycloudflare.com"},
        )
        self.assertEqual(st, 200, body)

    def test_f9_serve_allowed_host_allows_custom_host_header(self) -> None:
        hub = self.ws.start_hub(allowed_hosts=["tunnel.internal"])
        st, _, body = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={"Host": "tunnel.internal"},
        )
        self.assertEqual(st, 200, body)

    def test_f9_tls_reverse_proxy_origin_443_with_portless_host_allowed(self) -> None:
        hub = self.ws.start_hub(base_url="https://my-hub.trycloudflare.com")
        st, _, body = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={
                "Host": "my-hub.trycloudflare.com",
                "Origin": "https://my-hub.trycloudflare.com",
            },
        )
        self.assertEqual(st, 200, body)

    def test_f9_http_reverse_proxy_origin_80_with_portless_host_allowed(self) -> None:
        hub = self.ws.start_hub(base_url="http://my-hub.example.com")
        st, _, body = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={
                "Host": "my-hub.example.com",
                "Origin": "http://my-hub.example.com",
            },
        )
        self.assertEqual(st, 200, body)

    def test_f9_cloudflared_command_builder_uses_http2_protocol(self) -> None:
        from agsync.adapters.tunnel import build_cloudflared_command

        cmd = build_cloudflared_command("http://127.0.0.1:8765")
        self.assertEqual(
            cmd,
            ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8765", "--protocol", "http2"],
        )

    def test_f9_proxy_bypass_local_vs_remote_and_doctor_https_port(self) -> None:
        from agsync.adapters.network import build_opener_for_url

        hub = self.ws.start_hub()
        old_proxy = os.environ.get("HTTP_PROXY")
        old_https_proxy = os.environ.get("HTTPS_PROXY")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
        try:
            local_opener = build_opener_for_url(hub.base_url)
            st, _, _ = http_json("GET", f"{hub.base_url}/health", opener=local_opener)
            self.assertEqual(st, 200)

            remote_opener = build_opener_for_url("https://my-hub.trycloudflare.com")
            proxy_handlers = [
                h for h in remote_opener.handlers if isinstance(h, urllib.request.ProxyHandler)
            ]
            # Remote opener must not force an empty bypass ProxyHandler({})
            self.assertTrue(
                not proxy_handlers or any(h.proxies for h in proxy_handlers),
                "Remote URL opener must preserve system proxy settings rather than bypassing all proxies",
            )
        finally:
            if old_proxy is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old_proxy
            if old_https_proxy is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = old_https_proxy

        repo = self.ws.create_git_repo("doc_remote")
        # Pass an explicit portless https:// URL on localhost to verify doctor does not default to 8765
        res = self.ws.run_cli(
            "doctor",
            "--repo",
            str(repo),
            "--server",
            "https://127.0.0.1",
            "--json",
        )
        report = json.loads(res.stdout)
        tcp_check = next(c for c in report["checks"] if c["name"] == "hub_tcp")
        self.assertNotIn(":8765", tcp_check["detail"])
        self.assertIn(":443", tcp_check["detail"])


class TestF10MultiIdeHookProxy(unittest.TestCase):
    """F10: `agsync hook-proxy --host <antigravity|claude|cursor>` & Cursor `.claude/` dedup."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f10_antigravity_allow_and_deny_stdout_json(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("ag_repo")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")

        deny_payload = json.dumps(
            {
                "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "locked.py")}},
                "cwd": str(repo),
            }
        )
        res_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=deny_payload,
            cwd=repo,
        )
        self.assertEqual(res_deny.returncode, 0, res_deny.stderr)
        out_deny = json.loads(res_deny.stdout)
        self.assertEqual(out_deny["decision"], "deny")
        self.assertIn("agent-b", out_deny.get("reason", ""))

        allow_payload = json.dumps(
            {
                "toolCall": {"name": "read_file", "args": {"Path": str(repo / "src" / "locked.py")}},
                "cwd": str(repo),
            }
        )
        res_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=allow_payload,
            cwd=repo,
        )
        self.assertEqual(res_allow.returncode, 0, res_allow.stderr)
        out_allow = json.loads(res_allow.stdout)
        self.assertEqual(out_allow["decision"], "allow")

    def test_f10_antigravity_ask_or_require_lease_mode(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("ag_req_lease")
        payload = json.dumps(
            {
                "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}},
                "cwd": str(repo),
            }
        )
        res = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--require-lease",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=payload,
            cwd=repo,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertIn(out["decision"], ("deny", "ask"))

    def test_f10_claude_snake_case_allow_and_deny_exit2_stderr(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("claude_repo")
        _acquire_lease_http(hub.base_url, "agent-b", "src/api.py")

        deny_payload = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "api.py")},
                "cwd": str(repo),
            }
        )
        res_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=deny_payload,
            cwd=repo,
        )
        self.assertEqual(res_deny.returncode, 2, f"Expected exit 2 on Claude deny: {res_deny.stdout} {res_deny.stderr}")
        self.assertIn("agent-b", res_deny.stderr)

        allow_payload = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
            }
        )
        res_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=allow_payload,
            cwd=repo,
        )
        self.assertEqual(res_allow.returncode, 0, res_allow.stderr)

    def test_f10_claude_own_lease_allows_exit0(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("claude_own")
        _acquire_lease_http(hub.base_url, "agent-a", "src/mine.py")

        payload = json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(repo / "src" / "mine.py")},
                "cwd": str(repo),
            }
        )
        res = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--require-lease",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=payload,
            cwd=repo,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_f10_cursor_pretooluse_allow_and_deny(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("cursor_repo")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")

        deny_payload = json.dumps(
            {
                "hook_event_name": "preToolUse",
                "tool_name": "edit_file",
                "tool_input": {"file_path": str(repo / "src" / "locked.py")},
                "cwd": str(repo),
            }
        )
        res_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=deny_payload,
            cwd=repo,
        )
        self.assertEqual(res_deny.returncode, 0, res_deny.stderr)
        out_deny = json.loads(res_deny.stdout)
        self.assertEqual(out_deny.get("decision") or out_deny.get("permission"), "deny")

        allow_payload = json.dumps(
            {
                "hook_event_name": "preToolUse",
                "tool_name": "edit_file",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
            }
        )
        res_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=allow_payload,
            cwd=repo,
        )
        self.assertEqual(res_allow.returncode, 0, res_allow.stderr)
        out_allow = json.loads(res_allow.stdout)
        self.assertEqual(out_allow.get("decision") or out_allow.get("permission"), "allow")

    def test_f10_cursor_deduplicates_when_claude_hook_also_fires(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("cursor_dedup")
        marker_log = repo / "chain_runs.txt"
        user_hook_script = repo / "user_chain.py"
        user_hook_script.write_text(
            f"from pathlib import Path\np = Path({str(marker_log)!r})\nwith p.open('a', encoding='utf-8') as f:\n    f.write('ran\\n')\n",
            encoding="utf-8",
        )
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [{"type": "command", "command": f'"{sys.executable}" "{user_hook_script}"'}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        res_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-claude-hook",
            "--with-cursor-hook",
        )
        self.assertEqual(res_inst.returncode, 0, res_inst.stderr or res_inst.stdout)

        cursor_payload = json.dumps(
            {
                "hook_event_name": "preToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
                "cursor_version": "0.50.0",
            }
        )
        claude_payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
                "cursor_version": "0.50.0",
            }
        )
        r1 = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=cursor_payload,
            cwd=repo,
        )
        r2 = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=claude_payload,
            cwd=repo,
        )
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        dedup_dir = repo / ".agsync" / "hook-dedup"
        self.assertTrue(dedup_dir.is_dir() and any(dedup_dir.iterdir()), "Expected .agsync/hook-dedup marker")
        if marker_log.exists():
            runs = [ln for ln in marker_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(runs), 1, f"Chained hook ran {len(runs)} times instead of deduplicating")


class TestF11RecordAndHookChaining(unittest.TestCase):
    """F11: `--record` (`.agsync/hook-samples/`) & safe hook chaining + process-tree kill."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f11_record_writes_sanitized_sample_to_hook_samples(self) -> None:
        hub = self.ws.start_hub(token="super-secret-bearer")
        repo = self.ws.create_git_repo("rec_claude")
        payload = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
                "authorization": "Bearer super-secret-bearer",
            }
        )
        res = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--record",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--token",
            "super-secret-bearer",
            stdin_text=payload,
            cwd=repo,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        samples_dir = repo / ".agsync" / "hook-samples"
        self.assertTrue(samples_dir.is_dir())
        files = list(samples_dir.glob("*.json"))
        self.assertGreaterEqual(len(files), 1)
        raw_text = files[0].read_text(encoding="utf-8")
        self.assertNotIn("super-secret-bearer", raw_text)
        parsed = json.loads(raw_text)
        self.assertIn("decision", parsed)

    def test_f11_record_captures_antigravity_and_cursor_hosts(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("rec_hosts")

        ag_payload = json.dumps(
            {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}
        )
        cur_payload = json.dumps(
            {"hook_event_name": "preToolUse", "tool_name": "edit_file", "tool_input": {"file_path": str(repo / "src" / "free.py")}, "cwd": str(repo)}
        )
        r_ag = self.ws.run_cli(
            "hook-proxy", "--host", "antigravity", "--record", "--server", hub.base_url, "--agent", "a1", stdin_text=ag_payload, cwd=repo
        )
        r_cur = self.ws.run_cli(
            "hook-proxy", "--host", "cursor", "--record", "--server", hub.base_url, "--agent", "a1", stdin_text=cur_payload, cwd=repo
        )
        self.assertEqual((r_ag.returncode, r_cur.returncode), (0, 0))
        files = [p.name for p in (repo / ".agsync" / "hook-samples").glob("*.json")]
        self.assertTrue(any("antigravity" in f for f in files), files)
        self.assertTrue(any("cursor" in f for f in files), files)

    def test_f11_hook_chaining_executes_preserved_user_hook_on_allow(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("chain_allow")
        marker = repo / "preserved_ran.txt"
        hook_script = repo / "my_hook.py"
        hook_script.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ok', encoding='utf-8')\n",
            encoding="utf-8",
        )
        (repo / ".agents").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"command": f'"{sys.executable}" "{hook_script}"', "show_output": False}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        res_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-antigravity-hook",
        )
        self.assertEqual(res_inst.returncode, 0, res_inst.stderr or res_inst.stdout)

        payload = json.dumps(
            {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}
        )
        res = self.ws.run_cli(
            "hook-proxy", "--host", "antigravity", "--server", hub.base_url, "--agent", "agent-a", stdin_text=payload, cwd=repo
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["decision"], "allow")
        self.assertTrue(marker.is_file() and marker.read_text(encoding="utf-8") == "ok")

    def test_f11_hook_chaining_respects_agsync_deny_without_corrupting_decision(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("chain_deny")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")
        hook_script = repo / "allow_hook.py"
        hook_script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"matcher": "*", "hooks": [{"type": "command", "command": f'"{sys.executable}" "{hook_script}"'}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        res_inst = self.ws.run_cli(
            "install-hooks", "--repo", str(repo), "--server", hub.base_url, "--agent", "agent-a", "--with-claude-hook"
        )
        self.assertEqual(res_inst.returncode, 0, res_inst.stderr or res_inst.stdout)

        payload = json.dumps(
            {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "locked.py")}, "cwd": str(repo)}
        )
        res = self.ws.run_cli(
            "hook-proxy", "--host", "claude", "--server", hub.base_url, "--agent", "agent-a", stdin_text=payload, cwd=repo
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("agent-b", res.stderr)

    def test_f11_hook_chaining_timeout_kills_full_process_tree(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("chain_kill")
        pid_file = repo / "spawned_pids.json"
        hang_script = repo / "hang_tree.py"
        hang_script.write_text(
            (
                "import json, os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"Path({str(pid_file)!r}).write_text(json.dumps([os.getpid(), child.pid]), encoding='utf-8')\n"
                "time.sleep(30)\n"
            ),
            encoding="utf-8",
        )
        (repo / ".agents").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "hooks.json").write_text(
            json.dumps(
                {"hooks": {"PreToolUse": [{"command": f'"{sys.executable}" "{hang_script}"', "show_output": False}]}}
            ),
            encoding="utf-8",
        )
        res_inst = self.ws.run_cli(
            "install-hooks", "--repo", str(repo), "--server", hub.base_url, "--agent", "agent-a", "--with-antigravity-hook"
        )
        self.assertEqual(res_inst.returncode, 0, res_inst.stderr or res_inst.stdout)

        payload = json.dumps(
            {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}
        )
        t0 = time.monotonic()
        res = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--chain-timeout",
            "0.5",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=payload,
            cwd=repo,
            timeout=15.0,
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 6.0, f"hook-proxy did not enforce --chain-timeout ({elapsed:.2f}s)")
        self.assertTrue(pid_file.is_file(), "Chained hook never started")
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        self.assertEqual(len(pids), 2)
        time.sleep(0.3)
        for pid in pids:
            self.assertFalse(is_pid_alive(int(pid)), f"Orphan process {pid} survived chain-timeout tree kill")


class TestF12ExpandedSetupAndTemplates(unittest.TestCase):
    """F12: Expanded `setup-mcp`, `install-hooks`, `doctor` & IDE/REST templates."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f12_setup_mcp_antigravity_claude_cursor_atomic_merge(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("setup_mcp_all")

        ag_cfg = self.ws.home_dir / ".gemini" / "config" / "mcp_config.json"
        claude_cfg = repo / ".mcp.json"
        cursor_cfg = self.ws.home_dir / ".cursor" / "mcp.json"
        for p in (ag_cfg, claude_cfg, cursor_cfg):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"mcpServers": {"existing-tool": {"command": "echo"}}}), encoding="utf-8")

        for ide in ("antigravity", "claude", "cursor"):
            res = self.ws.run_cli(
                "setup-mcp",
                "--ide",
                ide,
                "--repo",
                str(repo),
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                cwd=repo,
            )
            self.assertEqual(res.returncode, 0, f"setup-mcp --ide {ide} failed: {res.stderr or res.stdout}")

        for p in (ag_cfg, cursor_cfg):
            parsed = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("existing-tool", parsed["mcpServers"])
            self.assertIn("agsync", parsed["mcpServers"])
            self.assertTrue((p.parent / f"{p.name}.bak").is_file())

    def test_f12_install_hooks_with_claude_and_cursor_hooks(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hooks_all")
        res = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-all-hooks",
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        self.assertTrue((repo / ".git" / "hooks" / "pre-commit").is_file())
        self.assertTrue((repo / ".agents" / "hooks.json").is_file())
        self.assertTrue((repo / ".claude" / "settings.json").is_file())
        self.assertTrue((repo / ".cursor" / "hooks.json").is_file())
        exclude_txt = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(".agsync", exclude_txt)

    def test_f12_installed_claude_and_cursor_hooks_execute_end_to_end(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hooks_exec")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")
        res_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-claude-hook",
            "--with-cursor-hook",
        )
        self.assertEqual(res_inst.returncode, 0, res_inst.stderr or res_inst.stdout)

        claude_cfg = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        claude_cmd = claude_cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        claude_in = json.dumps(
            {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "locked.py")}, "cwd": str(repo)}
        )
        shell_c = ["cmd", "/c", claude_cmd] if os.name == "nt" else ["sh", "-c", claude_cmd]
        p_claude = subprocess.run(
            shell_c, cwd=str(repo), env=self.ws.isolated_env(), input=claude_in, capture_output=True, text=True, timeout=15
        )
        self.assertEqual(p_claude.returncode, 2, f"Installed Claude hook did not deny with exit 2: {p_claude.stderr}")

        cursor_cfg = json.loads((repo / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
        cursor_hooks = cursor_cfg["hooks"].get("preToolUse") or cursor_cfg["hooks"].get("PreToolUse")
        cursor_cmd = cursor_hooks[0]["command"]
        cursor_in = json.dumps(
            {"hook_event_name": "preToolUse", "tool_name": "edit_file", "tool_input": {"file_path": str(repo / "src" / "locked.py")}, "cwd": str(repo)}
        )
        shell_cur = ["cmd", "/c", cursor_cmd] if os.name == "nt" else ["sh", "-c", cursor_cmd]
        p_cur = subprocess.run(
            shell_cur, cwd=str(repo), env=self.ws.isolated_env(), input=cursor_in, capture_output=True, text=True, timeout=15
        )
        self.assertEqual(p_cur.returncode, 0, p_cur.stderr)
        cur_out = json.loads(p_cur.stdout)
        self.assertEqual(cur_out.get("decision") or cur_out.get("permission"), "deny")

    def test_f12_templates_exist_and_validate_for_windsurf_codex_gemini_rest(self) -> None:
        tdir = REPO_ROOT / "templates"
        all_files = [p for p in tdir.rglob("*") if p.is_file()]
        self.assertGreater(len(all_files), 4)

        combined_names_and_text = ""
        for f in all_files:
            text = f.read_text(encoding="utf-8")
            combined_names_and_text += f"\n{f.as_posix()}\n{text}\n"
            if f.suffix.lower() == ".json":
                json.loads(text)
                self.assertNotIn("/sse", text, f"Legacy /sse found in {f}")

        low = combined_names_and_text.lower()
        for target in ("windsurf", "codex", "gemini"):
            self.assertIn(target, low, f"Missing template coverage for {target}")
        for rest_ep in ("/leases/acquire", "/tasks", "/messages", "/events"):
            self.assertIn(rest_ep, combined_names_and_text, f"Missing REST API template reference {rest_ep}")

    def test_f12_doctor_multi_ide_checks_pass_when_configured(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("doc_multi")
        self.ws.run_cli(
            "setup-mcp", "--ide", "all", "--repo", str(repo), "--server", hub.base_url, "--agent", "agent-a", cwd=repo
        )
        self.ws.run_cli(
            "install-hooks", "--repo", str(repo), "--server", hub.base_url, "--agent", "agent-a", "--with-all-hooks"
        )
        res = self.ws.run_cli("doctor", "--repo", str(repo), "--server", hub.base_url, "--json", cwd=repo)
        self.assertEqual(res.returncode, 0, res.stdout or res.stderr)
        report = json.loads(res.stdout)
        self.assertTrue(report["ok"], report)

    def test_f12_rest_api_client_flow_without_mcp(self) -> None:
        hub = self.ws.start_hub()
        self.assertEqual(_register_agent_http(hub.base_url, "rest-bot")["agent_id"], "rest-bot")
        self.assertEqual(_acquire_lease_http(hub.base_url, "rest-bot", "src/rest.py")["owner_id"], "rest-bot")
        st_t, _, task = http_json(
            "POST", f"{hub.base_url}/tasks", body={"title": "REST Task", "created_by": "rest-bot"}
        )
        self.assertEqual(st_t, 201)
        self.assertEqual(
            http_json("POST", f"{hub.base_url}/tasks/{task['task_id']}/claim", body={"agent_id": "rest-bot"})[0], 200
        )
        self.assertEqual(
            http_json("POST", f"{hub.base_url}/messages", body={"sender_id": "rest-bot", "recipient_id": "*", "body": "rest-ok"})[0],
            201,
        )
        self.assertEqual(
            http_json("PUT", f"{hub.base_url}/context/rest_k", body={"value": 42, "updated_by": "rest-bot"})[0], 200
        )
        events = drain_http_events(hub.base_url, since_id=0)
        self.assertGreaterEqual(len(events), 5)


class TestF13GitWorktreeManagement(unittest.TestCase):
    """F13: `agsync worktree create/list/remove`, `.agsync-agent`, `info/exclude`, Windows Junctions."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f13_worktree_create_sibling_path_and_agent_identity(self) -> None:
        proj = self.ws.create_git_repo("proj")
        res = self.ws.run_cli("worktree", "create", "agent-wt1", "--repo", str(proj))
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)

        wt_path = self.ws.ws_dir / "proj-wt-1"
        self.assertTrue(wt_path.is_dir(), f"Expected sibling worktree at {wt_path}")
        self.assertEqual((wt_path / ".agsync-agent").read_text(encoding="utf-8").strip(), "agent-wt1")

        st = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(wt_path), capture_output=True, text=True, check=True
        )
        self.assertNotIn(".agsync-agent", st.stdout)

    def test_f13_worktree_create_sequential_numbering_and_custom_branch(self) -> None:
        proj = self.ws.create_git_repo("proj_seq")
        r1 = self.ws.run_cli("worktree", "create", "agent-a", "--repo", str(proj))
        r2 = self.ws.run_cli(
            "worktree", "create", "agent-b", "--repo", str(proj), "--branch", "feat/custom-b"
        )
        self.assertEqual(r1.returncode, 0, r1.stderr or r1.stdout)
        self.assertEqual(r2.returncode, 0, r2.stderr or r2.stdout)

        self.assertTrue((self.ws.ws_dir / "proj_seq-wt-1").is_dir())
        self.assertTrue((self.ws.ws_dir / "proj_seq-wt-2").is_dir())
        porc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=str(proj), capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("feat/custom-b", porc)

    def test_f13_worktree_list_json_reports_main_and_linked_worktrees(self) -> None:
        proj = self.ws.create_git_repo("proj_list")
        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-l", "--repo", str(proj)).returncode, 0)
        res = self.ws.run_cli("worktree", "list", "--repo", str(proj), "--json")
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        data = json.loads(res.stdout)
        items = data.get("worktrees", data) if isinstance(data, dict) else data
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(any(str(i.get("agent_id", "")) == "agent-l" for i in items))

    def test_f13_worktree_create_links_node_modules_and_venv_junctions(self) -> None:
        proj = self.ws.create_git_repo("proj_junc")
        (proj / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (proj / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
        (proj / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (proj / ".venv" / "Scripts" / "marker.txt").write_text("venv-ok\n", encoding="utf-8")

        res = self.ws.run_cli("worktree", "create", "agent-j", "--repo", str(proj))
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        wt1 = self.ws.ws_dir / "proj_junc-wt-1"
        self.assertTrue((wt1 / "node_modules" / "pkg" / "index.js").is_file())
        self.assertTrue((wt1 / ".venv" / "Scripts" / "marker.txt").is_file())
        if os.name == "nt":
            self.assertTrue(os.path.isjunction(wt1 / "node_modules"))
            self.assertTrue(os.path.isjunction(wt1 / ".venv"))

        res_no = self.ws.run_cli("worktree", "create", "agent-noj", "--repo", str(proj), "--no-junctions")
        self.assertEqual(res_no.returncode, 0, res_no.stderr or res_no.stdout)
        wt2 = self.ws.ws_dir / "proj_junc-wt-2"
        self.assertFalse((wt2 / "node_modules").exists())
        self.assertFalse((wt2 / ".venv").exists())

    def test_f13_worktree_remove_preserves_main_shared_caches(self) -> None:
        proj = self.ws.create_git_repo("proj_rm")
        (proj / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (proj / "node_modules" / "pkg" / "index.js").write_text("keep-me\n", encoding="utf-8")
        (proj / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (proj / ".venv" / "Scripts" / "marker.txt").write_text("keep-venv\n", encoding="utf-8")

        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-rm", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "proj_rm-wt-1"
        self.assertTrue(wt1.is_dir())

        res_rm = self.ws.run_cli("worktree", "remove", "agent-rm", "--repo", str(proj))
        self.assertEqual(res_rm.returncode, 0, res_rm.stderr or res_rm.stdout)
        self.assertFalse(wt1.exists())
        self.assertEqual((proj / "node_modules" / "pkg" / "index.js").read_text(encoding="utf-8"), "keep-me\n")
        self.assertEqual((proj / ".venv" / "Scripts" / "marker.txt").read_text(encoding="utf-8"), "keep-venv\n")

    def test_f13_worktree_create_with_install_hooks_wires_hook_guard(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("proj_hooks")
        res = self.ws.run_cli(
            "worktree",
            "create",
            "agent-h",
            "--repo",
            str(proj),
            "--server",
            hub.base_url,
            "--install-hooks",
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        wt1 = self.ws.ws_dir / "proj_hooks-wt-1"
        self.assertEqual((wt1 / ".agsync-agent").read_text(encoding="utf-8").strip(), "agent-h")
        self.assertTrue((proj / ".git" / "hooks" / "pre-commit").is_file() or (wt1 / ".agents" / "hooks.json").is_file())


class TestF14MultiWorktreeIdentityGuard(unittest.TestCase):
    """F14: Multi-worktree identity guard (refuse ambiguous `socket.gethostname()` fallback)."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_f14_single_repo_without_worktrees_allows_default_identity(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("single_repo")
        res = self.ws.run_cli("check", "src/free.py", "--server", hub.base_url, cwd=repo)
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)

    def test_f14_linked_worktree_without_identity_refuses_hostname_fallback_cli(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("wt_noid")
        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-w", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "wt_noid-wt-1"
        (wt1 / ".agsync-agent").unlink(missing_ok=True)
        (wt1 / ".agsync" / "agent_id").unlink(missing_ok=True)

        res = self.ws.run_cli("lock", "src/app.py", "--server", hub.base_url, cwd=wt1)
        self.assertNotEqual(res.returncode, 0, "Expected non-zero exit when worktree lacks explicit identity")
        combined = (res.stdout + "\n" + res.stderr).lower()
        self.assertRegex(combined, r"\.agsync-agent|identity|ambiguous|agent")

    def test_f14_main_repo_with_multiple_worktrees_without_identity_refuses_fallback(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("main_noid")
        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-w1", "--repo", str(proj)).returncode, 0)
        (proj / ".agsync-agent").unlink(missing_ok=True)
        (proj / ".agsync" / "agent_id").unlink(missing_ok=True)

        res = self.ws.run_cli("lock", "src/app.py", "--server", hub.base_url, cwd=proj)
        self.assertNotEqual(res.returncode, 0, "Expected main repo without .agsync-agent to refuse hostname fallback when >1 worktrees exist")

    def test_f14_explicit_agsync_agent_or_env_or_flag_succeeds_in_multi_worktree(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("wt_explicit")
        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-ok", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "wt_explicit-wt-1"

        r_file = self.ws.run_cli("lock", "src/app.py", "--server", hub.base_url, cwd=wt1)
        self.assertEqual(r_file.returncode, 0, r_file.stderr or r_file.stdout)

        (wt1 / ".agsync-agent").unlink(missing_ok=True)
        r_env = self.ws.run_cli(
            "check", "src/app.py", "--server", hub.base_url, env_overrides={"AGSYNC_AGENT": "agent-env"}, cwd=wt1
        )
        self.assertEqual(r_env.returncode, 0, r_env.stderr or r_env.stdout)

        r_flag = self.ws.run_cli(
            "check", "src/app.py", "--server", hub.base_url, "--agent", "agent-flag", cwd=wt1
        )
        self.assertEqual(r_flag.returncode, 0, r_flag.stderr or r_flag.stdout)

    def test_f14_doctor_flags_missing_worktree_identity(self) -> None:
        proj = self.ws.create_git_repo("wt_doc")
        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-d", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "wt_doc-wt-1"
        (wt1 / ".agsync-agent").unlink(missing_ok=True)

        res_bad = self.ws.run_cli("doctor", "--repo", str(wt1), "--json", cwd=wt1)
        rep_bad = json.loads(res_bad.stdout)
        identity_checks = [
            c for c in rep_bad["checks"]
            if "identity" in c["name"].lower() or "worktree" in c["name"].lower() or ".agsync-agent" in c.get("detail", "")
        ]
        self.assertGreaterEqual(len(identity_checks), 1, rep_bad["checks"])
        self.assertTrue(any(c["status"] in ("warn", "fail") for c in identity_checks), identity_checks)

        (wt1 / ".agsync-agent").write_text("agent-d\n", encoding="utf-8")
        res_ok = self.ws.run_cli("doctor", "--repo", str(wt1), "--json", cwd=wt1)
        rep_ok = json.loads(res_ok.stdout)
        identity_ok = [
            c for c in rep_ok["checks"]
            if "identity" in c["name"].lower() or "worktree" in c["name"].lower()
        ]
        self.assertTrue(all(c["status"] == "ok" for c in identity_ok), identity_ok)


class TestF15ControlPanelPhase2GUI(unittest.TestCase):
    """F15: Control Panel GUI Phase 2 controls (`scripts/control_panel.py`)."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def _run_tk_driver(self, script_body: str, *, timeout: float = 20.0) -> dict[str, Any]:
        """Run control_panel test driver in an isolated-HOME subprocess."""
        driver = self.ws.root / "tk_driver.py"
        driver.write_text(script_body, encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(driver)],
            cwd=str(REPO_ROOT),
            env=self.ws.isolated_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
        if res.returncode == 42:
            self.skipTest(res.stdout.strip() or "Tk display unavailable")
        self.assertEqual(res.returncode, 0, f"GUI driver failed ({res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        return json.loads(res.stdout)

    def test_f15_control_panel_instantiates_with_phase2_controls(self) -> None:
        script = """
import json, sys, tkinter as tk
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {exc}")
    sys.exit(42)
root.withdraw()
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    root.update_idletasks()
    attrs = dir(panel)
    print(json.dumps({
        "attrs": attrs,
        "has_ide": any("ide" in a.lower() for a in attrs),
        "has_worktree": any("worktree" in a.lower() for a in attrs),
        "has_base_url": any("base_url" in a.lower() or "tunnel" in a.lower() for a in attrs),
        "has_invite": any("invite" in a.lower() or "join" in a.lower() for a in attrs),
    }))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertTrue(out["has_ide"], "Missing multi-IDE controls in AgsyncControlPanel")
        self.assertTrue(out["has_worktree"], "Missing worktree controls in AgsyncControlPanel")
        self.assertTrue(out["has_base_url"], "Missing base_url/tunnel controls in AgsyncControlPanel")
        self.assertTrue(out["has_invite"], "Missing invite/join controls in AgsyncControlPanel")

    def test_f15_control_panel_multi_ide_setup_action_isolated(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("gui_ide")
        script = f"""
import json, sys, time, tkinter as tk
from pathlib import Path
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    panel.repo_var.set({str(repo)!r})
    panel.agent_var.set("gui-agent")
    panel.host_ip_var.set("127.0.0.1")
    panel.port_var.set({str(hub.port)!r})
    for attr in ("ide_var", "target_ide_var", "setup_ide_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("claude")
            break
    handler = None
    for name in ("setup_selected_ide", "connect_ide_mcp", "setup_multi_ide", "setup_ide", "connect_mcp"):
        if hasattr(panel, name):
            handler = getattr(panel, name)
            break
    assert handler is not None, "No multi-IDE setup handler found on AgsyncControlPanel"
    handler()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        root.update()
        if (Path({str(repo)!r}) / ".mcp.json").is_file() or (Path.home() / ".gemini" / "config" / "mcp_config.json").is_file():
            break
        time.sleep(0.05)
    print(json.dumps({{
        "repo_mcp": (Path({str(repo)!r}) / ".mcp.json").is_file(),
        "ag_mcp": (Path.home() / ".gemini" / "config" / "mcp_config.json").is_file(),
    }}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertTrue(out["repo_mcp"] or out["ag_mcp"], "Panel IDE setup wrote no isolated MCP config")

    def test_f15_control_panel_worktree_actions_create_list_remove(self) -> None:
        proj = self.ws.create_git_repo("gui_wt")
        wt1 = self.ws.ws_dir / "gui_wt-wt-1"
        script = f"""
import json, sys, time, tkinter as tk
from pathlib import Path
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    panel.repo_var.set({str(proj)!r})
    for attr in ("wt_agent_var", "worktree_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-wt-agent")
    create_fn = next((getattr(panel, n) for n in ("create_worktree", "worktree_create", "on_create_worktree") if hasattr(panel, n)), None)
    list_fn = next((getattr(panel, n) for n in ("list_worktrees", "worktree_list", "refresh_worktrees") if hasattr(panel, n)), None)
    remove_fn = next((getattr(panel, n) for n in ("remove_worktree", "worktree_remove", "on_remove_worktree") if hasattr(panel, n)), None)
    assert create_fn and list_fn and remove_fn, "Missing create/list/remove worktree handlers"
    create_fn()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not Path({str(wt1)!r}).is_dir():
        root.update()
        time.sleep(0.05)
    created = Path({str(wt1)!r}).is_dir()
    list_fn()
    root.update()
    for attr in ("wt_target_var", "worktree_target_var", "wt_agent_var", "worktree_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-wt-agent")
    remove_fn()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and Path({str(wt1)!r}).exists():
        root.update()
        time.sleep(0.05)
    removed = not Path({str(wt1)!r}).exists()
    print(json.dumps({{"created": created, "removed": removed}}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertTrue(out["created"], "GUI worktree create failed")
        self.assertTrue(out["removed"], "GUI worktree remove failed")

    def test_f15_control_panel_invite_and_join_handlers(self) -> None:
        hub = self.ws.start_hub(token="hub-admin")
        repo = self.ws.create_git_repo("gui_join")
        script = f"""
import json, sys, time, tkinter as tk
from pathlib import Path
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    panel.host_ip_var.set("127.0.0.1")
    panel.port_var.set({str(hub.port)!r})
    panel.token_var.set("hub-admin")
    panel.repo_var.set({str(repo)!r})
    for attr in ("invite_agent_var", "peer_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-peer")
    inv_fn = next((getattr(panel, n) for n in ("create_invite", "generate_invite", "on_create_invite") if hasattr(panel, n)), None)
    join_fn = next((getattr(panel, n) for n in ("join_invite", "join_hub", "on_join_invite") if hasattr(panel, n)), None)
    assert inv_fn and join_fn, "Missing invite/join handlers on AgsyncControlPanel"
    inv_fn()
    deadline = time.monotonic() + 6.0
    inv_tok = ""
    while time.monotonic() < deadline:
        root.update()
        for attr in ("invite_link_var", "join_token_var", "invite_token_var", "join_link_var"):
            if hasattr(panel, attr) and getattr(panel, attr).get().strip():
                inv_tok = getattr(panel, attr).get().strip()
                break
        if inv_tok:
            break
        time.sleep(0.05)
    assert inv_tok, "Create invite did not populate invite token/link var"
    join_fn()
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and not (Path({str(repo)!r}) / ".agsync" / "token").is_file():
        root.update()
        time.sleep(0.05)
    print(json.dumps({{
        "got_invite": bool(inv_tok),
        "joined_repo_token": (Path({str(repo)!r}) / ".agsync" / "token").is_file(),
    }}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertTrue(out["got_invite"])
        self.assertTrue(out["joined_repo_token"])

    def test_f15_control_panel_base_url_and_tunnel_config_persistence(self) -> None:
        script = """
import json, sys, tkinter as tk
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {exc}")
    sys.exit(42)
root.withdraw()
try:
    from agsync.config import load_config
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    base_attr = next((a for a in ("base_url_var", "remote_url_var", "public_url_var") if hasattr(panel, a)), None)
    assert base_attr is not None, "Missing base_url StringVar on AgsyncControlPanel"
    getattr(panel, base_attr).set("https://panel-hub.trycloudflare.com")
    panel._save_current_config()
    reloaded = load_config()
    print(json.dumps({"reloaded": reloaded}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        reloaded_str = json.dumps(out["reloaded"])
        self.assertIn("https://panel-hub.trycloudflare.com", reloaded_str)


class TestF16BilingualDocsAndTemplates(unittest.TestCase):
    """F16: Bilingual Docs & Templates sync (`README.md`, `README.fa.md`, `docs/*.md`, `templates/AGENTS.md`)."""

    def test_f16_readme_en_and_fa_document_phase2_commands(self) -> None:
        for fname in ("README.md", "README.fa.md"):
            text = (REPO_ROOT / fname).read_text(encoding="utf-8")
            for kw in ("hook-proxy", "worktree", "invite", "join", "sync_wait", "base-url"):
                self.assertIn(kw, text, f"{fname} missing Phase 2 keyword {kw}")
            self.assertNotIn("Phase 2 (Planned)", text)

    def test_f16_setup_guide_en_and_fa_cover_multi_ide_worktree_remote(self) -> None:
        for fname in ("docs/SETUP_GUIDE.md", "docs/SETUP_GUIDE.fa.md"):
            text = (REPO_ROOT / fname).read_text(encoding="utf-8")
            low = text.lower()
            for kw in ("antigravity", "claude", "cursor", "windsurf", "codex", "gemini", "worktree", "invite", "join"):
                self.assertIn(kw, low, f"{fname} missing setup coverage for {kw}")

    def test_f16_architecture_en_and_fa_reflect_phase2_modules(self) -> None:
        for fname in ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.fa.md"):
            text = (REPO_ROOT / fname).read_text(encoding="utf-8")
            for kw in ("events", "invites", "sync_wait", "hook-proxy", "worktree"):
                self.assertIn(kw, text, f"{fname} missing Phase 2 architecture term {kw}")

    def test_f16_templates_agents_md_and_skills_updated_for_phase2(self) -> None:
        agents_md = (REPO_ROOT / "templates" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("sync_wait", agents_md)
        self.assertIn(".agsync-agent", agents_md)

        skills = list((REPO_ROOT / "templates" / ".agents" / "skills").glob("*/SKILL.md"))
        self.assertGreaterEqual(len(skills), 3)
        for sk in skills:
            txt = sk.read_text(encoding="utf-8")
            self.assertTrue(txt.startswith("---"), f"{sk} missing YAML frontmatter")
            self.assertIn("name:", txt)
            self.assertIn("description:", txt)

    def test_f16_bilingual_doc_links_and_cross_references_resolve(self) -> None:
        md_files = [REPO_ROOT / "README.md", REPO_ROOT / "README.fa.md"] + sorted(
            (REPO_ROOT / "docs").glob("*.md")
        )
        link_re = re.compile(r"\[[^\]]+\]\(([^)#\s]+)(?:#[^)\s]+)?\)")
        for md in md_files:
            text = md.read_text(encoding="utf-8")
            for target in link_re.findall(text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (md.parent / target).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"Broken relative link in {md.relative_to(REPO_ROOT)}: {target}",
                )


if __name__ == "__main__":
    unittest.main()
