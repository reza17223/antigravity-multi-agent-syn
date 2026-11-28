"""Tier 2 Opaque-Box Boundary, Negative, Security & Concurrency E2E Tests (F2-F16).

90 tests total: 15 TestCase classes (TestF02..TestF16), 6 tests per feature.
Zero third-party dependencies (Python 3.12 stdlib + tests_e2e.helpers only).
Zero top-level imports of unimplemented Phase 2 symbols.
Zero mocks/stubs/fakes and zero skipTest on unimplemented Phase 2 features.
"""

from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import os
import re
import socket
import socketserver
import sqlite3
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
    ManagedRepoTestMixin,
    auth_headers,
    create_v1_sqlite_db,
    http_json,
    http_request,
    inspect_sqlite,
    inspect_sqlite_schema,
    is_pid_alive,
    mcp_call,
    mcp_initialize,
    send_persistent_put_batch,
)


# ---------------------------------------------------------------------------
# Module-private E2E infrastructure helpers (100% stdlib, zero mocks)
# ---------------------------------------------------------------------------


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _DummyHttpProxy:
    """Real local HTTP/CONNECT proxy recording targets without external network access."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        parent = self

        class _ProxyHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _record_and_reject(self, method: str) -> None:
                with parent._lock:
                    parent.hits.append((method, self.path))
                body = b"Bad Gateway (local test proxy)"
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_GET(self) -> None:  # noqa: N802
                self._record_and_reject("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._record_and_reject("POST")

            def do_CONNECT(self) -> None:  # noqa: N802
                self._record_and_reject("CONNECT")

        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self.port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def snapshot_hits(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self.hits)

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        self._thread.join(timeout=3.0)


class _SlowTarpitServer:
    """Real local TCP socket accepting connections and holding them open to test non-blocking UI."""

    def __init__(self, hold_sec: float = 3.0) -> None:
        self.hold_sec = hold_sec
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._conns.append(conn)
            t = threading.Thread(target=self._hold_conn, args=(conn,), daemon=True)
            t.start()

    def _hold_conn(self, conn: socket.socket) -> None:
        try:
            self._stop.wait(timeout=self.hold_sec)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        self._thread.join(timeout=3.0)


def _poll_pid_dead(pid: int, timeout: float = 2.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.05)
    return not is_pid_alive(pid)


def _acquire_lease_http(
    base_url: str,
    owner_id: str,
    resource: str,
    *,
    token: str | None = None,
    scope: str = "file",
    reason: str = "tier2 e2e",
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


def _create_and_join_invite(
    base_url: str,
    admin_token: str,
    agent_id: str,
    *,
    ttl_sec: int = 300,
    role: str = "agent",
) -> tuple[dict[str, Any], dict[str, Any]]:
    st_i, _, inv = http_json(
        "POST",
        f"{base_url}/invites",
        body={"agent_id": agent_id, "ttl_sec": ttl_sec, "role": role},
        headers=auth_headers(token=admin_token),
    )
    if st_i not in (200, 201) or not isinstance(inv, dict) or not inv.get("token"):
        raise AssertionError(f"POST /invites failed ({st_i}): {inv}")
    st_j, _, joined = http_json(
        "POST",
        f"{base_url}/invites/join",
        body={"invite_token": inv["token"]},
    )
    if st_j != 200 or not isinstance(joined, dict) or not joined.get("token"):
        raise AssertionError(f"POST /invites/join failed ({st_j}): {joined}")
    return inv, joined


# ---------------------------------------------------------------------------
# Feature F2: SQLite Schema v2 Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF02SchemaV2Boundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F2: SQLite Schema v2 migration idempotency, corrupt/v0 DBs, WAL contention & constraints."""

    def test_f2_repeated_v1_to_v2_reopen_idempotency_and_user_version(self) -> None:
        db_path = self.ws.db_dir / "v1_idempotent.db"
        create_v1_sqlite_db(db_path, seed_sample_data=True)
        self.assertEqual(inspect_sqlite_schema(db_path)["user_version"], 1)

        hub = self.ws.start_hub(db_path=db_path)
        for _ in range(3):
            hub.stop()
            schema = inspect_sqlite_schema(db_path)
            self.assertEqual(schema["user_version"], 2)
            for table in ("events", "invites", "agent_tokens"):
                self.assertIn(table, schema["tables"])
            for table in ("agents", "tasks", "leases", "messages", "context_kv"):
                rows = inspect_sqlite(db_path, f"SELECT COUNT(*) AS cnt FROM {table};")
                self.assertEqual(rows[0]["cnt"], 1, f"Seeded count drifted for {table}")
            hub.start()

        st, _, snap = http_json("GET", f"{hub.base_url}/snapshot")
        self.assertEqual(st, 200)
        self.assertTrue(any(a["agent_id"] == "legacy-agent" for a in snap["agents"]))
        self.assertTrue(any(t["task_id"] == "T-0001" for t in snap["tasks"]))
        self.assertTrue(any(l["resource_norm"] == "src/app.py" for l in snap["active_leases"]))
        hub.stop()

    def test_f2_v0_upgrade_corrupted_file_and_missing_column_handling(self) -> None:
        # (a) Empty v0 SQLite DB upgrades cleanly to Schema v2
        v0_db = self.ws.db_dir / "v0_empty.db"
        conn0 = sqlite3.connect(str(v0_db))
        try:
            conn0.execute("PRAGMA user_version = 0;")
            conn0.commit()
        finally:
            conn0.close()
        hub0 = self.ws.start_hub(db_path=v0_db)
        self.assertEqual(http_json("GET", f"{hub0.base_url}/health")[0], 200)
        hub0.stop()
        schema0 = inspect_sqlite_schema(v0_db)
        self.assertEqual(schema0["user_version"], 2)
        for tbl in ("agents", "messages", "tasks", "leases", "context_kv", "events", "invites", "agent_tokens"):
            self.assertIn(tbl, schema0["tables"])

        # (b) Corrupted non-SQLite file causes `agsync serve` to fail cleanly
        corrupt_db = self.ws.db_dir / "corrupt.db"
        corrupt_db.write_bytes(b"NOT_A_VALID_SQLITE_DATABASE_FILE_HEADER_0000\x00\xff\xfe")
        res_bad = self.ws.run_cli(
            "serve", "--host", "127.0.0.1", "--port", "0", "--db", str(corrupt_db), "--no-discover", timeout=10.0
        )
        self.assertNotEqual(res_bad.returncode, 0, "Expected non-zero exit on corrupt SQLite file")

        # (c) Schema v2 with empty `events` table missing `boot_id` starts cleanly and handles write errors safely
        missing_col_db = self.ws.db_dir / "missing_col.db"
        hub_mc = self.ws.start_hub(db_path=missing_col_db)
        hub_mc.stop()
        conn_mc = sqlite3.connect(str(missing_col_db))
        try:
            conn_mc.executescript(
                """
                DROP TABLE events;
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    target_id TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                PRAGMA user_version = 2;
                """
            )
            conn_mc.commit()
        finally:
            conn_mc.close()

        hub_mc.start()
        self.assertEqual(http_json("GET", f"{hub_mc.base_url}/health")[0], 200)
        st_m, _, msg_err = http_json(
            "POST",
            f"{hub_mc.base_url}/messages",
            body={"sender_id": "a1", "recipient_id": "*", "body": "should trigger schema error"},
        )
        self.assertEqual(st_m, 500, f"Expected 500 on missing boot_id column, got {st_m} {msg_err}")
        self.assertIsInstance(msg_err, dict)
        self.assertEqual(msg_err.get("error"), "internal_error")
        self.assertEqual(http_json("GET", f"{hub_mc.base_url}/health")[0], 200)
        hub_mc.stop()

    def test_f2_wal_busy_timeout_contention_under_concurrent_writers(self) -> None:
        db_path = self.ws.db_dir / "wal_contention.db"
        hub = self.ws.start_hub(db_path=db_path)

        conn_check = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            jmode = str(conn_check.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
            self.assertEqual(jmode, "wal")
        finally:
            conn_check.close()

        lock_acquired = threading.Event()
        holder_errors: list[BaseException] = []

        def _hold_write_lock() -> None:
            conn = sqlite3.connect(str(db_path), timeout=15.0, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE;")
                lock_acquired.set()
                time.sleep(0.6)
                conn.execute("COMMIT;")
            except BaseException as exc:  # noqa: BLE001
                holder_errors.append(exc)
            finally:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                conn.close()

        lock_thread = threading.Thread(target=_hold_write_lock, daemon=True)
        lock_thread.start()
        self.assertTrue(lock_acquired.wait(timeout=5.0), "External SQLite writer did not acquire lock")

        num_writers = 20
        barrier = threading.Barrier(num_writers, timeout=10.0)
        results: list[int] = [0] * num_writers
        errors: list[str] = []
        err_lock = threading.Lock()

        def _http_writer(idx: int) -> None:
            try:
                barrier.wait()
                if idx % 2 == 0:
                    st, _, _ = http_json(
                        "PUT",
                        f"{hub.base_url}/context/wal_k_{idx}",
                        body={"value": idx, "updated_by": f"w-{idx}"},
                        timeout=20.0,
                    )
                else:
                    st, _, _ = http_json(
                        "POST",
                        f"{hub.base_url}/messages",
                        body={"sender_id": f"w-{idx}", "recipient_id": "*", "body": f"msg-{idx}"},
                        timeout=20.0,
                    )
                results[idx] = st
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(f"writer-{idx}: {exc}")

        threads = [threading.Thread(target=_http_writer, args=(i,), daemon=True) for i in range(num_writers)]
        for t in threads:
            t.start()
        lock_thread.join(timeout=10.0)
        self.assertFalse(lock_thread.is_alive())
        for t in threads:
            t.join(timeout=15.0)
            self.assertFalse(t.is_alive(), "Concurrent HTTP writer hung under WAL contention")

        self.assertEqual(holder_errors, [])
        self.assertEqual(errors, [])
        self.assertTrue(all(s in (200, 201) for s in results), f"Unexpected statuses under WAL contention: {results}")
        hub.stop()

        ctx_cnt = inspect_sqlite(db_path, "SELECT COUNT(*) AS cnt FROM context_kv WHERE key LIKE 'wal_k_%';")[0]["cnt"]
        msg_cnt = inspect_sqlite(db_path, "SELECT COUNT(*) AS cnt FROM messages WHERE body LIKE 'msg-%';")[0]["cnt"]
        self.assertEqual(ctx_cnt, 10)
        self.assertEqual(msg_cnt, 10)

    def test_f2_table_constraints_not_null_and_unique_agent_tokens(self) -> None:
        db_path = self.ws.db_dir / "constraints.db"
        hub = self.ws.start_hub(db_path=db_path)
        hub.stop()

        schema = inspect_sqlite_schema(db_path)
        at_cols = {c["name"]: c for c in schema["tables"]["agent_tokens"]}
        inv_cols = {c["name"]: c for c in schema["tables"]["invites"]}
        ev_cols = {c["name"]: c for c in schema["tables"]["events"]}

        self.assertEqual(at_cols["token_hash"]["pk"], 1)
        self.assertEqual(at_cols["agent_id"]["notnull"], 1)
        self.assertEqual(inv_cols["nonce"]["pk"], 1)
        for req_col in ("agent_id", "created_at", "expires_at", "used"):
            self.assertEqual(inv_cols[req_col]["notnull"], 1, f"invites.{req_col} must be NOT NULL")
        self.assertEqual(ev_cols["id"]["pk"], 1)
        for req_col in ("boot_id", "event_type", "actor_id", "created_at"):
            self.assertEqual(ev_cols[req_col]["notnull"], 1, f"events.{req_col} must be NOT NULL")

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO agent_tokens(token_hash, agent_id, created_at) VALUES (?, ?, ?);",
                ("h1", "dup-agent", 1.0),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO agent_tokens(token_hash, agent_id, created_at) VALUES (?, ?, ?);",
                    ("h2", "dup-agent", 2.0),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO agent_tokens(token_hash, agent_id, created_at) VALUES (?, ?, ?);",
                    ("h3", None, 2.0),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO invites(nonce, agent_id, created_at, expires_at) VALUES (?, ?, ?, ?);",
                    ("n1", None, 1.0, 2.0),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO events(boot_id, event_type, actor_id, created_at) VALUES (?, ?, ?, ?);",
                    (None, "t", "a", 1.0),
                )
        finally:
            conn.close()

    def test_f2_v2_indexes_and_column_defaults(self) -> None:
        db_path = self.ws.db_dir / "defaults_idx.db"
        hub = self.ws.start_hub(db_path=db_path)
        hub.stop()

        schema = inspect_sqlite_schema(db_path)
        indexes = schema["indexes"]
        self.assertIn("idx_events_created_at", indexes)
        self.assertEqual(indexes["idx_events_created_at"]["table"], "events")
        self.assertIn("created_at", indexes["idx_events_created_at"]["sql"].lower())
        self.assertIn("idx_invites_expires", indexes)
        self.assertEqual(indexes["idx_invites_expires"]["table"], "invites")
        self.assertIn("expires_at", indexes["idx_invites_expires"]["sql"].lower())

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO events(boot_id, event_type, actor_id, created_at) VALUES ('b1', 'test_ev', 'a1', 100.0);"
            )
            conn.execute(
                "INSERT INTO invites(nonce, agent_id, created_at, expires_at) VALUES ('nonce-def', 'a1', 100.0, 200.0);"
            )
            conn.execute(
                "INSERT INTO agent_tokens(token_hash, agent_id, created_at) VALUES ('hash-def', 'a1', 100.0);"
            )
            conn.commit()
        finally:
            conn.close()

        ev = inspect_sqlite(db_path, "SELECT * FROM events WHERE event_type = 'test_ev';")[0]
        self.assertEqual(ev["target_id"], "")
        self.assertEqual(ev["summary"], "")
        self.assertEqual(ev["payload_json"], "{}")

        inv = inspect_sqlite(db_path, "SELECT * FROM invites WHERE nonce = 'nonce-def';")[0]
        self.assertEqual(inv["role"], "agent")
        self.assertEqual(inv["created_by"], "")
        self.assertEqual(inv["used"], 0)
        self.assertIsNone(inv["used_at"])
        self.assertEqual(inv["used_by_agent"], "")

        tok = inspect_sqlite(db_path, "SELECT * FROM agent_tokens WHERE token_hash = 'hash-def';")[0]
        self.assertEqual(tok["role"], "agent")
        self.assertEqual(tok["revoked"], 0)

    def test_f2_higher_user_version_not_downgraded(self) -> None:
        db_path = self.ws.db_dir / "future_ver.db"
        hub = self.ws.start_hub(db_path=db_path)
        hub.stop()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA user_version = 99;")
            conn.commit()
        finally:
            conn.close()

        hub.start()
        st, _, health = http_json("GET", f"{hub.base_url}/health")
        self.assertEqual(st, 200)
        self.assertEqual(health["status"], "ok")
        hub.stop()

        self.assertEqual(inspect_sqlite_schema(db_path)["user_version"], 99)


# ---------------------------------------------------------------------------
# Feature F3: `events` Ring Buffer & `boot_id` Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF03EventsRingBufferBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F3: Exact 1000-row ring buffer boundary, boot_id uniqueness, Unicode, read non-emission & concurrency."""

    def test_f3_ring_buffer_exact_boundary_999_1000_1001(self) -> None:
        hub = self.ws.start_hub()
        statuses = send_persistent_put_batch("127.0.0.1", hub.port, 999, key_prefix="b_")
        self.assertTrue(all(s == 200 for s in statuses))

        row_999 = inspect_sqlite(
            hub.db_path, "SELECT COUNT(*) AS cnt, MIN(id) AS min_id, MAX(id) AS max_id FROM events;"
        )[0]
        self.assertEqual((row_999["cnt"], row_999["min_id"], row_999["max_id"]), (999, 1, 999))

        st1000, _, _ = http_json(
            "PUT", f"{hub.base_url}/context/b_1000", body={"value": 1000, "updated_by": "burst-agent"}
        )
        self.assertEqual(st1000, 200)
        row_1000 = inspect_sqlite(
            hub.db_path, "SELECT COUNT(*) AS cnt, MIN(id) AS min_id, MAX(id) AS max_id FROM events;"
        )[0]
        self.assertEqual((row_1000["cnt"], row_1000["min_id"], row_1000["max_id"]), (1000, 1, 1000))

        st1001, _, _ = http_json(
            "PUT", f"{hub.base_url}/context/b_1001", body={"value": 1001, "updated_by": "burst-agent"}
        )
        self.assertEqual(st1001, 200)
        row_1001 = inspect_sqlite(
            hub.db_path, "SELECT COUNT(*) AS cnt, MIN(id) AS min_id, MAX(id) AS max_id FROM events;"
        )[0]
        self.assertEqual((row_1001["cnt"], row_1001["min_id"], row_1001["max_id"]), (1000, 2, 1001))

    def test_f3_boot_id_uniqueness_across_multiple_restarts(self) -> None:
        hub = self.ws.start_hub()
        boot_ids: list[str] = []
        for idx in range(5):
            if idx > 0:
                hub.restart()
            st_h, _, health = http_json("GET", f"{hub.base_url}/health")
            self.assertEqual(st_h, 200)
            bid = str(health.get("boot_id", ""))
            self.assertTrue(len(bid) > 0)
            boot_ids.append(bid)
            st_m, _, _ = http_json(
                "POST",
                f"{hub.base_url}/messages",
                body={"sender_id": f"boot-{idx}", "recipient_id": "*", "body": f"run-{idx}"},
            )
            self.assertEqual(st_m, 201)

        self.assertEqual(len(set(boot_ids)), 5, f"Duplicate boot_ids observed: {boot_ids}")
        hub.stop()
        db_boots = {r["boot_id"] for r in inspect_sqlite(hub.db_path, "SELECT DISTINCT boot_id FROM events;")}
        for bid in boot_ids:
            self.assertIn(bid, db_boots)

    def test_f3_oversized_unicode_and_empty_payloads_in_events(self) -> None:
        hub = self.ws.start_hub()
        unicode_text = "سلام دنیا 🚀 日本語 — café & <xml>"
        large_val = "X" * 65536

        st_m, _, _ = http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "u-agent", "recipient_id": "*", "body": unicode_text},
        )
        self.assertEqual(st_m, 201)
        st_t, _, _ = http_json(
            "POST",
            f"{hub.base_url}/tasks",
            body={"title": unicode_text, "created_by": "u-agent", "description": ""},
        )
        self.assertEqual(st_t, 201)
        st_l, _, _ = http_json(
            "POST",
            f"{hub.base_url}/leases/acquire",
            body={"owner_id": "u-agent", "resource": "src/unicode.py", "reason": ""},
        )
        self.assertEqual(st_l, 200)
        st_c, _, _ = http_json(
            "PUT",
            f"{hub.base_url}/context/large_key",
            body={"value": large_val, "updated_by": "u-agent"},
        )
        self.assertEqual(st_c, 200)

        st_e, _, ev_body = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        self.assertEqual(st_e, 200)
        self.assertGreaterEqual(len(ev_body["events"]), 4)
        joined_summaries = "\n".join(str(e.get("summary", "")) for e in ev_body["events"])
        self.assertIn("سلام دنیا", joined_summaries)
        hub.stop()

        rows = inspect_sqlite(hub.db_path, "SELECT payload_json FROM events ORDER BY id ASC;")
        for r in rows:
            parsed = json.loads(r["payload_json"])
            self.assertIsInstance(parsed, dict)

    def test_f3_non_mutating_reads_do_not_create_events(self) -> None:
        hub = self.ws.start_hub()
        http_json("PUT", f"{hub.base_url}/context/k1", body={"value": "v1", "updated_by": "seed"})
        _, _, init_ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        baseline_id = int(init_ev["latest_id"])
        self.assertGreaterEqual(baseline_id, 1)

        mcp_initialize(hub.base_url)
        read_calls = [
            ("GET", "/health", None),
            ("GET", "/snapshot", None),
            ("GET", "/agents", None),
            ("GET", "/messages?agent_id=seed", None),
            ("GET", "/tasks", None),
            ("GET", "/leases", None),
            ("GET", "/context", None),
            ("GET", "/context/k1", None),
            ("GET", "/events?since_id=0&wait=0", None),
            ("POST", "/leases/check", {"paths": ["src/app.py"], "agent_id": "seed"}),
        ]
        for method, path, body in read_calls:
            st, _, _ = http_json(method, f"{hub.base_url}{path}", body=body)
            self.assertEqual(st, 200, f"{method} {path} returned {st}")

        for tool, args in (
            ("sync_status", {}),
            ("sync_get_context", {"key": "k1"}),
            ("sync_check_paths", {"paths": ["src/app.py"]}),
            ("sync_poll_messages", {"auto_ack": False}),
        ):
            st_m, rpc = mcp_call(hub.base_url, tool, args, agent_id="seed")
            self.assertEqual(st_m, 200)
            self.assertFalse(rpc["result"]["isError"])

        _, _, h_after = http_json("GET", f"{hub.base_url}/health")
        _, _, s_after = http_json("GET", f"{hub.base_url}/snapshot")
        _, _, e_after = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        self.assertEqual(int(h_after["latest_event_id"]), baseline_id)
        self.assertEqual(int(s_after["latest_event_id"]), baseline_id)
        self.assertEqual(int(e_after["latest_id"]), baseline_id)

    def test_f3_evicted_since_id_boundary_triggers_reset(self) -> None:
        hub = self.ws.start_hub()
        statuses = send_persistent_put_batch("127.0.0.1", hub.port, 1010, key_prefix="ev_")
        self.assertTrue(all(s == 200 for s in statuses))

        # Retained IDs are 11..1010 (min_id == 11, max_id == 1010)
        st1, _, b1 = http_json("GET", f"{hub.base_url}/events?since_id=10&wait=0&limit=1000")
        self.assertEqual(st1, 200)
        self.assertFalse(b1["reset"])
        self.assertEqual(int(b1["events"][0]["id"]), 11)

        st2, _, b2 = http_json("GET", f"{hub.base_url}/events?since_id=9&wait=0&limit=100")
        self.assertEqual(st2, 200)
        self.assertTrue(b2["reset"])

        st3, _, b3 = http_json("GET", f"{hub.base_url}/events?since_id=1010&wait=0")
        self.assertEqual(st3, 200)
        self.assertFalse(b3["reset"])
        self.assertEqual(b3["events"], [])

        st4, _, b4 = http_json("GET", f"{hub.base_url}/events?since_id=1011&wait=0")
        self.assertEqual(st4, 200)
        self.assertTrue(b4["reset"])

    def test_f3_concurrent_burst_mutations_preserve_1000_cap_and_monotonic_ids(self) -> None:
        hub = self.ws.start_hub()
        workers = 8
        per_worker = 150
        barrier = threading.Barrier(workers, timeout=10.0)
        failures: list[str] = []
        fail_lock = threading.Lock()

        def _burst(wid: int) -> None:
            try:
                barrier.wait()
                for i in range(per_worker):
                    if i % 2 == 0:
                        st, _, _ = http_json(
                            "PUT",
                            f"{hub.base_url}/context/c_{wid}_{i}",
                            body={"value": i, "updated_by": f"b-{wid}"},
                            timeout=20.0,
                        )
                    else:
                        st, _, _ = http_json(
                            "POST",
                            f"{hub.base_url}/messages",
                            body={"sender_id": f"b-{wid}", "recipient_id": "*", "body": f"{wid}:{i}"},
                            timeout=20.0,
                        )
                    if st not in (200, 201):
                        with fail_lock:
                            failures.append(f"w={wid} i={i} status={st}")
                        return
            except BaseException as exc:  # noqa: BLE001
                with fail_lock:
                    failures.append(f"w={wid} exc={exc}")

        threads = [threading.Thread(target=_burst, args=(w,), daemon=True) for w in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=45.0)
            self.assertFalse(t.is_alive(), "Concurrent burst worker hung")

        self.assertEqual(failures, [])
        st_e, _, ev_all = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0&limit=1000")
        self.assertEqual(st_e, 200)
        ids = [int(e["id"]) for e in ev_all["events"]]
        self.assertEqual(len(ids), 1000)
        self.assertTrue(all(ids[i] < ids[i + 1] for i in range(len(ids) - 1)))
        hub.stop()

        stats = inspect_sqlite(hub.db_path, "SELECT COUNT(*) AS cnt, MAX(id) AS max_id FROM events;")[0]
        self.assertEqual(stats["cnt"], 1000)
        self.assertGreaterEqual(stats["max_id"], workers * per_worker)


# ---------------------------------------------------------------------------
# Feature F4: `sync_wait` & `Condition` Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF04SyncWaitBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F4: `sync_wait` clamping <=15s, validation, boot_id reset, filters, non-blocking lock, bridge override."""

    def test_f4_sync_wait_timeout_zero_returns_immediately(self) -> None:
        hub = self.ws.start_hub()
        mcp_initialize(hub.base_url)
        _, _, ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(ev["latest_id"])

        t0 = time.monotonic()
        st, rpc = mcp_call(hub.base_url, "sync_wait", {"since_id": latest, "timeout_sec": 0}, timeout=10.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(st, 200)
        self.assertLess(elapsed, 1.5)
        self.assertFalse(rpc["result"]["isError"])
        sc = rpc["result"]["structuredContent"]
        self.assertTrue(sc["timed_out"])
        self.assertFalse(sc["reset"])
        self.assertEqual(sc["events"], [])

    def test_f4_sync_wait_timeout_clamped_le_15s_and_invalid_params_rejected(self) -> None:
        hub = self.ws.start_hub()
        mcp_initialize(hub.base_url)
        _, _, ev = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(ev["latest_id"])

        invalid_cases = [
            {"since_id": -1},
            {"since_id": True},
            {"since_id": 1.5},
            {"timeout_sec": -1},
            {"timeout_sec": True},
            {"timeout_sec": float("nan")},
            {"event_types": [""]},
            {"boot_id": 123},
            {"agent_id": "*"},
        ]
        for bad_args in invalid_cases:
            st, rpc = mcp_call(hub.base_url, "sync_wait", bad_args, timeout=10.0)
            self.assertEqual(st, 200, f"Unexpected HTTP status for {bad_args}: {rpc}")
            self.assertTrue(rpc["result"]["isError"], f"Expected isError=True for {bad_args}: {rpc}")
            self.assertEqual(rpc["result"]["structuredContent"].get("error"), "validation_error")

        # Upper valid boundary timeout_sec=15 with stale boot_id returns immediately
        st_b, rpc_b = mcp_call(
            hub.base_url,
            "sync_wait",
            {"since_id": latest, "boot_id": "stale-boot", "timeout_sec": 15},
            timeout=10.0,
        )
        self.assertEqual(st_b, 200)
        self.assertTrue(rpc_b["result"]["structuredContent"]["reset"])

        # Clamp boundary timeout_sec=999 must clamp to <= 15s
        t0 = time.monotonic()
        st_c, rpc_c = mcp_call(
            hub.base_url,
            "sync_wait",
            {"since_id": latest, "timeout_sec": 999},
            timeout=25.0,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(st_c, 200)
        self.assertFalse(rpc_c["result"]["isError"])
        self.assertTrue(rpc_c["result"]["structuredContent"]["timed_out"])
        self.assertGreaterEqual(elapsed, 14.0)
        self.assertLessEqual(elapsed, 18.0)

    def test_f4_sync_wait_boot_id_mismatch_returns_reset_immediately_without_blocking(self) -> None:
        hub = self.ws.start_hub()
        _, _, health = http_json("GET", f"{hub.base_url}/health")
        t0 = time.monotonic()
        st, rpc = mcp_call(
            hub.base_url,
            "sync_wait",
            {"since_id": int(health["latest_event_id"]), "boot_id": "stale-boot", "timeout_sec": 15},
            timeout=10.0,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(st, 200)
        self.assertLess(elapsed, 1.5)
        self.assertFalse(rpc["result"]["isError"])
        sc = rpc["result"]["structuredContent"]
        self.assertTrue(sc["reset"])
        self.assertFalse(sc["timed_out"])
        self.assertEqual(sc["boot_id"], health["boot_id"])

    def test_f4_sync_wait_event_types_filter_non_matching_vs_matching(self) -> None:
        hub = self.ws.start_hub()
        _, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(ev0["latest_id"])
        holder: dict[str, Any] = {}

        def _waiter() -> None:
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {"since_id": latest, "timeout_sec": 5.0, "event_types": ["lease_acquired"]},
                timeout=15.0,
            )
            holder["status"] = st
            holder["rpc"] = rpc

        th = threading.Thread(target=_waiter, daemon=True)
        th.start()
        time.sleep(0.25)

        http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "s1", "recipient_id": "*", "body": "non-matching"},
        )
        time.sleep(0.25)
        self.assertTrue(th.is_alive(), "sync_wait woke on non-matching message_sent event")

        _acquire_lease_http(hub.base_url, "lease-agent", "src/match.py")
        th.join(timeout=5.0)
        self.assertFalse(th.is_alive(), "sync_wait did not wake on matching lease_acquired event")
        self.assertEqual(holder["status"], 200)
        sc = holder["rpc"]["result"]["structuredContent"]
        self.assertFalse(sc["timed_out"])
        self.assertGreaterEqual(len(sc["events"]), 1)
        self.assertTrue(all(e["event_type"] == "lease_acquired" for e in sc["events"]))

    def test_f4_condition_wait_does_not_hold_db_lock_for_concurrent_writers(self) -> None:
        hub = self.ws.start_hub()
        _, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(ev0["latest_id"])

        n_waiters = 3
        barrier = threading.Barrier(n_waiters, timeout=5.0)
        results: list[dict[str, Any]] = [{} for _ in range(n_waiters)]

        def _waiter(idx: int) -> None:
            barrier.wait()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {"since_id": latest, "timeout_sec": 10.0},
                timeout=20.0,
            )
            results[idx] = {"status": st, "rpc": rpc}

        threads = [threading.Thread(target=_waiter, args=(i,), daemon=True) for i in range(n_waiters)]
        for t in threads:
            t.start()
        time.sleep(0.35)

        t_snap = time.monotonic()
        st_s, _, _ = http_json("GET", f"{hub.base_url}/snapshot", timeout=5.0)
        self.assertEqual(st_s, 200)
        self.assertLess(time.monotonic() - t_snap, 1.5)

        t_reg = time.monotonic()
        st_r, _, _ = http_json(
            "POST",
            f"{hub.base_url}/agents/register",
            body={"agent_id": "unblocked-writer", "role": "agent"},
            timeout=5.0,
        )
        self.assertEqual(st_r, 200)
        self.assertLess(time.monotonic() - t_reg, 1.5)

        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())
        for res in results:
            self.assertEqual(res["status"], 200)
            self.assertFalse(res["rpc"]["result"]["structuredContent"]["timed_out"])

    def test_f4_mcp_bridge_and_sync_client_timeout_exceeds_wait_plus_5s(self) -> None:
        from agsync.client import SyncClient

        hub = self.ws.start_hub()
        _, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(ev0["latest_id"])

        bridge_in = "\n".join(
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
                        "params": {"name": "sync_wait", "arguments": {"since_id": latest, "timeout_sec": 2.0}},
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
                "bridge-t2",
                "--timeout",
                "0.4",
            ],
            cwd=str(REPO_ROOT),
            env=self.ws.isolated_env(),
            input=bridge_in,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15.0,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        resp2 = next(r for r in lines if r.get("id") == 2)
        self.assertFalse(resp2["result"]["isError"], resp2)
        self.assertTrue(resp2["result"]["structuredContent"]["timed_out"])

        client = SyncClient(hub.base_url, timeout=0.2)
        t0 = time.monotonic()
        ev_res = client.get_events(since_id=latest, wait=1.5)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 1.2)
        self.assertEqual(ev_res["events"], [])


# ---------------------------------------------------------------------------
# Feature F5: `GET /events` Long-Polling Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF05GetEventsBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F5: `GET /events` query parameter boundaries, malformed input handling & concurrent long-pollers."""

    def test_f5_since_id_boundaries_negative_zero_and_future(self) -> None:
        hub = self.ws.start_hub()
        http_json("PUT", f"{hub.base_url}/context/s1", body={"value": 1, "updated_by": "a1"})
        _, _, init = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init["latest_id"])

        for neg in ("-1", "-100"):
            st, _, body = http_json("GET", f"{hub.base_url}/events?since_id={neg}&wait=0")
            self.assertEqual(st, 400, f"Expected 400 for since_id={neg}: {body}")
            self.assertEqual(body.get("error"), "validation_error")

        st0, _, b0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        self.assertEqual(st0, 200)
        self.assertFalse(b0["reset"])
        self.assertGreaterEqual(len(b0["events"]), 1)

        for future in (latest + 1, 999999):
            t0 = time.monotonic()
            st_f, _, bf = http_json("GET", f"{hub.base_url}/events?since_id={future}&wait=10", timeout=15.0)
            elapsed = time.monotonic() - t0
            self.assertEqual(st_f, 200)
            self.assertLess(elapsed, 1.5)
            self.assertTrue(bf["reset"])
            self.assertEqual(int(bf["latest_id"]), latest)

    def test_f5_wait_boundaries_negative_zero_15_and_999_clamped(self) -> None:
        hub = self.ws.start_hub()
        _, _, init = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init["latest_id"])

        for bad_wait in ("-0.1", "-10", "nan", "inf"):
            st, _, body = http_json("GET", f"{hub.base_url}/events?since_id={latest}&wait={bad_wait}")
            self.assertEqual(st, 400, f"Expected 400 for wait={bad_wait}: {body}")
            self.assertEqual(body.get("error"), "validation_error")

        t0 = time.monotonic()
        st0, _, b0 = http_json("GET", f"{hub.base_url}/events?since_id={latest}&wait=0")
        self.assertEqual(st0, 200)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(b0["events"], [])

        t1 = time.monotonic()
        st999, _, b999 = http_json("GET", f"{hub.base_url}/events?since_id={latest}&wait=999", timeout=25.0)
        elapsed = time.monotonic() - t1
        self.assertEqual(st999, 200)
        self.assertEqual(b999["events"], [])
        self.assertFalse(b999["reset"])
        self.assertGreaterEqual(elapsed, 14.0)
        self.assertLessEqual(elapsed, 18.0)

    def test_f5_limit_boundaries_zero_one_1000_and_10000(self) -> None:
        hub = self.ws.start_hub()
        statuses = send_persistent_put_batch("127.0.0.1", hub.port, 1005, key_prefix="lim_")
        self.assertTrue(all(s == 200 for s in statuses))

        for bad_lim in ("0", "-1", "1001", "10000"):
            st, _, body = http_json("GET", f"{hub.base_url}/events?since_id=0&limit={bad_lim}&wait=0")
            self.assertEqual(st, 400, f"Expected 400 for limit={bad_lim}: {body}")
            self.assertEqual(body.get("error"), "validation_error")

        st1, _, b1 = http_json("GET", f"{hub.base_url}/events?since_id=0&limit=1&wait=0")
        self.assertEqual(st1, 200)
        self.assertEqual(len(b1["events"]), 1)

        st1000, _, b1000 = http_json("GET", f"{hub.base_url}/events?since_id=0&limit=1000&wait=0")
        self.assertEqual(st1000, 200)
        self.assertEqual(len(b1000["events"]), 1000)

    def test_f5_types_filter_empty_unknown_and_multi_filter(self) -> None:
        hub = self.ws.start_hub()
        http_json("POST", f"{hub.base_url}/messages", body={"sender_id": "a1", "recipient_id": "*", "body": "m"})
        _acquire_lease_http(hub.base_url, "a1", "src/t.py")
        http_json("PUT", f"{hub.base_url}/context/ctx_t", body={"value": 1, "updated_by": "a1"})

        st_empty, _, b_empty = http_json("GET", f"{hub.base_url}/events?since_id=0&types=&wait=0")
        self.assertEqual(st_empty, 200)
        self.assertGreaterEqual(len(b_empty["events"]), 3)

        st_unk, _, b_unk = http_json("GET", f"{hub.base_url}/events?since_id=0&types=no_such_event&wait=0")
        self.assertEqual(st_unk, 200)
        self.assertEqual(b_unk["events"], [])

        st_csv, _, b_csv = http_json(
            "GET", f"{hub.base_url}/events?since_id=0&types=message_sent,lease_acquired&wait=0"
        )
        self.assertEqual(st_csv, 200)
        csv_types = {e["event_type"] for e in b_csv["events"]}
        self.assertEqual(csv_types, {"message_sent", "lease_acquired"})

        st_rep, _, b_rep = http_json(
            "GET", f"{hub.base_url}/events?since_id=0&types=message_sent&types=lease_acquired&wait=0"
        )
        self.assertEqual(st_rep, 200)
        rep_types = {e["event_type"] for e in b_rep["events"]}
        self.assertEqual(rep_types, {"message_sent", "lease_acquired"})

    def test_f5_malformed_query_params_return_400_safely(self) -> None:
        hub = self.ws.start_hub()
        bad_queries = [
            "since_id=abc",
            "limit=xyz",
            "wait=notanumber",
            "agent_id=*",
            "agent_id=../bad",
        ]
        for qs in bad_queries:
            st, _, body = http_json("GET", f"{hub.base_url}/events?{qs}")
            self.assertEqual(st, 400, f"Expected 400 for query '{qs}', got {st} {body}")
            self.assertEqual(body.get("error"), "validation_error")

        # Abrupt socket disconnect during long-poll leaves hub healthy
        sock = socket.create_connection(("127.0.0.1", int(hub.port)), timeout=5.0)
        try:
            sock.sendall(
                f"GET /events?since_id=0&wait=10 HTTP/1.1\r\nHost: 127.0.0.1:{hub.port}\r\n\r\n".encode("ascii")
            )
            time.sleep(0.15)
        finally:
            sock.close()

        st_ok, _, _ = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        self.assertEqual(st_ok, 200)

    def test_f5_boot_id_mismatch_and_concurrent_long_pollers_wake(self) -> None:
        hub = self.ws.start_hub()
        _, _, init = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        latest = int(init["latest_id"])

        t0 = time.monotonic()
        st_b, _, b_boot = http_json(
            "GET", f"{hub.base_url}/events?since_id={latest}&boot_id=stale-boot&wait=10", timeout=10.0
        )
        self.assertEqual(st_b, 200)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertTrue(b_boot["reset"])

        n_pollers = 4
        barrier = threading.Barrier(n_pollers, timeout=5.0)
        poll_results: list[dict[str, Any]] = [{} for _ in range(n_pollers)]

        def _poller(idx: int) -> None:
            barrier.wait()
            ts = time.monotonic()
            st, _, body = http_json("GET", f"{hub.base_url}/events?since_id={latest}&wait=10", timeout=20.0)
            poll_results[idx] = {"status": st, "body": body, "elapsed": time.monotonic() - ts}

        threads = [threading.Thread(target=_poller, args=(i,), daemon=True) for i in range(n_pollers)]
        for t in threads:
            t.start()
        time.sleep(0.3)

        http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "wake-sender", "recipient_id": "*", "body": "wake-all"},
        )
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())

        for res in poll_results:
            self.assertEqual(res["status"], 200)
            self.assertLess(res["elapsed"], 3.0)
            self.assertGreaterEqual(len(res["body"]["events"]), 1)


# ---------------------------------------------------------------------------
# Feature F6: Upgraded `/dashboard` Security & UI/UX Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF06DashboardSecurityAndUIUXBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F6: `/dashboard` XSS escaping, auth boundary, dark/light token parity, WCAG, SVG-only, method rejection."""

    def _extract_js_function(self, html: str, fn_name: str) -> str:
        m = re.search(rf"function\s+{re.escape(fn_name)}\s*\([^)]*\)\s*\{{", html)
        self.assertIsNotNone(m, f"Missing renderer function {fn_name}")
        start = m.end()
        depth = 1
        idx = start
        while idx < len(html) and depth > 0:
            ch = html[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            idx += 1
        self.assertEqual(depth, 0, f"Unclosed JS function {fn_name}")
        return html[start : idx - 1]

    def test_f6_dashboard_xss_escaping_in_feed_and_tables(self) -> None:
        hub = self.ws.start_hub()
        xss = '<script>alert("xss")</script><img src=x onerror=alert(1)>'
        http_json("POST", f"{hub.base_url}/tasks", body={"title": xss, "created_by": "xss-agent"})
        _acquire_lease_http(hub.base_url, "xss-agent", "src/xss.py", reason=xss)
        http_json("PUT", f"{hub.base_url}/context/xss_key", body={"value": xss, "updated_by": "xss-agent"})
        http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "xss-agent", "recipient_id": "*", "body": xss},
        )

        st, _, raw = http_request("GET", f"{hub.base_url}/dashboard")
        self.assertEqual(st, 200)
        html = raw.decode("utf-8", errors="replace")
        self.assertNotIn("<script>alert", html)
        self.assertIn("function esc(", html)
        for seq in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(seq, html)

        # Verify all dynamic ${...} expressions inside the 5 table renderers use esc(...) or known safe status/class helpers
        for fn_name in ("renderFeed", "renderAgents", "renderLeases", "renderTasks", "renderContext"):
            fn_body = self._extract_js_function(html, fn_name)
            for expr in re.findall(r"\$\{([^}]+)\}", fn_body):
                safe = (
                    expr.strip().startswith("esc(")
                    or "eventBadgeClass(" in expr
                    or "is_online ?" in expr
                    or 'status === "done"' in expr
                )
                self.assertTrue(safe, f"Unsafe unescaped interpolation ${{{expr}}} in {fn_name}")

        self.assertIn("errBanner.textContent", html)
        self.assertIn('document.getElementById("hub-meta").textContent', html)

    def test_f6_dashboard_missing_and_invalid_auth_behavior(self) -> None:
        hub = self.ws.start_hub(token="dash-secret")
        for route in ("/", "/dashboard"):
            st, hdrs, _ = http_request("GET", f"{hub.base_url}{route}")
            self.assertEqual(st, 200)
            self.assertIn("text/html", hdrs.get("content-type", ""))

        for api_path in ("/snapshot", "/events?since_id=0&wait=0"):
            st_no, hdrs_no, _ = http_json("GET", f"{hub.base_url}{api_path}")
            self.assertEqual(st_no, 401)
            self.assertIn("bearer", hdrs_no.get("www-authenticate", "").lower())

            st_bad, hdrs_bad, _ = http_json(
                "GET", f"{hub.base_url}{api_path}", headers=auth_headers(token="wrong-secret")
            )
            self.assertEqual(st_bad, 401)
            self.assertIn("bearer", hdrs_bad.get("www-authenticate", "").lower())

        _, _, raw = http_request("GET", f"{hub.base_url}/dashboard")
        html = raw.decode("utf-8", errors="replace")
        self.assertIn("401", html)
        self.assertRegex(html, r'id="token-input"[^>]*type="password"')
        self.assertIn("sessionStorage", html)
        self.assertNotIn("localStorage", html)

    def test_f6_dashboard_dark_light_token_parity_and_reduced_motion(self) -> None:
        hub = self.ws.start_hub()
        _, _, raw = http_request("GET", f"{hub.base_url}/dashboard")
        html = raw.decode("utf-8", errors="replace")

        m_dark = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{(.*?)\}", html, re.DOTALL)
        self.assertIsNotNone(m_dark, "Missing @media (prefers-color-scheme: dark) :root block")
        dark_block = m_dark.group(1)

        light_block = html[: m_dark.start()]
        m_light = re.search(r":root\s*\{(.*?)\}", light_block, re.DOTALL)
        self.assertIsNotNone(m_light, "Missing light :root block")

        light_tokens = set(re.findall(r"(--[a-z0-9-]+)\s*:", m_light.group(1)))
        dark_tokens = set(re.findall(r"(--[a-z0-9-]+)\s*:", dark_block))
        self.assertGreaterEqual(len(light_tokens), 10)
        self.assertEqual(light_tokens, dark_tokens, "Dark and light CSS custom properties do not match")

        m_rm = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\s*\}", html, re.DOTALL)
        self.assertIsNotNone(m_rm, "Missing prefers-reduced-motion rule")
        self.assertIn("animation-duration", m_rm.group(1))
        self.assertIn("transition-duration", m_rm.group(1))

    def test_f6_dashboard_table_overflow_x_auto_wrappers_and_wcag_targets(self) -> None:
        hub = self.ws.start_hub()
        _, _, raw = http_request("GET", f"{hub.base_url}/dashboard")
        html = raw.decode("utf-8", errors="replace")

        self.assertRegex(html, r"\.table-wrap\s*\{[^}]*overflow-x:\s*auto")
        for fn_name in ("renderFeed", "renderAgents", "renderLeases", "renderTasks", "renderContext"):
            fn_body = self._extract_js_function(html, fn_name)
            self.assertIn('<div class="table-wrap"><table>', fn_body, f"{fn_name} missing .table-wrap wrapper")

        self.assertRegex(html, r'<meta\s+name="viewport"\s+content="width=device-width,\s*initial-scale=1"')
        self.assertNotIn("user-scalable=no", html.lower())
        self.assertNotIn("maximum-scale=1", html.lower())
        self.assertIn("min-height: 44px", html)
        self.assertIn(":focus-visible", html)
        self.assertIn("<label", html)
        self.assertIn("aria-label", html)

    def test_f6_dashboard_zero_emoji_structural_icons_svg_only(self) -> None:
        hub = self.ws.start_hub()
        _, _, raw = http_request("GET", f"{hub.base_url}/dashboard")
        html = raw.decode("utf-8", errors="replace")

        self.assertEqual(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", html), [])
        svg_tags = re.findall(r"<svg\b[^>]*>", html, re.IGNORECASE)
        self.assertGreaterEqual(len(svg_tags), 3)
        for tag in svg_tags:
            self.assertIn('aria-hidden="true"', tag, f"Decorative SVG missing aria-hidden='true': {tag}")

    def test_f6_dashboard_no_unescaped_inline_script_breaks_and_method_rejection(self) -> None:
        hub = self.ws.start_hub()
        _, _, raw = http_request("GET", f"{hub.base_url}/dashboard")
        html = raw.decode("utf-8", errors="replace")

        self.assertEqual(len(re.findall(r"<script\b", html, re.IGNORECASE)), 1)
        self.assertEqual(len(re.findall(r"</script>", html, re.IGNORECASE)), 1)
        self.assertIsNone(re.search(r"\son(?:click|error|load)\s*=", html, re.IGNORECASE))

        st_post, _, _ = http_json("POST", f"{hub.base_url}/dashboard", body={})
        self.assertIn(st_post, (404, 405))
        st_del, _, _ = http_json("DELETE", f"{hub.base_url}/dashboard")
        self.assertIn(st_del, (404, 405))


# ---------------------------------------------------------------------------
# Feature F7: Invite / Join Security Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF07InviteJoinSecurityBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F7: Invite TTL expiry, HMAC tampering, cross-hub rejection, concurrent replay race, validation & auth."""

    def test_f7_expired_ttl_and_nonpositive_ttl_rejected(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        for bad_ttl in (0, -5):
            st_bad, _, body_bad = http_json(
                "POST",
                f"{hub.base_url}/invites",
                body={"agent_id": "ttl-peer", "ttl_sec": bad_ttl},
                headers=auth_headers(token="admin-secret"),
            )
            self.assertEqual(st_bad, 400, f"Expected 400 for ttl_sec={bad_ttl}: {body_bad}")

        st_ok, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "short-ttl-peer", "ttl_sec": 1},
            headers=auth_headers(token="admin-secret"),
        )
        self.assertIn(st_ok, (200, 201), f"POST /invites failed: {st_ok} {inv}")
        time.sleep(1.3)

        st_j, _, body_j = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertIn(st_j, (401, 403, 410), f"Expected rejection on expired invite, got {st_j} {body_j}")
        rows = inspect_sqlite(hub.db_path, "SELECT used FROM invites WHERE nonce = ?;", (inv["nonce"],))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["used"], 0)

    def test_f7_tampered_hmac_signature_rejected_401_or_403(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        st_ok, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "hmac-peer", "ttl_sec": 300},
            headers=auth_headers(token="admin-secret"),
        )
        self.assertIn(st_ok, (200, 201), f"POST /invites failed: {st_ok} {inv}")
        orig = str(inv["token"])

        flipped_last = ("A" if orig[-1] != "A" else "B")
        tampered_sig = orig[:-1] + flipped_last
        tampered_mid = ("X" if orig[0] != "X" else "Y") + orig[1:]
        for bad_tok in (tampered_sig, "invalid.token.sig", tampered_mid):
            st_b, _, body_b = http_json(
                "POST",
                f"{hub.base_url}/invites/join",
                body={"invite_token": bad_tok},
            )
            self.assertIn(st_b, (401, 403), f"Tampered invite token accepted/crashed: {st_b} {body_b}")

        # Untampered original token must still succeed (tampering did not consume nonce)
        st_good, _, joined = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": orig},
        )
        self.assertEqual(st_good, 200, f"Valid invite failed after tampered attempts: {st_good} {joined}")

    def test_f7_wrong_or_unknown_nonce_and_cross_hub_token_rejected(self) -> None:
        hub1 = self.ws.start_hub(token="admin-1", db_path=self.ws.db_dir / "hub1.db")
        hub2 = self.ws.start_hub(token="admin-2", db_path=self.ws.db_dir / "hub2.db")

        st2, _, inv2 = http_json(
            "POST",
            f"{hub2.base_url}/invites",
            body={"agent_id": "foreign-peer", "ttl_sec": 300},
            headers=auth_headers(token="admin-2"),
        )
        self.assertIn(st2, (200, 201), f"hub2 POST /invites failed: {st2} {inv2}")

        for bad_tok in ("unknown-nonce-token", inv2["token"]):
            st_r, _, body_r = http_json(
                "POST",
                f"{hub1.base_url}/invites/join",
                body={"invite_token": bad_tok},
            )
            self.assertIn(st_r, (401, 403, 404), f"Cross-hub/unknown token should be rejected: {st_r} {body_r}")

        rows = inspect_sqlite(hub1.db_path, "SELECT COUNT(*) AS cnt FROM agent_tokens;")
        self.assertEqual(rows[0]["cnt"], 0)

    def test_f7_concurrent_parallel_join_replay_race_only_one_succeeds(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        st_i, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "race-peer", "ttl_sec": 300},
            headers=auth_headers(token="admin-secret"),
        )
        self.assertIn(st_i, (200, 201), f"POST /invites failed: {st_i} {inv}")

        n_threads = 8
        barrier = threading.Barrier(n_threads, timeout=5.0)
        statuses: list[int] = [0] * n_threads
        errors: list[str] = []
        err_lock = threading.Lock()

        def _joiner(idx: int) -> None:
            try:
                barrier.wait()
                st, _, _ = http_json(
                    "POST",
                    f"{hub.base_url}/invites/join",
                    body={"invite_token": inv["token"]},
                    timeout=15.0,
                )
                statuses[idx] = st
            except BaseException as exc:  # noqa: BLE001
                with err_lock:
                    errors.append(f"t={idx}: {exc}")

        threads = [threading.Thread(target=_joiner, args=(i,), daemon=True) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(statuses.count(200), 1, f"Expected exactly 1 winner, got {statuses}")
        losers = [s for s in statuses if s != 200]
        self.assertTrue(all(s in (401, 403, 409) for s in losers), f"Unexpected loser statuses: {losers}")

        inv_rows = inspect_sqlite(hub.db_path, "SELECT used FROM invites WHERE nonce = ?;", (inv["nonce"],))
        self.assertEqual(inv_rows[0]["used"], 1)
        tok_rows = inspect_sqlite(hub.db_path, "SELECT COUNT(*) AS cnt FROM agent_tokens WHERE agent_id = 'race-peer';")
        self.assertEqual(tok_rows[0]["cnt"], 1)

        st_rep, _, _ = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertIn(st_rep, (401, 403, 409))

    def test_f7_missing_or_empty_invite_token_or_invalid_agent_id(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        for bad_join_body in ({}, {"invite_token": ""}, {"invite_token": "   "}):
            st, _, body = http_json("POST", f"{hub.base_url}/invites/join", body=bad_join_body)
            self.assertIn(st, (400, 401), f"Bad join accepted: {st} {body}")

        st_txt, _, _ = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body="not-json",
            headers={"Content-Type": "text/plain"},
        )
        self.assertIn(st_txt, (400, 415))

        st_raw, _, _ = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body="{bad-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(st_raw, 400)

        for bad_create in ({}, {"agent_id": ""}, {"agent_id": "*"}, {"agent_id": "../bad"}):
            st_c, _, body_c = http_json(
                "POST",
                f"{hub.base_url}/invites",
                body=bad_create,
                headers=auth_headers(token="admin-secret"),
            )
            self.assertEqual(st_c, 400, f"Expected 400 for POST /invites {bad_create}, got {st_c} {body_c}")

    def test_f7_invites_require_admin_auth_while_join_is_tokenless(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        st_no_post, _, _ = http_json("POST", f"{hub.base_url}/invites", body={"agent_id": "x"})
        st_no_get, _, _ = http_json("GET", f"{hub.base_url}/invites")
        st_bad_post, _, _ = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "x"},
            headers=auth_headers(token="wrong-admin"),
        )
        self.assertEqual((st_no_post, st_no_get, st_bad_post), (401, 401, 401))

        st_ok, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "tokenless-peer"},
            headers=auth_headers(token="admin-secret"),
        )
        self.assertIn(st_ok, (200, 201), f"POST /invites failed: {st_ok} {inv}")

        st_j, _, joined = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertEqual(st_j, 200, f"Tokenless join failed: {st_j} {joined}")


# ---------------------------------------------------------------------------
# Feature F8: Per-`agent_id` Bearer Token Scoping Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF08AgentTokenScopingBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F8: Invalid/revoked tokens, 403 admin isolation, impersonation prevention, header casing & re-invite."""

    def test_f8_invalid_malformed_and_revoked_bearer_token_returns_401(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        _, joined = _create_and_join_invite(hub.base_url, "admin-secret", "revokable-peer")
        peer_tok = str(joined["token"])

        bad_headers = [
            {},
            {"Authorization": "Bearer wrong-token"},
            {"Authorization": "Token abc"},
            {"Authorization": "Basic dXNlcjpw"},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer   "},
        ]
        for hdrs in bad_headers:
            st, resp_hdrs, _ = http_json("GET", f"{hub.base_url}/snapshot", headers=hdrs)
            self.assertEqual(st, 401, f"Expected 401 for headers {hdrs}, got {st}")
            self.assertIn("bearer", resp_hdrs.get("www-authenticate", "").lower())

        hub.stop()
        conn = sqlite3.connect(str(hub.db_path))
        try:
            conn.execute("UPDATE agent_tokens SET revoked = 1 WHERE agent_id = ?;", ("revokable-peer",))
            conn.commit()
        finally:
            conn.close()
        hub.start()

        st_rev, _, _ = http_json(
            "GET",
            f"{hub.base_url}/snapshot",
            headers=auth_headers(token=peer_tok, agent_id="revokable-peer"),
        )
        self.assertEqual(st_rev, 401)

    def test_f8_agent_token_forbidden_403_on_state_and_invites_endpoints(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        _, joined = _create_and_join_invite(hub.base_url, "admin-secret", "scoped-peer")
        hdrs = auth_headers(token=joined["token"], agent_id="scoped-peer")

        self.assertEqual(http_json("GET", f"{hub.base_url}/state/export", headers=hdrs)[0], 403)
        self.assertEqual(http_json("POST", f"{hub.base_url}/state/import", body={"agents": []}, headers=hdrs)[0], 403)
        self.assertEqual(
            http_json("POST", f"{hub.base_url}/invites", body={"agent_id": "other"}, headers=hdrs)[0], 403
        )
        self.assertEqual(http_json("GET", f"{hub.base_url}/invites", headers=hdrs)[0], 403)

    def test_f8_agent_token_impersonation_header_query_and_heartbeat_returns_403(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        _, joined_a = _create_and_join_invite(hub.base_url, "admin-secret", "agent-a")
        tok_a = str(joined_a["token"])
        http_json(
            "POST",
            f"{hub.base_url}/agents/register",
            body={"agent_id": "agent-b", "role": "agent"},
            headers=auth_headers(token="admin-secret"),
        )

        self.assertEqual(
            http_json("GET", f"{hub.base_url}/snapshot", headers=auth_headers(token=tok_a, agent_id="agent-b"))[0],
            403,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/messages",
                body={"sender_id": "agent-a", "recipient_id": "*", "body": "hi"},
                headers=auth_headers(token=tok_a, agent_id="agent-b"),
            )[0],
            403,
        )
        self.assertEqual(
            http_json(
                "GET",
                f"{hub.base_url}/messages?agent_id=agent-b",
                headers=auth_headers(token=tok_a, agent_id="agent-a"),
            )[0],
            403,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/agents/agent-b/heartbeat",
                body={"status": "online"},
                headers=auth_headers(token=tok_a, agent_id="agent-a"),
            )[0],
            403,
        )
        st_mcp, rpc_mcp = mcp_call(
            hub.base_url,
            "sync_status",
            {},
            token=tok_a,
            agent_id="agent-a",
            query="agent=agent-b",
        )
        self.assertTrue(
            st_mcp == 403 or bool(rpc_mcp.get("result", {}).get("isError")),
            f"Expected 403/isError on MCP query impersonation: {st_mcp} {rpc_mcp}",
        )

    def test_f8_agent_token_impersonation_body_actor_fields_returns_403(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        _, joined_a = _create_and_join_invite(hub.base_url, "admin-secret", "agent-a")
        hdrs_a = auth_headers(token=joined_a["token"], agent_id="agent-a")

        st_t, _, task = http_json(
            "POST",
            f"{hub.base_url}/tasks",
            body={"title": "Open Task", "created_by": "admin"},
            headers=auth_headers(token="admin-secret"),
        )
        self.assertEqual(st_t, 201)
        tid = task["task_id"]

        impersonation_calls = [
            ("POST", "/agents/register", {"agent_id": "agent-b", "role": "agent"}),
            ("POST", "/messages/ack", {"agent_id": "agent-b", "up_to_id": 0}),
            ("POST", f"/tasks/{tid}/claim", {"agent_id": "agent-b"}),
            ("PATCH", f"/tasks/{tid}", {"agent_id": "agent-b", "status": "done"}),
            ("POST", "/messages", {"sender_id": "agent-b", "recipient_id": "*", "body": "spoof"}),
            ("POST", "/leases/acquire", {"owner_id": "agent-b", "resource": "src/spoof.py"}),
            ("POST", "/leases/renew", {"owner_id": "agent-b", "resource": "src/spoof.py"}),
            ("POST", "/leases/release", {"owner_id": "agent-b", "resource": "src/spoof.py"}),
            ("PUT", "/context/spoof", {"value": "x", "updated_by": "agent-b"}),
            ("POST", "/tasks", {"title": "Spoof", "created_by": "agent-b"}),
        ]
        for method, path, body in impersonation_calls:
            st, _, resp = http_json(method, f"{hub.base_url}{path}", body=body, headers=hdrs_a)
            self.assertEqual(st, 403, f"Expected 403 on {method} {path} {body}, got {st} {resp}")

        st_m, rpc_m = mcp_call(
            hub.base_url,
            "sync_send_message",
            {"sender_id": "agent-b", "recipient_id": "*", "body": "spoof"},
            token=joined_a["token"],
            agent_id="agent-a",
        )
        self.assertTrue(
            st_m == 403 or bool(rpc_m.get("result", {}).get("isError")),
            f"Expected 403/isError on MCP body impersonation: {st_m} {rpc_m}",
        )

    def test_f8_empty_and_case_variant_auth_headers(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        _, joined = _create_and_join_invite(hub.base_url, "admin-secret", "hdr-peer")
        tok = str(joined["token"])

        def _raw_get(auth_name: str, auth_val: str, extra_agent: str | None = None) -> int:
            conn = http.client.HTTPConnection("127.0.0.1", int(hub.port), timeout=10.0)
            try:
                hdrs = {auth_name: auth_val}
                if extra_agent:
                    hdrs["X-Agent-Id"] = extra_agent
                conn.request("GET", "/snapshot", headers=hdrs)
                resp = conn.getresponse()
                resp.read()
                return int(resp.status)
            finally:
                conn.close()

        self.assertEqual(_raw_get("authorization", f"Bearer {tok}"), 200)
        self.assertEqual(_raw_get("Authorization", ""), 401)
        self.assertEqual(_raw_get("Authorization", "Bearer "), 401)
        st_lower = _raw_get("Authorization", f"bearer {tok}")
        self.assertIn(st_lower, (200, 401))
        if st_lower == 200:
            self.assertEqual(_raw_get("Authorization", f"bearer {tok}", extra_agent="other-agent"), 403)

    def test_f8_agent_token_read_impersonation_and_duplicate_agent_reinvite(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")
        _, joined_1 = _create_and_join_invite(hub.base_url, "admin-secret", "agent-a")
        tok_1 = str(joined_1["token"])
        hdrs_1 = auth_headers(token=tok_1, agent_id="agent-a")

        self.assertEqual(http_json("GET", f"{hub.base_url}/messages?agent_id=agent-b", headers=hdrs_1)[0], 403)
        st_chk, _, _ = http_json(
            "POST",
            f"{hub.base_url}/leases/check",
            body={"paths": ["src/app.py"], "agent_id": "agent-b"},
            headers=hdrs_1,
        )
        self.assertEqual(st_chk, 403)

        st_i2, _, inv2 = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "agent-a"},
            headers=auth_headers(token="admin-secret"),
        )
        self.assertIn(st_i2, (200, 201))
        st_j2, _, joined_2 = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv2["token"]},
        )
        self.assertIn(st_j2, (200, 409), f"Re-invite join returned unexpected status {st_j2}: {joined_2}")
        if st_j2 == 200:
            tok_2 = str(joined_2["token"])
            self.assertEqual(
                http_json("GET", f"{hub.base_url}/snapshot", headers=auth_headers(token=tok_2, agent_id="agent-a"))[0],
                200,
            )
            self.assertEqual(
                http_json("GET", f"{hub.base_url}/snapshot", headers=auth_headers(token=tok_1, agent_id="agent-a"))[0],
                401,
            )


# ---------------------------------------------------------------------------
# Feature F9: Cloudflare Tunnel, `--base-url` & Remote Proxy Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF09TunnelAndRemoteBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F9: Host/Origin security, X-Forwarded-Host, TLS port matching, bad base_url, proxy-bypass, tunnel adapter."""

    def test_f9_disallowed_host_and_mismatched_origin_return_403(self) -> None:
        hub = self.ws.start_hub(base_url="https://allowed-hub.example.com")

        st_h, _, b_h = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={"Host": "evil-rebinding.example.com"},
        )
        self.assertEqual(st_h, 403, b_h)
        self.assertEqual(b_h.get("error"), "forbidden_host")

        for bad_origin in ("https://evil.example.com", "null", "file://localhost"):
            st_o, _, b_o = http_json(
                "GET",
                f"{hub.base_url}/health",
                headers={"Host": "allowed-hub.example.com", "Origin": bad_origin},
            )
            self.assertEqual(st_o, 403, f"Origin {bad_origin!r} should be blocked: {b_o}")
            self.assertEqual(b_o.get("error"), "forbidden_origin")

    def test_f9_spoofed_x_forwarded_host_cannot_bypass_host_or_origin_check(self) -> None:
        hub = self.ws.start_hub(base_url="https://trusted-hub.example.com")

        st1, _, b1 = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={
                "Host": "attacker.example.com",
                "X-Forwarded-Host": "trusted-hub.example.com",
            },
        )
        self.assertEqual(st1, 403, b1)
        self.assertEqual(b1.get("error"), "forbidden_host")

        st2, _, b2 = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={
                "Host": f"127.0.0.1:{hub.port}",
                "X-Forwarded-Host": "spoofed.example.com",
                "Origin": "https://spoofed.example.com",
            },
        )
        self.assertEqual(st2, 403, b2)
        self.assertEqual(b2.get("error"), "forbidden_origin")

        st3, _, b3 = http_json(
            "POST",
            f"{hub.base_url}/leases/acquire",
            body={"owner_id": "a1", "resource": "src/app.py"},
            headers={
                "Host": f"127.0.0.1:{hub.port}",
                "X-Forwarded-Host": "spoofed.example.com",
                "Origin": "https://spoofed.example.com",
            },
        )
        self.assertEqual(st3, 403, b3)

    def test_f9_https_origin_443_vs_nonstandard_port_mismatch_rejected(self) -> None:
        hub = self.ws.start_hub(base_url="https://tls-hub.example.com")

        for hdrs in (
            {"Host": "tls-hub.example.com", "Origin": "https://tls-hub.example.com:8443"},
            {"Host": "tls-hub.example.com:8443", "Origin": "https://tls-hub.example.com"},
            {"Host": "tls-hub.example.com", "Origin": "http://tls-hub.example.com:443"},
        ):
            st, _, body = http_json("GET", f"{hub.base_url}/health", headers=hdrs)
            self.assertEqual(st, 403, f"Expected 403 for {hdrs}, got {st} {body}")
            self.assertEqual(body.get("error"), "forbidden_origin")

        st_ok, _, body_ok = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={"Host": "tls-hub.example.com", "Origin": "https://tls-hub.example.com:443"},
        )
        self.assertEqual(st_ok, 200, body_ok)

    def test_f9_invalid_or_non_http_base_url_rejected_cleanly(self) -> None:
        hub = self.ws.start_hub(token="admin-secret")

        for bad_url in ("ftp://bad.example.com", "not-a-url", "javascript:alert(1)"):
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agsync.cli",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--db",
                    str(self.ws.db_dir / "bad_base.db"),
                    "--base-url",
                    bad_url,
                    "--no-discover",
                ],
                cwd=str(REPO_ROOT),
                env=self.ws.isolated_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.ws.register_proc(proc)
            rc = proc.wait(timeout=5.0)
            self.assertNotEqual(rc, 0, f"serve accepted invalid --base-url {bad_url!r}")

            res_inv = self.ws.run_cli(
                "invite",
                "--server",
                hub.base_url,
                "--token",
                "admin-secret",
                "--agent",
                "bad-url-peer",
                "--base-url",
                bad_url,
            )
            self.assertNotEqual(res_inv.returncode, 0, f"invite accepted invalid --base-url {bad_url!r}")

        st_p, _, body_p = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "bad-base-peer", "base_url": "ftp://bad.example.com"},
            headers=auth_headers(token="admin-secret"),
        )
        self.assertEqual(st_p, 400, f"Expected 400 for invalid base_url on POST /invites, got {st_p} {body_p}")

    def test_f9_proxy_bypass_local_and_lan_bypass_while_remote_https_respects_proxy(self) -> None:
        from agsync.adapters.network import build_opener_for_url
        from agsync.client import SyncClient, SyncClientError

        proxy = _DummyHttpProxy()
        self.ws.register_cleanup(proxy.stop)
        hub = self.ws.start_hub()

        res_cli = self.ws.run_cli(
            "status",
            "--server",
            hub.base_url,
            "--json",
            env_overrides={"HTTP_PROXY": proxy.url, "HTTPS_PROXY": proxy.url},
        )
        self.assertEqual(res_cli.returncode, 0, res_cli.stderr or res_cli.stdout)
        self.assertEqual(proxy.snapshot_hits(), [], "Loopback CLI status routed through HTTP_PROXY")

        old_http = os.environ.get("HTTP_PROXY")
        old_https = os.environ.get("HTTPS_PROXY")
        os.environ["HTTP_PROXY"] = proxy.url
        os.environ["HTTPS_PROXY"] = proxy.url
        try:
            for local_url in (hub.base_url, "http://192.168.1.50:8765"):
                local_opener = build_opener_for_url(local_url)
                has_empty_proxy_handler = any(
                    isinstance(h, urllib.request.ProxyHandler) and h.proxies == {}
                    for h in local_opener.handlers
                )
                self.assertTrue(has_empty_proxy_handler, f"Expected ProxyHandler({{}}) bypass for {local_url}")

            client = SyncClient("https://remote-tunnel.trycloudflare.com", timeout=2.0)
            with self.assertRaises(SyncClientError):
                client.health()
            hits = proxy.snapshot_hits()
            self.assertTrue(
                any("remote-tunnel.trycloudflare.com" in target for _, target in hits),
                f"Remote HTTPS request did not route via HTTPS_PROXY: {hits}",
            )
        finally:
            if old_http is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old_http
            if old_https is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = old_https

    def test_f9_tunnel_adapter_invalid_url_and_missing_cloudflared_binary(self) -> None:
        from agsync.adapters.tunnel import CloudflareTunnelAdapter, build_cloudflared_command

        for bad_url in ("", "ftp://bad.example.com", "not-a-url"):
            with self.assertRaises((ValueError, TypeError)):
                build_cloudflared_command(bad_url)

        adapter = CloudflareTunnelAdapter("http://127.0.0.1:8765")
        adapter.stop()  # Must be idempotent when never started

        old_path = os.environ.get("PATH")
        os.environ["PATH"] = ""
        try:
            with self.assertRaises((FileNotFoundError, RuntimeError, OSError)):
                adapter.start()
        finally:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path
            adapter.stop()

        repo = self.ws.create_git_repo("doc_https_8443")
        res = self.ws.run_cli(
            "doctor",
            "--repo",
            str(repo),
            "--server",
            "https://127.0.0.1:8443",
            "--json",
        )
        report = json.loads(res.stdout)
        tcp_chk = next(c for c in report["checks"] if c["name"] == "hub_tcp")
        self.assertIn(":8443", tcp_chk["detail"])
        self.assertNotEqual(tcp_chk["status"], "ok")


# ---------------------------------------------------------------------------
# Feature F10: Multi-IDE Hook Proxy & Dedup Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF10HookProxyBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F10: Malformed/missing stdin fail-open vs fail-closed, protected files, host contracts & dedup TTL."""

    def test_f10_malformed_and_empty_stdin_fail_open_vs_fail_closed(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hp_malformed")

        for bad_stdin in ("{bad json", "", "null"):
            # Fail-open defaults
            r_ag = self.ws.run_cli(
                "hook-proxy", "--host", "antigravity", "--server", hub.base_url, "--agent", "a1", stdin_text=bad_stdin, cwd=repo
            )
            self.assertEqual(r_ag.returncode, 0, r_ag.stderr)
            self.assertEqual(json.loads(r_ag.stdout)["decision"], "allow")

            r_cl = self.ws.run_cli(
                "hook-proxy", "--host", "claude", "--server", hub.base_url, "--agent", "a1", stdin_text=bad_stdin, cwd=repo
            )
            self.assertEqual(r_cl.returncode, 0, r_cl.stderr)

            r_cu = self.ws.run_cli(
                "hook-proxy", "--host", "cursor", "--server", hub.base_url, "--agent", "a1", stdin_text=bad_stdin, cwd=repo
            )
            self.assertEqual(r_cu.returncode, 0, r_cu.stderr)
            cu_out = json.loads(r_cu.stdout)
            self.assertEqual(cu_out.get("decision") or cu_out.get("permission"), "allow")

            # --fail-closed rejects malformed/empty input
            fc_ag = self.ws.run_cli(
                "hook-proxy", "--host", "antigravity", "--fail-closed", "--server", hub.base_url, "--agent", "a1", stdin_text=bad_stdin, cwd=repo
            )
            self.assertEqual(fc_ag.returncode, 0, fc_ag.stderr)
            self.assertEqual(json.loads(fc_ag.stdout)["decision"], "deny")

            fc_cl = self.ws.run_cli(
                "hook-proxy", "--host", "claude", "--fail-closed", "--server", hub.base_url, "--agent", "a1", stdin_text=bad_stdin, cwd=repo
            )
            self.assertEqual(fc_cl.returncode, 2)
            self.assertTrue(fc_cl.stderr.strip())

            fc_cu = self.ws.run_cli(
                "hook-proxy", "--host", "cursor", "--fail-closed", "--server", hub.base_url, "--agent", "a1", stdin_text=bad_stdin, cwd=repo
            )
            self.assertEqual(fc_cu.returncode, 0, fc_cu.stderr)
            cu_fc_out = json.loads(fc_cu.stdout)
            self.assertEqual(cu_fc_out.get("decision") or cu_fc_out.get("permission"), "deny")

    def test_f10_missing_path_non_write_tool_and_outside_repo_allowed(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hp_bypass")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")
        outside_file = self.ws.ws_dir / "outside.txt"
        outside_file.write_text("outside\n", encoding="utf-8")

        # Missing path argument -> allow
        r_ag_nopath = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"toolCall": {"name": "edit_file", "args": {}}, "cwd": str(repo)}),
            cwd=repo,
        )
        self.assertEqual(r_ag_nopath.returncode, 0)
        self.assertEqual(json.loads(r_ag_nopath.stdout)["decision"], "allow")

        r_cl_nopath = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"tool_name": "Edit", "tool_input": {}, "cwd": str(repo)}),
            cwd=repo,
        )
        self.assertEqual(r_cl_nopath.returncode, 0)

        # Read-only tool targeting locked file -> allow
        r_read = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps(
                {"tool_name": "Read", "tool_input": {"file_path": str(repo / "src" / "locked.py")}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(r_read.returncode, 0)

        # Edit outside repo -> allow
        r_out = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps(
                {"tool_name": "Edit", "tool_input": {"file_path": str(outside_file)}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(r_out.returncode, 0)

        # Protected agsync metadata files -> deny across hosts
        for protected_rel in (".agsync-agent", ".agsync/token"):
            r_prot_ag = self.ws.run_cli(
                "hook-proxy",
                "--host",
                "antigravity",
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                stdin_text=json.dumps(
                    {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / protected_rel)}}, "cwd": str(repo)}
                ),
                cwd=repo,
            )
            self.assertEqual(r_prot_ag.returncode, 0)
            self.assertEqual(json.loads(r_prot_ag.stdout)["decision"], "deny")

            r_prot_cl = self.ws.run_cli(
                "hook-proxy",
                "--host",
                "claude",
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                stdin_text=json.dumps(
                    {"tool_name": "Write", "tool_input": {"file_path": str(repo / protected_rel)}, "cwd": str(repo)}
                ),
                cwd=repo,
            )
            self.assertEqual(r_prot_cl.returncode, 2)

    def test_f10_antigravity_exact_json_schema_for_allow_deny_ask(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hp_ag_schema")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")

        # allow
        r_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps(
                {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(r_allow.returncode, 0)
        j_allow = json.loads(r_allow.stdout)
        self.assertEqual(j_allow["decision"], "allow")

        # deny
        r_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps(
                {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "locked.py")}}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(r_deny.returncode, 0)
        j_deny = json.loads(r_deny.stdout)
        self.assertEqual(j_deny["decision"], "deny")
        self.assertTrue(j_deny.get("reason"))
        self.assertIn("agent-b", j_deny["reason"])
        self.assertIn("locked.py", j_deny["reason"])

        # --require-lease on unleased file -> ask or deny with non-empty reason
        r_req = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--require-lease",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps(
                {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(r_req.returncode, 0)
        j_req = json.loads(r_req.stdout)
        self.assertIn(j_req["decision"], ("ask", "deny"))
        self.assertTrue(j_req.get("reason"))

    def test_f10_claude_deny_exit2_stderr_and_snake_case_path_variants(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hp_cl_variants")
        _acquire_lease_http(hub.base_url, "agent-b", "src/api.py")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")

        for tool_name, key_name, rel_path in (
            ("NotebookEdit", "notebook_path", "src/api.py"),
            ("Write", "path", "src/locked.py"),
        ):
            res = self.ws.run_cli(
                "hook-proxy",
                "--host",
                "claude",
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                stdin_text=json.dumps(
                    {"tool_name": tool_name, "tool_input": {key_name: str(repo / rel_path)}, "cwd": str(repo)}
                ),
                cwd=repo,
            )
            self.assertEqual(res.returncode, 2, f"Expected exit 2 for {tool_name}/{key_name}: {res.stdout} {res.stderr}")
            self.assertIn("agent-b", res.stderr)
            self.assertEqual(res.stdout.strip(), "")

        # Unreachable server: fail-open (0) vs --fail-closed (2 + stderr)
        unreach_payload = json.dumps(
            {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "free.py")}, "cwd": str(repo)}
        )
        r_open = self.ws.run_cli(
            "hook-proxy", "--host", "claude", "--server", "http://127.0.0.1:9", "--agent", "agent-a", stdin_text=unreach_payload, cwd=repo
        )
        self.assertEqual(r_open.returncode, 0)
        r_closed = self.ws.run_cli(
            "hook-proxy", "--host", "claude", "--fail-closed", "--server", "http://127.0.0.1:9", "--agent", "agent-a", stdin_text=unreach_payload, cwd=repo
        )
        self.assertEqual(r_closed.returncode, 2)
        self.assertTrue(r_closed.stderr.strip())

    def test_f10_cursor_non_pretooluse_invalid_payload_and_unknown_host(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hp_cursor_events")
        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")

        for event_name in ("afterFileEdit", "stop"):
            res = self.ws.run_cli(
                "hook-proxy",
                "--host",
                "cursor",
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                stdin_text=json.dumps(
                    {
                        "hook_event_name": event_name,
                        "tool_name": "edit_file",
                        "tool_input": {"file_path": str(repo / "src" / "locked.py")},
                        "cwd": str(repo),
                    }
                ),
                cwd=repo,
            )
            self.assertEqual(res.returncode, 0)
            out = json.loads(res.stdout)
            self.assertEqual(out.get("decision") or out.get("permission"), "allow")

        res_empty = self.ws.run_cli(
            "hook-proxy", "--host", "cursor", "--server", hub.base_url, "--agent", "agent-a", stdin_text="{}", cwd=repo
        )
        self.assertEqual(res_empty.returncode, 0)
        out_empty = json.loads(res_empty.stdout)
        self.assertEqual(out_empty.get("decision") or out_empty.get("permission"), "allow")

        res_unk = self.ws.run_cli("hook-proxy", "--host", "windsurf", "--server", hub.base_url, stdin_text="{}")
        self.assertNotEqual(res_unk.returncode, 0)
        self.assertTrue(res_unk.stderr.strip())

    def test_f10_cursor_dedup_cross_file_isolation_and_stale_ttl_expiry(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("hp_dedup_ttl")
        runs_log = repo / "runs.log"
        user_hook = repo / "count_hook.py"
        user_hook.write_text(
            f"from pathlib import Path\nwith Path({str(runs_log)!r}).open('a', encoding='utf-8') as f:\n    f.write('1\\n')\n",
            encoding="utf-8",
        )
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"matcher": "*", "hooks": [{"type": "command", "command": f'"{sys.executable}" "{user_hook}"'}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        r_inst = self.ws.run_cli(
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
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        def _run_hook(host: str, rel_file: str) -> None:
            ev_name = "preToolUse" if host == "cursor" else "PreToolUse"
            res = self.ws.run_cli(
                "hook-proxy",
                "--host",
                host,
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                stdin_text=json.dumps(
                    {
                        "hook_event_name": ev_name,
                        "tool_name": "Edit",
                        "tool_input": {"file_path": str(repo / rel_file)},
                        "cwd": str(repo),
                        "cursor_version": "0.50.0",
                    }
                ),
                cwd=repo,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

        _run_hook("cursor", "src/free.py")
        self.assertTrue(runs_log.is_file(), "Chained hook never executed on initial cursor invocation")
        self.assertEqual(len(runs_log.read_text(encoding="utf-8").splitlines()), 1)

        # Different file must NOT falsely dedup
        _run_hook("cursor", "src/app.py")
        self.assertEqual(len(runs_log.read_text(encoding="utf-8").splitlines()), 2)

        # Immediate Claude hook on free.py MUST dedup against cursor marker (count stays 2)
        _run_hook("claude", "src/free.py")
        self.assertEqual(len(runs_log.read_text(encoding="utf-8").splitlines()), 2)

        # Backdate dedup markers by 120s (both mtime and any JSON timestamp fields) to expire TTL
        dedup_dir = repo / ".agsync" / "hook-dedup"
        self.assertTrue(dedup_dir.is_dir())
        stale_ts = time.time() - 120.0
        for marker in dedup_dir.iterdir():
            if marker.is_file():
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        for k in ("ts", "timestamp", "created_at", "expires_at"):
                            if k in data and isinstance(data[k], (int, float)):
                                data[k] = stale_ts
                        marker.write_text(json.dumps(data), encoding="utf-8")
                except (ValueError, OSError):
                    pass
                os.utime(marker, (stale_ts, stale_ts))

        _run_hook("claude", "src/free.py")
        self.assertEqual(len(runs_log.read_text(encoding="utf-8").splitlines()), 3)


# ---------------------------------------------------------------------------
# Feature F11: `--record` & Safe Hook Chaining Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF11RecordAndChainingBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F11: Secret redaction in `--record`, chained errors/crashes, 3-level tree-kill, concurrency & stdout isolation."""

    def test_f11_record_redacts_nested_secrets_headers_and_env_tokens(self) -> None:
        cli_secret = "cli-secret-tok-991"
        env_secret = "env-secret-tok-992"
        hdr_secret = "hdr-secret-tok-993"
        nest_auth = "nest-auth-tok-994"
        nest_xkey = "nest-xkey-tok-995"
        tok_secret = "body-tok-996"
        api_secret = "body-apikey-997"
        pwd_secret = "body-pass-998"

        hub = self.ws.start_hub(token=cli_secret)
        repo = self.ws.create_git_repo("rec_redact")
        payload = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
                "authorization": f"Bearer {hdr_secret}",
                "headers": {
                    "Authorization": f"Bearer {nest_auth}",
                    "x-api-key": nest_xkey,
                },
                "token": tok_secret,
                "api_key": api_secret,
                "password": pwd_secret,
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
            cli_secret,
            env_overrides={"AGSYNC_TOKEN": env_secret},
            stdin_text=payload,
            cwd=repo,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        samples = list((repo / ".agsync" / "hook-samples").glob("*.json"))
        self.assertGreaterEqual(len(samples), 1, "Expected recorded sample JSON in .agsync/hook-samples/")
        raw_text = samples[0].read_text(encoding="utf-8")
        for secret in (cli_secret, env_secret, hdr_secret, nest_auth, nest_xkey, tok_secret, api_secret, pwd_secret):
            self.assertNotIn(secret, raw_text, f"Secret {secret} leaked into hook sample!")
        parsed = json.loads(raw_text)
        self.assertIn("decision", parsed)
        self.assertIn("tool_name", json.dumps(parsed))

    def test_f11_chained_user_hook_nonzero_exit_propagates_decision(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("chain_user_deny")

        cl_hook = repo / "cl_deny.py"
        cl_hook.write_text("import sys\nsys.stderr.write('blocked by user linter\\n')\nsys.exit(2)\n", encoding="utf-8")
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"matcher": "*", "hooks": [{"type": "command", "command": f'"{sys.executable}" "{cl_hook}"'}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        ag_hook = repo / "ag_deny.py"
        ag_hook.write_text(
            'import json, sys\nprint(json.dumps({"decision": "deny", "reason": "user ag hook denied"}))\nsys.exit(1)\n',
            encoding="utf-8",
        )
        (repo / ".agents").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "hooks.json").write_text(
            json.dumps(
                {
                    "user-pretool-guard": {
                        "PreToolUse": [
                            {
                                "matcher": "edit_file|write_file",
                                "hooks": [{"type": "command", "command": f'"{sys.executable}" "{ag_hook}"', "timeout": 5}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        r_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-claude-hook",
            "--with-antigravity-hook",
        )
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        r_cl = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "free.py")}, "cwd": str(repo)}),
            cwd=repo,
        )
        self.assertEqual(r_cl.returncode, 2)
        self.assertIn("blocked by user linter", r_cl.stderr)

        r_ag = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}),
            cwd=repo,
        )
        self.assertEqual(r_ag.returncode, 0)
        self.assertEqual(json.loads(r_ag.stdout)["decision"], "deny")

    def test_f11_missing_or_crashing_chained_hook_handled_gracefully(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("chain_crash")
        crash_hook = repo / "crash_hook.py"
        crash_hook.write_text("raise RuntimeError('boom in user hook')\n", encoding="utf-8")

        (repo / ".agents").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "hooks.json").write_text(
            json.dumps(
                {
                    "user-missing-guard": {
                        "PreToolUse": [
                            {
                                "matcher": "edit_file|write_file",
                                "hooks": [
                                    {"type": "command", "command": str(repo / "nonexistent_hook_bin"), "timeout": 5},
                                    {"type": "command", "command": f'"{sys.executable}" "{crash_hook}"', "timeout": 5},
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        r_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-antigravity-hook",
        )
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        r_free = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}),
            cwd=repo,
        )
        self.assertEqual(r_free.returncode, 0)
        self.assertNotIn("Traceback", r_free.stdout)
        j_free = json.loads(r_free.stdout)
        self.assertIn(j_free["decision"], ("allow", "deny"))

        _acquire_lease_http(hub.base_url, "agent-b", "src/locked.py")
        r_locked = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "locked.py")}}, "cwd": str(repo)}),
            cwd=repo,
        )
        self.assertEqual(r_locked.returncode, 0)
        j_locked = json.loads(r_locked.stdout)
        self.assertEqual(j_locked["decision"], "deny")
        self.assertIn("agent-b", j_locked.get("reason", ""))

    def test_f11_chain_timeout_0_3_kills_grandchild_process_tree_no_orphans(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("chain_3level")
        pids_file = repo / "pids.json"
        grandchild_code = "import time; time.sleep(30)"
        child_code = (
            "import json, os, subprocess, sys, time; "
            f"gc = subprocess.Popen([{sys.executable!r}, '-S', '-c', {grandchild_code!r}]); "
            "print(json.dumps([os.getpid(), gc.pid]), flush=True); "
            "time.sleep(30)"
        )
        top_hook = repo / "tree_hook.py"
        top_hook.write_text(
            (
                "import json, os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                f"c = subprocess.Popen([sys.executable, '-S', '-c', {child_code!r}], stdout=subprocess.PIPE, text=True)\n"
                "child_pids = json.loads(c.stdout.readline())\n"
                f"tmp = Path({str(pids_file)!r} + '.tmp')\n"
                "tmp.write_text(json.dumps([os.getpid(), *child_pids]), encoding='utf-8')\n"
                f"os.replace(tmp, Path({str(pids_file)!r}))\n"
                "time.sleep(30)\n"
            ),
            encoding="utf-8",
        )
        (repo / ".agents").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "hooks.json").write_text(
            json.dumps(
                {
                    "user-tree-guard": {
                        "PreToolUse": [
                            {
                                "matcher": "edit_file|write_file",
                                "hooks": [{"type": "command", "command": f'"{sys.executable}" -S "{top_hook}"', "timeout": 30}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        r_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-antigravity-hook",
        )
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        t0 = time.monotonic()
        res = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--chain-timeout",
            "0.6",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}),
            cwd=repo,
            timeout=15.0,
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 4.0, f"hook-proxy exceeded chain-timeout budget ({elapsed:.2f}s)")
        json.loads(res.stdout)
        self.assertTrue(pids_file.is_file(), "3-level tree hook failed to write pids.json before timeout")
        pids = json.loads(pids_file.read_text(encoding="utf-8"))
        self.assertEqual(len(set(pids)), 3, f"Expected 3 distinct PIDs, got {pids}")
        for pid in pids:
            self.assertTrue(_poll_pid_dead(int(pid), timeout=2.5), f"Orphan PID {pid} survived tree-kill")

    def test_f11_concurrent_record_writes_do_not_corrupt_json(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("rec_concurrent")
        n_threads = 8
        barrier = threading.Barrier(n_threads, timeout=5.0)
        results: list[tuple[int, str]] = [(1, "")] * n_threads

        def _worker(idx: int) -> None:
            barrier.wait()
            res = self.ws.run_cli(
                "hook-proxy",
                "--host",
                "antigravity",
                "--record",
                "--server",
                hub.base_url,
                "--agent",
                f"rec-{idx}",
                stdin_text=json.dumps(
                    {
                        "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}},
                        "cwd": str(repo),
                        "seq": idx,
                    }
                ),
                cwd=repo,
            )
            results[idx] = (res.returncode, res.stdout)

        threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20.0)
            self.assertFalse(t.is_alive())

        for rc, out in results:
            self.assertEqual(rc, 0)
            json.loads(out)

        samples = list((repo / ".agsync" / "hook-samples").glob("*.json"))
        self.assertGreaterEqual(len(samples), n_threads, f"Expected >= {n_threads} samples, found {len(samples)}")
        for sp in samples:
            parsed = json.loads(sp.read_text(encoding="utf-8"))
            self.assertIsInstance(parsed, dict)

    def test_f11_chained_hook_noisy_stdout_and_timeout_never_corrupts_proxy_json(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("chain_noisy")
        noisy_hook = repo / "noisy.py"
        noisy_hook.write_text("import sys, time\nprint('GARBAGE NOT JSON', flush=True)\ntime.sleep(10)\n", encoding="utf-8")

        (repo / ".agents").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "hooks.json").write_text(
            json.dumps(
                {
                    "user-noisy-ag": {
                        "PreToolUse": [
                            {
                                "matcher": "edit_file|write_file",
                                "hooks": [{"type": "command", "command": f'"{sys.executable}" -S "{noisy_hook}"', "timeout": 10}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (repo / ".cursor").mkdir(parents=True, exist_ok=True)
        (repo / ".cursor" / "hooks.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {"preToolUse": [{"command": f'"{sys.executable}" -S "{noisy_hook}"'}]},
                }
            ),
            encoding="utf-8",
        )
        r_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            "--with-antigravity-hook",
            "--with-cursor-hook",
        )
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        r_ag = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--chain-timeout",
            "0.2",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps({"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}),
            cwd=repo,
        )
        self.assertNotIn("GARBAGE NOT JSON", r_ag.stdout)
        json.loads(r_ag.stdout)

        r_cu = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--chain-timeout",
            "0.2",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=json.dumps(
                {
                    "hook_event_name": "preToolUse",
                    "tool_name": "edit_file",
                    "tool_input": {"file_path": str(repo / "src" / "free.py")},
                    "cwd": str(repo),
                }
            ),
            cwd=repo,
        )
        self.assertNotIn("GARBAGE NOT JSON", r_cu.stdout)
        json.loads(r_cu.stdout)


# ---------------------------------------------------------------------------
# Feature F12: Expanded `setup-mcp`, `install-hooks`, `doctor` & Templates (6 tests)
# ---------------------------------------------------------------------------


class TestF12SetupHooksTemplatesBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F12: Corrupt JSON protection, UTF-16 BOM merge, idempotency, bad flags, doctor & template validation."""

    def test_f12_invalid_json_in_preexisting_ide_config_preserves_file_without_loss(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("bad_json_cfgs")
        corrupt = "{broken json"

        ag_cfg = self.ws.home_dir / ".gemini" / "config" / "mcp_config.json"
        claude_cfg = repo / ".mcp.json"
        cursor_cfg = self.ws.home_dir / ".cursor" / "mcp.json"
        for ide, cfg_path in (("antigravity", ag_cfg), ("claude", claude_cfg), ("cursor", cursor_cfg)):
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(corrupt, encoding="utf-8")
            res = self.ws.run_cli(
                "setup-mcp", "--ide", ide, "--repo", str(repo), "--server", hub.base_url, "--agent", "a1", cwd=repo
            )
            self.assertNotEqual(res.returncode, 0, f"setup-mcp --ide {ide} should reject corrupt JSON")
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), corrupt)

        cl_hook = repo / ".claude" / "settings.json"
        cu_hook = repo / ".cursor" / "hooks.json"
        for flag, hpath in (("--with-claude-hook", cl_hook), ("--with-cursor-hook", cu_hook)):
            hpath.parent.mkdir(parents=True, exist_ok=True)
            hpath.write_text(corrupt, encoding="utf-8")
            res_h = self.ws.run_cli(
                "install-hooks", "--repo", str(repo), "--server", hub.base_url, "--agent", "a1", flag
            )
            self.assertNotEqual(res_h.returncode, 0, f"install-hooks {flag} should reject corrupt JSON")
            self.assertEqual(hpath.read_text(encoding="utf-8"), corrupt)

        # Valid UTF-16 BOM .claude/settings.json merges cleanly
        cl_hook.write_bytes(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo keep"}]}]
                    }
                }
            ).encode("utf-16")
        )
        res_utf16 = self.ws.run_cli(
            "install-hooks", "--repo", str(repo), "--server", hub.base_url, "--agent", "a1", "--with-claude-hook"
        )
        self.assertEqual(res_utf16.returncode, 0, res_utf16.stderr or res_utf16.stdout)
        self.assertTrue((cl_hook.parent / "settings.json.bak").is_file())
        merged = json.loads(cl_hook.read_text(encoding="utf-8"))
        self.assertIn("echo keep", json.dumps(merged))
        self.assertIn("hook-proxy", json.dumps(merged))

    def test_f12_idempotent_reruns_setup_mcp_and_install_hooks_no_duplicates(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("idemp_setup")

        ag_cfg = self.ws.home_dir / ".gemini" / "config" / "mcp_config.json"
        claude_cfg = repo / ".mcp.json"
        cursor_cfg = self.ws.home_dir / ".cursor" / "mcp.json"
        for p in (ag_cfg, claude_cfg, cursor_cfg):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"mcpServers": {"existing-tool": {"command": "echo"}}}), encoding="utf-8")

        for mode in ("http", "http", "stdio", "http"):
            res = self.ws.run_cli(
                "setup-mcp",
                "--ide",
                "all",
                "--mode",
                mode,
                "--repo",
                str(repo),
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                cwd=repo,
            )
            self.assertEqual(res.returncode, 0, res.stderr or res.stdout)

        for p in (ag_cfg, claude_cfg, cursor_cfg):
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("existing-tool", data["mcpServers"])
            entry = data["mcpServers"]["agsync"]
            self.assertFalse("url" in entry and "serverUrl" in entry, f"Conflicting url+serverUrl in {p}")
            self.assertTrue((p.parent / f"{p.name}.bak").is_file(), f"Missing .bak for {p}")

        for _ in range(3):
            res_h = self.ws.run_cli(
                "install-hooks",
                "--repo",
                str(repo),
                "--server",
                hub.base_url,
                "--agent",
                "agent-a",
                "--with-all-hooks",
            )
            self.assertEqual(res_h.returncode, 0, res_h.stderr or res_h.stdout)

        for hp in (repo / ".agents" / "hooks.json", repo / ".claude" / "settings.json", repo / ".cursor" / "hooks.json"):
            self.assertTrue(hp.is_file(), f"Missing hook file {hp}")
            txt = hp.read_text(encoding="utf-8")
            self.assertEqual(txt.count("hook-proxy"), 1, f"Duplicate agsync hook entry in {hp}: {txt}")

        exclude_lines = [
            ln.strip() for ln in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        self.assertLessEqual(exclude_lines.count(".agsync/"), 1)
        self.assertLessEqual(exclude_lines.count(".agsync-agent"), 1)

    def test_f12_setup_mcp_unknown_ide_and_invalid_server_url_rejected(self) -> None:
        repo = self.ws.create_git_repo("bad_setup_flags")
        ag_cfg = self.ws.home_dir / ".gemini" / "config" / "mcp_config.json"

        cases = [
            ("setup-mcp", "--ide", "nonexistent-ide", "--repo", str(repo), "--server", "http://127.0.0.1:8765"),
            ("setup-mcp", "--ide", "antigravity", "--repo", str(repo), "--server", "ftp://invalid.host"),
            ("setup-mcp", "--ide", "antigravity", "--repo", str(repo), "--server", "http://127.0.0.1:8765", "--agent", "*"),
        ]
        for args in cases:
            res = self.ws.run_cli(*args, cwd=repo)
            self.assertNotEqual(res.returncode, 0, f"Expected failure for {args}")
            self.assertTrue((res.stderr or res.stdout).strip())
        self.assertFalse(ag_cfg.exists(), "Invalid setup-mcp invocation created mcp_config.json")

    def test_f12_doctor_detects_broken_hooks_legacy_mcp_and_stale_server_url(self) -> None:
        repo = self.ws.create_git_repo("doctor_broken")

        # Find an unused ephemeral port deterministically
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            unused_port = int(s.getsockname()[1])
        finally:
            s.close()

        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text("{corrupt", encoding="utf-8")
        (repo / ".cursor").mkdir(parents=True, exist_ok=True)
        (repo / ".cursor" / "hooks.json").write_text("{corrupt", encoding="utf-8")

        ag_cfg = self.ws.home_dir / ".gemini" / "config" / "mcp_config.json"
        ag_cfg.parent.mkdir(parents=True, exist_ok=True)
        ag_cfg.write_text(
            json.dumps({"mcpServers": {"agsync": {"serverUrl": f"http://127.0.0.1:{unused_port}/sse"}}}),
            encoding="utf-8",
        )

        res = self.ws.run_cli(
            "doctor", "--repo", str(repo), "--server", f"http://127.0.0.1:{unused_port}", "--json", cwd=repo
        )
        self.assertEqual(res.returncode, 1, res.stdout or res.stderr)
        report = json.loads(res.stdout)
        self.assertFalse(report["ok"])
        checks = report["checks"]
        self.assertTrue(
            any(c["status"] == "fail" and ("claude" in c["name"].lower() or "cursor" in c["name"].lower() or "hook" in c["name"].lower()) for c in checks),
            checks,
        )
        self.assertTrue(
            any(c["status"] in ("warn", "fail") and ("sse" in c.get("detail", "").lower() or "serverurl" in c.get("detail", "").lower() or "mcp" in c["name"].lower()) for c in checks),
            checks,
        )
        tcp_chk = next(c for c in checks if c["name"] == "hub_tcp")
        self.assertIn(tcp_chk["status"], ("warn", "fail"))

    def test_f12_install_hooks_refuses_foreign_precommit_without_force_and_overwrites_with_force(self) -> None:
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("foreign_precommit")
        precommit = repo / ".git" / "hooks" / "pre-commit"
        precommit.parent.mkdir(parents=True, exist_ok=True)
        custom_hook = "#!/bin/sh\necho custom-linter\nexit 0\n"
        precommit.write_text(custom_hook, encoding="utf-8")

        res_no = self.ws.run_cli(
            "install-hooks", "--repo", str(repo), "--server", hub.base_url, "--agent", "a1"
        )
        self.assertNotEqual(res_no.returncode, 0)
        self.assertEqual(precommit.read_text(encoding="utf-8"), custom_hook)

        res_yes = self.ws.run_cli(
            "install-hooks", "--repo", str(repo), "--server", hub.base_url, "--agent", "a1", "--force"
        )
        self.assertEqual(res_yes.returncode, 0, res_yes.stderr or res_yes.stdout)
        self.assertIn("agsync", precommit.read_text(encoding="utf-8").lower())

    def test_f12_all_templates_parse_cleanly_zero_placeholder_or_legacy_bugs(self) -> None:
        tdir = REPO_ROOT / "templates"
        files = [p for p in tdir.rglob("*") if p.is_file()]
        self.assertGreater(len(files), 4)

        combined = ""
        for f in files:
            text = f.read_text(encoding="utf-8")
            combined += f"\n{f.as_posix()}\n{text}\n"
            for bad_tok in ("/sse", "serverUrl", "TODO", "FIXME", "{{", "}}"):
                self.assertNotIn(bad_tok, text, f"Forbidden token {bad_tok!r} in {f}")
            if f.suffix.lower() == ".json":
                parsed = json.loads(text)
                self.assertIsInstance(parsed, (dict, list))

        low = combined.lower()
        for ide in ("windsurf", "codex", "gemini", "claude", "cursor"):
            self.assertIn(ide, low, f"Missing template coverage for {ide}")
        for ep in ("/mcp", "/events", "/leases/acquire", "/tasks", "/messages"):
            self.assertIn(ep, combined, f"Missing canonical endpoint {ep} in templates/")
        self.assertIn("8765", combined)


# ---------------------------------------------------------------------------
# Feature F13: Opt-In `git worktree` Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF13WorktreeBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F13: Duplicate/invalid targets, non-git errors, junctions & safe removal, dirty worktrees."""

    def test_f13_duplicate_agent_id_existing_target_dir_and_invalid_agent_rejected(self) -> None:
        proj = self.ws.create_git_repo("wt_dup")
        r1 = self.ws.run_cli("worktree", "create", "agent-a", "--repo", str(proj))
        self.assertEqual(r1.returncode, 0, r1.stderr or r1.stdout)
        wt1 = self.ws.ws_dir / "wt_dup-wt-1"
        self.assertEqual((wt1 / ".agsync-agent").read_text(encoding="utf-8").strip(), "agent-a")

        r_dup = self.ws.run_cli("worktree", "create", "agent-a", "--repo", str(proj))
        self.assertNotEqual(r_dup.returncode, 0)
        self.assertEqual((wt1 / ".agsync-agent").read_text(encoding="utf-8").strip(), "agent-a")

        occupied = self.ws.ws_dir / "occupied_dir"
        occupied.mkdir(parents=True, exist_ok=True)
        (occupied / "keep.txt").write_text("KEEP", encoding="utf-8")
        r_occ = self.ws.run_cli("worktree", "create", "agent-b", "--repo", str(proj), "--path", str(occupied))
        self.assertNotEqual(r_occ.returncode, 0)
        self.assertEqual((occupied / "keep.txt").read_text(encoding="utf-8"), "KEEP")

        for bad_agent in ("*", "../escape"):
            r_bad = self.ws.run_cli("worktree", "create", bad_agent, "--repo", str(proj))
            self.assertNotEqual(r_bad.returncode, 0, f"Invalid agent_id {bad_agent!r} accepted")

    def test_f13_worktree_commands_in_invalid_or_missing_git_repo_fail_cleanly(self) -> None:
        plain_dir = self.ws.ws_dir / "not_a_git_repo"
        plain_dir.mkdir(parents=True, exist_ok=True)
        missing_dir = self.ws.ws_dir / "does_not_exist"

        for repo_arg in (plain_dir, missing_dir):
            for cmd in (
                ("worktree", "create", "a1", "--repo", str(repo_arg)),
                ("worktree", "list", "--repo", str(repo_arg)),
                ("worktree", "remove", "a1", "--repo", str(repo_arg)),
            ):
                res = self.ws.run_cli(*cmd)
                self.assertNotEqual(res.returncode, 0, f"Expected failure for {cmd}")
                self.assertNotIn("Traceback (most recent call last)", res.stderr)

    def test_f13_no_junctions_and_missing_shared_cache_dirs(self) -> None:
        proj1 = self.ws.create_git_repo("wt_nojunc")
        (proj1 / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (proj1 / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
        (proj1 / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (proj1 / ".venv" / "Scripts" / "p.txt").write_text("v", encoding="utf-8")

        r_no = self.ws.run_cli("worktree", "create", "agent-nj", "--repo", str(proj1), "--no-junctions")
        self.assertEqual(r_no.returncode, 0, r_no.stderr or r_no.stdout)
        wt_nj = self.ws.ws_dir / "wt_nojunc-wt-1"
        self.assertTrue(wt_nj.is_dir())
        self.assertFalse((wt_nj / "node_modules").exists())
        self.assertFalse((wt_nj / ".venv").exists())

        proj2 = self.ws.create_git_repo("wt_nocache")
        r_ok = self.ws.run_cli("worktree", "create", "agent-nc", "--repo", str(proj2))
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr or r_ok.stdout)
        self.assertTrue((self.ws.ws_dir / "wt_nocache-wt-1").is_dir())

    def test_f13_remove_nonexistent_worktree_and_refuse_removing_main_repo(self) -> None:
        proj = self.ws.create_git_repo("wt_rm_guard")
        r_ghost = self.ws.run_cli("worktree", "remove", "ghost-agent", "--repo", str(proj))
        self.assertNotEqual(r_ghost.returncode, 0)

        r_main = self.ws.run_cli("worktree", "remove", str(proj), "--repo", str(proj))
        self.assertNotEqual(r_main.returncode, 0)
        self.assertTrue((proj / ".git").exists())
        self.assertTrue((proj / "README.md").is_file())

    def test_f13_remove_worktree_with_active_windows_junctions_preserves_target_files(self) -> None:
        proj = self.ws.create_git_repo("wt_junc_rm")
        (proj / "node_modules" / "deep").mkdir(parents=True, exist_ok=True)
        (proj / "node_modules" / "deep" / "lib.js").write_text("LIB_KEEP", encoding="utf-8")
        (proj / ".venv" / "Lib").mkdir(parents=True, exist_ok=True)
        (proj / ".venv" / "Lib" / "site.py").write_text("VENV_KEEP", encoding="utf-8")

        r_c = self.ws.run_cli("worktree", "create", "agent-jrm", "--repo", str(proj))
        self.assertEqual(r_c.returncode, 0, r_c.stderr or r_c.stdout)
        wt1 = self.ws.ws_dir / "wt_junc_rm-wt-1"
        self.assertEqual((wt1 / "node_modules" / "deep" / "lib.js").read_text(encoding="utf-8"), "LIB_KEEP")
        self.assertEqual((wt1 / ".venv" / "Lib" / "site.py").read_text(encoding="utf-8"), "VENV_KEEP")
        if os.name == "nt":
            self.assertTrue(os.path.isjunction(wt1 / "node_modules"))
            self.assertTrue(os.path.isjunction(wt1 / ".venv"))

        r_rm = self.ws.run_cli("worktree", "remove", "agent-jrm", "--repo", str(proj))
        self.assertEqual(r_rm.returncode, 0, r_rm.stderr or r_rm.stdout)
        self.assertFalse(wt1.exists())
        self.assertEqual((proj / "node_modules" / "deep" / "lib.js").read_text(encoding="utf-8"), "LIB_KEEP")
        self.assertEqual((proj / ".venv" / "Lib" / "site.py").read_text(encoding="utf-8"), "VENV_KEEP")

    def test_f13_dirty_worktree_refuses_remove_without_force_and_succeeds_with_force(self) -> None:
        proj = self.ws.create_git_repo("wt_dirty")
        (proj / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (proj / "node_modules" / "pkg" / "index.js").write_text("KEEP_DIRTY", encoding="utf-8")

        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-d", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "wt_dirty-wt-1"
        untracked = wt1 / "untracked.txt"
        untracked.write_text("dirty\n", encoding="utf-8")

        r_no = self.ws.run_cli("worktree", "remove", "agent-d", "--repo", str(proj))
        self.assertNotEqual(r_no.returncode, 0)
        self.assertTrue(wt1.is_dir() and untracked.is_file())

        r_yes = self.ws.run_cli("worktree", "remove", "agent-d", "--repo", str(proj), "--force")
        self.assertEqual(r_yes.returncode, 0, r_yes.stderr or r_yes.stdout)
        self.assertFalse(wt1.exists())
        self.assertEqual((proj / "node_modules" / "pkg" / "index.js").read_text(encoding="utf-8"), "KEEP_DIRTY")


# ---------------------------------------------------------------------------
# Feature F14: Multi-Worktree Identity Guard Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF14IdentityGuardBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F14: Missing/whitespace `.agsync-agent`, precedence (`--agent` > `AGSYNC_AGENT`), worktree removal & doctor."""

    def test_f14_linked_worktree_missing_identity_blocks_guard_and_hook_proxy(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("id_missing")
        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-w", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "id_missing-wt-1"
        (wt1 / ".agsync-agent").unlink(missing_ok=True)
        (wt1 / ".agsync" / "agent_id").unlink(missing_ok=True)

        r_guard = self.ws.run_cli("guard", "--server", hub.base_url, cwd=wt1)
        self.assertNotEqual(r_guard.returncode, 0)
        self.assertRegex((r_guard.stdout + r_guard.stderr).lower(), r"\.agsync-agent|identity|ambiguous|agent")

        r_ag = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            stdin_text=json.dumps({"toolCall": {"name": "edit_file", "args": {"Path": str(wt1 / "src" / "app.py")}}, "cwd": str(wt1)}),
            cwd=wt1,
        )
        self.assertEqual(r_ag.returncode, 0)
        ag_out = json.loads(r_ag.stdout)
        self.assertEqual(ag_out["decision"], "deny")
        self.assertRegex(ag_out.get("reason", "").lower(), r"\.agsync-agent|identity|ambiguous|agent")

        r_cl = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            stdin_text=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(wt1 / "src" / "app.py")}, "cwd": str(wt1)}),
            cwd=wt1,
        )
        self.assertEqual(r_cl.returncode, 2)
        self.assertRegex(r_cl.stderr.lower(), r"\.agsync-agent|identity|ambiguous|agent")

    def test_f14_whitespace_only_agsync_agent_treated_as_missing_in_multi_worktree(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("id_ws")
        self.assertEqual(self.ws.run_cli("worktree", "create", "agent-w", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "id_ws-wt-1"
        (wt1 / ".agsync-agent").write_text("   \n\t\n", encoding="utf-8")

        r_lock = self.ws.run_cli("lock", "src/app.py", "--server", hub.base_url, cwd=wt1)
        self.assertNotEqual(r_lock.returncode, 0)

        r_cl = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            stdin_text=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(wt1 / "src" / "app.py")}, "cwd": str(wt1)}),
            cwd=wt1,
        )
        self.assertEqual(r_cl.returncode, 2)

    def test_f14_explicit_agent_flag_overrides_missing_and_file_identity(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("id_flag")
        self.assertEqual(self.ws.run_cli("worktree", "create", "file-agent", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "id_flag-wt-1"

        r_over_file = self.ws.run_cli(
            "lock", "src/app.py", "--server", hub.base_url, "--agent", "flag-overrides-file", cwd=wt1
        )
        self.assertEqual(r_over_file.returncode, 0, r_over_file.stderr or r_over_file.stdout)

        (wt1 / ".agsync-agent").unlink(missing_ok=True)
        r_over_missing = self.ws.run_cli(
            "lock", "src/api.py", "--server", hub.base_url, "--agent", "flag-overrides-missing", cwd=wt1
        )
        self.assertEqual(r_over_missing.returncode, 0, r_over_missing.stderr or r_over_missing.stdout)

        st, _, leases = http_json("GET", f"{hub.base_url}/leases")
        self.assertEqual(st, 200)
        owners = {l["owner_id"] for l in leases["leases"]}
        self.assertIn("flag-overrides-file", owners)
        self.assertIn("flag-overrides-missing", owners)

    def test_f14_agsync_agent_env_overrides_missing_file_in_cli_and_hook_proxy(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("id_env")
        self.assertEqual(self.ws.run_cli("worktree", "create", "wt-init", "--repo", str(proj)).returncode, 0)
        wt1 = self.ws.ws_dir / "id_env-wt-1"
        (wt1 / ".agsync-agent").unlink(missing_ok=True)

        r_lock = self.ws.run_cli(
            "lock", "src/app.py", "--server", hub.base_url, env_overrides={"AGSYNC_AGENT": "env-wt-1"}, cwd=wt1
        )
        self.assertEqual(r_lock.returncode, 0, r_lock.stderr or r_lock.stdout)

        payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(wt1 / "src" / "app.py")}, "cwd": str(wt1)})
        r_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--require-lease",
            "--server",
            hub.base_url,
            env_overrides={"AGSYNC_AGENT": "env-wt-2"},
            stdin_text=payload,
            cwd=wt1,
        )
        self.assertEqual(r_deny.returncode, 2)

        r_ok = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--require-lease",
            "--server",
            hub.base_url,
            env_overrides={"AGSYNC_AGENT": "env-wt-1"},
            stdin_text=payload,
            cwd=wt1,
        )
        self.assertEqual(r_ok.returncode, 0)

    def test_f14_removing_extra_worktree_restores_single_repo_default_behavior(self) -> None:
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("id_restore")
        self.assertEqual(self.ws.run_cli("worktree", "create", "wt-temp", "--repo", str(proj)).returncode, 0)
        (proj / ".agsync-agent").unlink(missing_ok=True)
        (proj / ".agsync" / "agent_id").unlink(missing_ok=True)

        r_fail = self.ws.run_cli("lock", "src/app.py", "--server", hub.base_url, cwd=proj)
        self.assertNotEqual(r_fail.returncode, 0)

        self.assertEqual(self.ws.run_cli("worktree", "remove", "wt-temp", "--repo", str(proj)).returncode, 0)
        r_chk = self.ws.run_cli("check", "src/app.py", "--server", hub.base_url, cwd=proj)
        self.assertEqual(r_chk.returncode, 0, r_chk.stderr or r_chk.stdout)
        r_ok = self.ws.run_cli("lock", "src/app.py", "--server", hub.base_url, cwd=proj)
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr or r_ok.stdout)

    def test_f14_doctor_flags_missing_identity_on_main_repo_in_multi_worktree(self) -> None:
        proj = self.ws.create_git_repo("id_doc_main")
        self.assertEqual(self.ws.run_cli("worktree", "create", "wt-peer", "--repo", str(proj)).returncode, 0)
        (proj / ".agsync-agent").unlink(missing_ok=True)

        r_bad = self.ws.run_cli("doctor", "--repo", str(proj), "--json", cwd=proj)
        rep_bad = json.loads(r_bad.stdout)
        id_chks = [
            c for c in rep_bad["checks"]
            if "identity" in c["name"].lower() or "worktree" in c["name"].lower() or ".agsync-agent" in c.get("detail", "")
        ]
        self.assertGreaterEqual(len(id_chks), 1, rep_bad["checks"])
        self.assertTrue(any(c["status"] in ("warn", "fail") for c in id_chks), id_chks)

        (proj / ".agsync-agent").write_text("main-agent\n", encoding="utf-8")
        r_ok = self.ws.run_cli("doctor", "--repo", str(proj), "--json", cwd=proj)
        rep_ok = json.loads(r_ok.stdout)
        id_ok = [c for c in rep_ok["checks"] if "identity" in c["name"].lower() or "worktree" in c["name"].lower()]
        self.assertTrue(all(c["status"] == "ok" for c in id_ok), id_ok)


# ---------------------------------------------------------------------------
# Feature F15: Control Panel GUI Phase 2 Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF15ControlPanelBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F15: Tk Control Panel validation, non-blocking workers, worktree/tunnel/invite errors & clean shutdown."""

    def _run_tk_driver(
        self,
        script_body: str,
        *,
        env_overrides: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        driver = self.ws.root / f"tk_driver_{time.monotonic_ns()}.py"
        driver.write_text(script_body, encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(driver)],
            cwd=str(REPO_ROOT),
            env=self.ws.isolated_env(extra=env_overrides),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
        if res.returncode == 42:
            self.skipTest(res.stdout.strip() or "Tk display unavailable")
        self.assertEqual(
            res.returncode,
            0,
            f"GUI driver failed ({res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )
        return json.loads(res.stdout)

    @staticmethod
    def _drain_logs_snippet() -> str:
        return (
            "def drain_logs(p):\n"
            "    out = []\n"
            "    while not p.log_queue.empty():\n"
            "        out.append(p.log_queue.get_nowait())\n"
            "    return '\\n'.join(out)\n"
        )

    @staticmethod
    def _dialog_hook_snippet(askyesno_ret: bool = True) -> str:
        return (
            "dialogs = []\n"
            "from tkinter import messagebox as _mb\n"
            "_mb.showerror = lambda t, m, **kw: dialogs.append(('error', str(t), str(m)))\n"
            "_mb.showwarning = lambda t, m, **kw: dialogs.append(('warn', str(t), str(m)))\n"
            "_mb.showinfo = lambda t, m, **kw: dialogs.append(('info', str(t), str(m)))\n"
            f"_mb.askyesno = lambda t, m, **kw: (dialogs.append(('askyesno', str(t), str(m))), {askyesno_ret!r})[1]\n"
        )

    def test_f15_invalid_or_empty_base_url_and_agent_id_shows_validation_error(self) -> None:
        repo = self.ws.create_git_repo("gui_val")
        script = f"""
import json, sys, time, tkinter as tk
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
{self._dialog_hook_snippet()}
{self._drain_logs_snippet()}
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    panel.repo_var.set({str(repo)!r})
    panel.port_var.set("99999")
    panel._validated_port()
    base_attr = next((a for a in ("base_url_var", "remote_url_var", "public_url_var") if hasattr(panel, a)), None)
    assert base_attr is not None, "Missing base_url StringVar on AgsyncControlPanel"
    getattr(panel, base_attr).set("ftp://bad.example.com")
    panel.agent_var.set("bad agent *")
    for fn_name in ("save_remote_config", "start_tunnel", "setup_selected_ide", "connect_ide_mcp", "_save_current_config"):
        if hasattr(panel, fn_name):
            getattr(panel, fn_name)()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.05)
    logs = drain_logs(panel)
    print(json.dumps({{
        "alive": root.winfo_exists(),
        "dialogs": dialogs,
        "logs": logs,
    }}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertEqual(out["alive"], 1)
        self.assertTrue(
            len(out["dialogs"]) >= 1 or "invalid" in out["logs"].lower() or "error" in out["logs"].lower(),
            out,
        )

    def test_f15_slow_unreachable_server_runs_on_background_thread_nonblocking(self) -> None:
        tarpit = _SlowTarpitServer(hold_sec=3.0)
        self.ws.register_cleanup(tarpit.stop)
        script = f"""
import json, sys, time, tkinter as tk
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
{self._dialog_hook_snippet()}
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root, initial_mode="peer")
    panel.host_ip_var.set("127.0.0.1")
    panel.port_var.set({str(tarpit.port)!r})
    ticks = []
    def _tick():
        ticks.append(time.monotonic())
        if len(ticks) < 10:
            root.after(20, _tick)
    root.after(10, _tick)
    panel.run_diagnostics()
    t0 = time.monotonic()
    while len(ticks) < 10 and (time.monotonic() - t0) < 3.0:
        root.update()
        time.sleep(0.01)
    elapsed = time.monotonic() - t0
    print(json.dumps({{"ticks": len(ticks), "elapsed": elapsed}}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertEqual(out["ticks"], 10, out)
        self.assertLess(out["elapsed"], 1.0, f"UI thread blocked by slow network call: {out}")

    def test_f15_duplicate_and_missing_worktree_actions_surface_error_gracefully(self) -> None:
        proj = self.ws.create_git_repo("gui_wt_err")
        wt1 = self.ws.ws_dir / "gui_wt_err-wt-1"
        script = f"""
import json, sys, time, tkinter as tk
from pathlib import Path
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
{self._dialog_hook_snippet()}
{self._drain_logs_snippet()}
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    panel.repo_var.set({str(proj)!r})
    wt_attr = next((a for a in ("wt_agent_var", "worktree_agent_var") if hasattr(panel, a)), None)
    assert wt_attr is not None, "Missing worktree agent StringVar"
    create_fn = next((getattr(panel, n) for n in ("create_worktree", "worktree_create", "on_create_worktree") if hasattr(panel, n)), None)
    remove_fn = next((getattr(panel, n) for n in ("remove_worktree", "worktree_remove", "on_remove_worktree") if hasattr(panel, n)), None)
    assert create_fn and remove_fn, "Missing worktree create/remove handlers"

    getattr(panel, wt_attr).set("dup-wt-agent")
    create_fn()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not Path({str(wt1)!r}).is_dir():
        root.update()
        time.sleep(0.05)
    created = Path({str(wt1)!r}).is_dir()

    # Duplicate create
    create_fn()
    # Missing worktree remove
    for attr in ("wt_target_var", "worktree_target_var", "wt_agent_var", "worktree_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("no-such-wt-agent")
    remove_fn()
    deadline2 = time.monotonic() + 5.0
    while time.monotonic() < deadline2:
        root.update()
        time.sleep(0.05)
    logs = drain_logs(panel)
    print(json.dumps({{
        "alive": root.winfo_exists(),
        "created": created,
        "dialogs": dialogs,
        "logs": logs,
    }}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertEqual(out["alive"], 1)
        self.assertTrue(out["created"])
        combined = (out["logs"] + json.dumps(out["dialogs"])).lower()
        self.assertTrue("error" in combined or "fail" in combined or "exist" in combined or len(out["dialogs"]) >= 1, out)

    def test_f15_tunnel_missing_binary_and_stop_idempotency_handled_gracefully(self) -> None:
        script = f"""
import json, sys, time, tkinter as tk
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
{self._dialog_hook_snippet()}
{self._drain_logs_snippet()}
try:
    from agsync.config import load_config
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    base_attr = next((a for a in ("base_url_var", "remote_url_var", "public_url_var") if hasattr(panel, a)), None)
    assert base_attr is not None, "Missing base_url StringVar"
    start_t = next((getattr(panel, n) for n in ("start_tunnel", "on_start_tunnel") if hasattr(panel, n)), None)
    stop_t = next((getattr(panel, n) for n in ("stop_tunnel", "on_stop_tunnel") if hasattr(panel, n)), None)
    assert start_t and stop_t, "Missing tunnel start/stop handlers"

    # Stop when idle must be idempotent
    stop_t()
    # Start with empty PATH -> genuine missing cloudflared binary
    start_t()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.05)

    # base_url set/save and clear/save persistence
    getattr(panel, base_attr).set("https://custom-hub.example.com")
    panel._save_current_config()
    cfg_set = load_config()
    getattr(panel, base_attr).set("")
    panel._save_current_config()
    cfg_cleared = load_config()

    print(json.dumps({{
        "alive": root.winfo_exists(),
        "dialogs": dialogs,
        "logs": drain_logs(panel),
        "cfg_set": cfg_set,
        "cfg_cleared": cfg_cleared,
    }}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script, env_overrides={"PATH": ""})
        self.assertEqual(out["alive"], 1)
        combined = (out["logs"] + json.dumps(out["dialogs"])).lower()
        self.assertTrue("cloudflared" in combined or "error" in combined or len(out["dialogs"]) >= 1, out)
        self.assertIn("https://custom-hub.example.com", json.dumps(out["cfg_set"]))
        self.assertNotIn("https://custom-hub.example.com", json.dumps(out["cfg_cleared"]))

    def test_f15_invalid_invite_token_and_unauthorized_invite_create_surface_errors(self) -> None:
        hub = self.ws.start_hub(token="real-admin-secret")
        repo = self.ws.create_git_repo("gui_inv_err")
        script = f"""
import json, sys, time, tkinter as tk
from pathlib import Path
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
{self._dialog_hook_snippet()}
{self._drain_logs_snippet()}
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root)
    panel.host_ip_var.set("127.0.0.1")
    panel.port_var.set({str(hub.port)!r})
    panel.token_var.set("wrong-admin-token")
    panel.repo_var.set({str(repo)!r})
    for attr in ("invite_agent_var", "peer_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("bad-peer")
    inv_fn = next((getattr(panel, n) for n in ("create_invite", "generate_invite", "on_create_invite") if hasattr(panel, n)), None)
    join_fn = next((getattr(panel, n) for n in ("join_invite", "join_hub", "on_join_invite") if hasattr(panel, n)), None)
    assert inv_fn and join_fn, "Missing invite/join handlers"

    inv_fn()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.05)

    for attr in ("invite_link_var", "join_token_var", "invite_token_var", "join_link_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("tampered.invite.token")
    join_fn()
    deadline2 = time.monotonic() + 3.0
    while time.monotonic() < deadline2:
        root.update()
        time.sleep(0.05)

    inv_val = ""
    for attr in ("invite_link_var", "join_token_var", "invite_token_var", "join_link_var"):
        if hasattr(panel, attr):
            inv_val = getattr(panel, attr).get()
            break
    print(json.dumps({{
        "alive": root.winfo_exists(),
        "dialogs": dialogs,
        "logs": drain_logs(panel),
        "token_written": (Path({str(repo)!r}) / ".agsync" / "token").exists(),
        "inv_val": inv_val,
    }}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        out = self._run_tk_driver(script)
        self.assertEqual(out["alive"], 1)
        self.assertFalse(out["token_written"])
        combined = (out["logs"] + json.dumps(out["dialogs"])).lower()
        self.assertTrue("error" in combined or "fail" in combined or "401" in combined or len(out["dialogs"]) >= 1, out)

    def test_f15_on_close_terminates_running_processes_without_orphans(self) -> None:
        # Pick an ephemeral port for panel.start_server()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        finally:
            s.close()

        script = f"""
import json, sys, time, tkinter as tk
try:
    root = tk.Tk()
except tk.TclError as exc:
    print(f"Tk display unavailable: {{exc}}")
    sys.exit(42)
root.withdraw()
{self._dialog_hook_snippet(askyesno_ret=True)}
panel = None
srv_pid = None
try:
    from scripts.control_panel import AgsyncControlPanel
    panel = AgsyncControlPanel(root, initial_mode="host")
    panel.port_var.set({str(port)!r})
    panel.discover_chk_var.set(False)
    panel.start_server()
    assert panel.server_proc is not None, "start_server() did not spawn server_proc"
    srv_pid = int(panel.server_proc.pid)
    time.sleep(0.5)
    panel._on_close()
    print(json.dumps({{"srv_pid": srv_pid}}))
finally:
    if panel is not None:
        try:
            if panel.server_proc is not None and panel.server_proc.poll() is None:
                panel.server_proc.kill()
        except Exception:
            pass
        try:
            panel._job.close()
        except Exception:
            pass
    try:
        if root.winfo_exists():
            root.destroy()
    except Exception:
        pass
"""
        out = self._run_tk_driver(script)
        srv_pid = int(out["srv_pid"])
        self.assertTrue(_poll_pid_dead(srv_pid, timeout=4.0), f"Panel server_proc PID {srv_pid} survived _on_close()")


# ---------------------------------------------------------------------------
# Feature F16: Bilingual Docs & Templates Sync Boundaries (6 tests)
# ---------------------------------------------------------------------------


class TestF16BilingualDocsBoundaries(ManagedRepoTestMixin, unittest.TestCase):
    """F16: EN/FA command parity, Markdown links & anchors, UTF-8 integrity, tool sync, negative Phase-1 scan, fenced JSON."""

    def test_f16_readme_en_and_fa_strict_phase2_command_and_flag_parity(self) -> None:
        required = (
            "hook-proxy",
            "worktree",
            "invite",
            "join",
            "setup-mcp",
            "install-hooks",
            "sync_wait",
            "--base-url",
            "--protocol http2",
            "/events",
        )
        for fname in ("README.md", "README.fa.md"):
            text = (REPO_ROOT / fname).read_text(encoding="utf-8")
            for tok in required:
                self.assertIn(tok, text, f"{fname} missing required Phase 2 token {tok!r}")
            self.assertNotIn("/sse", text, f"{fname} still references legacy /sse")
            self.assertNotIn("serverUrl", text, f"{fname} still references legacy serverUrl")

    def test_f16_all_markdown_internal_links_resolve_across_repo_and_templates(self) -> None:
        md_files = (
            [REPO_ROOT / "README.md", REPO_ROOT / "README.fa.md"]
            + sorted((REPO_ROOT / "docs").glob("*.md"))
            + sorted((REPO_ROOT / "templates").rglob("*.md"))
        )
        link_re = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
        heading_re = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)

        def _slugify(heading: str) -> str:
            cleaned = re.sub(r"[`*_\[\]()]", "", heading).strip().lower()
            cleaned = re.sub(r"[^\w\s-]", "", cleaned)
            return re.sub(r"\s+", "-", cleaned)

        for md in md_files:
            text = md.read_text(encoding="utf-8")
            for raw_target in link_re.findall(text):
                if raw_target.startswith(("http://", "https://", "mailto:")):
                    continue
                if "#" in raw_target:
                    path_part, frag = raw_target.split("#", 1)
                else:
                    path_part, frag = raw_target, ""
                target_file = md if not path_part else (md.parent / path_part).resolve()
                self.assertTrue(
                    target_file.is_file(),
                    f"Broken link target in {md.relative_to(REPO_ROOT)}: {raw_target}",
                )
                if frag and frag.isascii():
                    t_text = target_file.read_text(encoding="utf-8")
                    slugs = {_slugify(h) for h in heading_re.findall(t_text)}
                    html_ids = set(re.findall(r'id=["\']([^"\']+)["\']', t_text))
                    self.assertTrue(
                        frag.lower() in slugs or frag in html_ids,
                        f"Broken anchor #{frag} in {md.relative_to(REPO_ROOT)} -> {target_file.name}",
                    )

    def test_f16_persian_docs_utf8_integrity_and_bilingual_cross_links(self) -> None:
        fa_files = [REPO_ROOT / "README.fa.md"] + sorted((REPO_ROOT / "docs").glob("*.fa.md"))
        self.assertGreaterEqual(len(fa_files), 3)
        for fa in fa_files:
            raw = fa.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"Unexpected UTF-8 BOM in {fa}")
            self.assertFalse(raw.startswith((b"\xff\xfe", b"\xfe\xff")), f"Unexpected UTF-16 BOM in {fa}")
            text = raw.decode("utf-8")
            self.assertNotIn("\ufffd", text, f"Replacement char mojibake in {fa}")
            persian_chars = re.findall(r"[\u0600-\u06FF]", text)
            self.assertGreaterEqual(len(persian_chars), 100, f"Too few Persian chars in {fa}")

        pairs = [
            (REPO_ROOT / "README.md", REPO_ROOT / "README.fa.md", "README.fa.md", "README.md"),
            (REPO_ROOT / "docs" / "SETUP_GUIDE.md", REPO_ROOT / "docs" / "SETUP_GUIDE.fa.md", "SETUP_GUIDE.fa.md", "SETUP_GUIDE.md"),
            (REPO_ROOT / "docs" / "ARCHITECTURE.md", REPO_ROOT / "docs" / "ARCHITECTURE.fa.md", "ARCHITECTURE.fa.md", "ARCHITECTURE.md"),
        ]
        for en_p, fa_p, link_to_fa, link_to_en in pairs:
            self.assertIn(link_to_fa, en_p.read_text(encoding="utf-8"))
            self.assertIn(link_to_en, fa_p.read_text(encoding="utf-8"))

    def test_f16_templates_agents_md_phase2_and_protocol_tool_consistency(self) -> None:
        from agsync.protocol import MCP_TOOLS

        agents_md = (REPO_ROOT / "templates" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("sync_wait", agents_md)
        self.assertIn(".agsync-agent", agents_md)
        self.assertRegex(agents_md.lower(), r"hook-proxy|hook_guard|cli")
        self.assertNotIn("/sse", agents_md)
        self.assertNotIn("Phase 2 (Planned)", agents_md)

        valid_tools = {t["name"] for t in MCP_TOOLS}
        mentioned_sync_tools = set(re.findall(r"\bsync_[a-z0-9_]+\b", agents_md))
        self.assertGreaterEqual(len(mentioned_sync_tools), 1)
        unknown = mentioned_sync_tools - valid_tools
        self.assertEqual(unknown, set(), f"templates/AGENTS.md references unknown MCP tool(s): {unknown}")

    def test_f16_zero_leftover_phase1_only_contradictory_claims(self) -> None:
        operational_docs = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "README.fa.md",
            REPO_ROOT / "docs" / "SETUP_GUIDE.md",
            REPO_ROOT / "docs" / "SETUP_GUIDE.fa.md",
            REPO_ROOT / "docs" / "ARCHITECTURE.md",
            REPO_ROOT / "docs" / "ARCHITECTURE.fa.md",
            REPO_ROOT / "docs" / "OVERVIEW.fa.md",
        ]
        forbidden_phrases = (
            "Phase 2 (Planned)",
            "Phase 2 adds",
            "در فاز ۲",
            "13 `sync_*` tools",
            "13 ابزار",
            "v0.1.0 — Phase 1",
            "v0.1.0 — فاز ۱",
            "(Phase 1)",
            "(فاز ۱)",
        )
        for doc in operational_docs:
            self.assertTrue(doc.is_file(), f"Missing operational doc {doc}")
            text = doc.read_text(encoding="utf-8")
            for phrase in forbidden_phrases:
                self.assertNotIn(phrase, text, f"Stale Phase-1 phrase {phrase!r} found in {doc.name}")

    def test_f16_fenced_json_snippets_in_docs_parse_cleanly_without_legacy_keys(self) -> None:
        docs = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "README.fa.md",
            REPO_ROOT / "docs" / "SETUP_GUIDE.md",
            REPO_ROOT / "docs" / "SETUP_GUIDE.fa.md",
        ]
        fence_re = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
        placeholder_re = re.compile(r"<[^>]+>")
        checked = 0
        for doc in docs:
            text = doc.read_text(encoding="utf-8")
            for snippet in fence_re.findall(text):
                checked += 1
                self.assertNotIn("/sse", snippet, f"Legacy /sse snippet in {doc.name}")
                self.assertNotIn("serverUrl", snippet, f"Legacy serverUrl snippet in {doc.name}")
                sanitized = placeholder_re.sub("placeholder", snippet)
                parsed = json.loads(sanitized)
                if isinstance(parsed, dict) and isinstance(parsed.get("mcpServers"), dict):
                    agsync_cfg = parsed["mcpServers"].get("agsync")
                    if isinstance(agsync_cfg, dict):
                        has_url = "/mcp" in str(agsync_cfg.get("url", ""))
                        has_cmd = "agsync.mcp_bridge" in json.dumps(agsync_cfg)
                        self.assertTrue(
                            has_url or has_cmd,
                            f"mcpServers.agsync snippet in {doc.name} lacks /mcp url or agsync.mcp_bridge command: {agsync_cfg}",
                        )
        self.assertGreaterEqual(checked, 1, "Expected at least one ```json snippet in setup/README docs")


if __name__ == "__main__":
    unittest.main()
