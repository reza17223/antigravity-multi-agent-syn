"""SQLite WAL database layer, PRAGMA user_version migrations, and transaction helpers."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

SCHEMA_VERSION = 2

_BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    machine_name TEXT NOT NULL,
    role TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    registered_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    ttl_sec INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON messages(recipient_id, id);

CREATE TABLE IF NOT EXISTS agent_cursors (
    agent_id TEXT PRIMARY KEY,
    last_read_msg_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    scope_paths_json TEXT NOT NULL,
    status TEXT NOT NULL,
    owner_id TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result_summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    resource TEXT NOT NULL,
    resource_norm TEXT NOT NULL,
    scope TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leases_expires
    ON leases(expires_at);

CREATE TABLE IF NOT EXISTS context_kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    val INTEGER NOT NULL
);
INSERT OR IGNORE INTO counters(name, val) VALUES ('fencing_seq', 0);
INSERT OR IGNORE INTO counters(name, val) VALUES ('task_seq', 0);
"""

_SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    boot_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created_at
    ON events(created_at);

CREATE TABLE IF NOT EXISTS invites (
    nonce TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'agent',
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_at REAL,
    used_by_agent TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_invites_expires
    ON invites(expires_at);

CREATE TABLE IF NOT EXISTS agent_tokens (
    token_hash TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'agent',
    created_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""

MIGRATIONS: dict[int, str] = {
    1: _BASE_SCHEMA_SQL,
    2: _SCHEMA_V2_SQL,
}


def default_data_dir() -> Path:
    """Return user-local agsync runtime directory (~/.agsync)."""
    return Path.home() / ".agsync"


def default_db_path() -> Path:
    """Return default SQLite state database path (~/.agsync/state.db)."""
    return default_data_dir() / "state.db"


class SyncDatabase:
    """Thread-safe SQLite WAL connection manager with versioned migrations."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        case_insensitive: bool | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        resolved = default_db_path() if db_path is None else db_path
        self.db_path = str(resolved)
        self.case_insensitive = (
            (sys.platform == "win32") if case_insensitive is None else bool(case_insensitive)
        )
        self.now_fn = time_fn or time.time
        self.lock = threading.RLock()
        self.boot_id = uuid.uuid4().hex[:16]
        self.event_cond = threading.Condition()
        self._event_seq = 0
        self._closed = False

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        try:
            self._init_db()
        except Exception:
            self.conn.close()
            raise

    def notify_events(self) -> None:
        with self.event_cond:
            self._event_seq += 1
            self.event_cond.notify_all()

    def wake_waiters(self, closing: bool = True) -> None:
        with self.event_cond:
            if closing:
                self._closed = True
            self._event_seq += 1
            self.event_cond.notify_all()

    def close(self) -> None:
        self.wake_waiters(closing=True)
        with self.lock:
            self.conn.close()

    def _init_db(self) -> None:
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.execute("PRAGMA busy_timeout=10000;")
            self.conn.execute("PRAGMA foreign_keys=ON;")
            self._run_migrations_locked()

    def _run_migrations_locked(self) -> None:
        row = self.conn.execute("PRAGMA user_version;").fetchone()
        current_ver = int(row[0]) if row else 0
        if current_ver >= SCHEMA_VERSION:
            return

        for ver in range(current_ver + 1, SCHEMA_VERSION + 1):
            sql = MIGRATIONS.get(ver)
            if not sql:
                continue
            self.conn.executescript(sql)
            self.conn.execute(f"PRAGMA user_version = {int(ver)};")

    @contextmanager
    def read_tx(self) -> Iterator[None]:
        """Run a consistent deferred read transaction without acquiring a write lock."""
        with self.lock:
            if self.conn.in_transaction:
                try:
                    self.conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
            self.conn.execute("BEGIN DEFERRED;")
            try:
                yield
            finally:
                if self.conn.in_transaction:
                    try:
                        self.conn.execute("ROLLBACK;")
                    except sqlite3.Error:
                        pass

    @contextmanager
    def write_tx(self) -> Iterator[None]:
        """Run an immediate write transaction with safe rollback if COMMIT fails."""
        with self.lock:
            if self.conn.in_transaction:
                try:
                    self.conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
            self.conn.execute("BEGIN IMMEDIATE;")
            try:
                yield
                self.conn.execute("COMMIT;")
            except Exception:
                if self.conn.in_transaction:
                    try:
                        self.conn.execute("ROLLBACK;")
                    except sqlite3.Error:
                        pass
                raise

    def next_counter(self, name: str) -> int:
        self.conn.execute("UPDATE counters SET val = val + 1 WHERE name = ?;", (name,))
        row = self.conn.execute("SELECT val FROM counters WHERE name = ?;", (name,)).fetchone()
        return int(row["val"])

    def backup_to(self, backup_path: str | Path) -> Path | None:
        """Create a consistent SQLite online backup of a file-backed database."""
        if self.db_path == ":memory:":
            return None
        dest = Path(backup_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            dst_conn = sqlite3.connect(str(dest), timeout=30.0)
            try:
                self.conn.backup(dst_conn)
            finally:
                dst_conn.close()
        return dest
