"""SQLite WAL coordination state store with strict serialized transactions."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from agsync.protocol import (
    DEFAULT_AGENT_TTL_SEC,
    DEFAULT_LEASE_TTL_SEC,
    ProtocolError,
    normalize_resource,
    paths_overlap,
    validate_agent_id,
    validate_agent_status,
    validate_msg_type,
    validate_recipient_id,
    validate_scope,
    validate_task_status,
)


class SyncError(Exception):
    """Base error mapped to HTTP status codes and structured JSON errors."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.error_code,
            "message": self.message,
        }
        if self.details:
            payload.update(self.details)
        return payload


class ConflictError(SyncError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_code, message, status_code=409, details=details)


class NotFoundError(SyncError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_code, message, status_code=404, details=details)


class ValidationError(SyncError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_code, message, status_code=400, details=details)


def _wrap_protocol(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ProtocolError as exc:
        raise ValidationError("validation_error", str(exc)) from exc


class SyncStore:
    """Thread-safe, crash-safe SQLite WAL store for two-machine coordination."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        case_insensitive: bool | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.case_insensitive = (
            (sys.platform == "win32") if case_insensitive is None else bool(case_insensitive)
        )
        self._now = time_fn or time.time
        self._lock = threading.RLock()

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._init_db()
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=10000;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(
                """
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
            )

    @contextmanager
    def _write_tx(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE;")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK;")
                raise
            else:
                self._conn.execute("COMMIT;")

    def _next_counter(self, name: str) -> int:
        self._conn.execute(
            "UPDATE counters SET val = val + 1 WHERE name = ?;", (name,)
        )
        row = self._conn.execute(
            "SELECT val FROM counters WHERE name = ?;", (name,)
        ).fetchone()
        return int(row["val"])

    def _purge_expired_leases_locked(self, now: float) -> int:
        cur = self._conn.execute(
            "DELETE FROM leases WHERE expires_at <= ?;", (now,)
        )
        return int(cur.rowcount)

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    def _row_to_agent(self, row: sqlite3.Row, now: float) -> dict[str, Any]:
        last_hb = float(row["last_heartbeat"])
        ttl = int(row["ttl_sec"])
        is_online = now <= (last_hb + ttl)
        return {
            "agent_id": row["agent_id"],
            "machine_name": row["machine_name"],
            "role": row["role"],
            "branch": row["branch"],
            "status": row["status"] if is_online else "offline",
            "reported_status": row["status"],
            "is_online": is_online,
            "capabilities": json.loads(row["capabilities_json"]),
            "registered_at": float(row["registered_at"]),
            "last_heartbeat": last_hb,
            "ttl_sec": ttl,
            "expires_at": last_hb + ttl,
        }

    def register_agent(
        self,
        agent_id: str,
        *,
        machine_name: str = "",
        role: str = "agent",
        branch: str = "",
        status: str = "online",
        capabilities: list[str] | None = None,
        ttl_sec: int = DEFAULT_AGENT_TTL_SEC,
    ) -> dict[str, Any]:
        aid = _wrap_protocol(validate_agent_id, agent_id)
        st = _wrap_protocol(validate_agent_status, status)
        ttl = int(ttl_sec)
        if ttl <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        caps_json = json.dumps(list(capabilities or []))
        now = float(self._now())

        with self._write_tx():
            existing = self._conn.execute(
                "SELECT registered_at FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
            reg_at = float(existing["registered_at"]) if existing else now
            self._conn.execute(
                """
                INSERT INTO agents (
                    agent_id, machine_name, role, branch, status,
                    capabilities_json, registered_at, last_heartbeat, ttl_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    machine_name = excluded.machine_name,
                    role = excluded.role,
                    branch = excluded.branch,
                    status = excluded.status,
                    capabilities_json = excluded.capabilities_json,
                    last_heartbeat = excluded.last_heartbeat,
                    ttl_sec = excluded.ttl_sec;
                """,
                (
                    aid,
                    str(machine_name),
                    str(role),
                    str(branch),
                    st,
                    caps_json,
                    reg_at,
                    now,
                    ttl,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
        return self._row_to_agent(row, now)

    def heartbeat_agent(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        branch: str | None = None,
        ttl_sec: int | None = None,
    ) -> dict[str, Any]:
        aid = _wrap_protocol(validate_agent_id, agent_id)
        if status is not None:
            _wrap_protocol(validate_agent_status, status)
        if ttl_sec is not None and int(ttl_sec) <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        now = float(self._now())

        with self._write_tx():
            row = self._conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
            if not row:
                raise NotFoundError(
                    "agent_not_found", f"Agent '{aid}' is not registered"
                )
            new_status = status if status is not None else row["status"]
            new_branch = branch if branch is not None else row["branch"]
            new_ttl = int(ttl_sec) if ttl_sec is not None else int(row["ttl_sec"])
            self._conn.execute(
                """
                UPDATE agents
                SET status = ?, branch = ?, ttl_sec = ?, last_heartbeat = ?
                WHERE agent_id = ?;
                """,
                (new_status, str(new_branch), new_ttl, now, aid),
            )
            updated = self._conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
        return self._row_to_agent(updated, now)

    def list_agents(self, *, include_offline: bool = True) -> list[dict[str, Any]]:
        now = float(self._now())
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY registered_at ASC;"
            ).fetchall()
        agents = [self._row_to_agent(r, now) for r in rows]
        if not include_offline:
            agents = [a for a in agents if a["is_online"]]
        return agents

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def _row_to_message(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "sender_id": row["sender_id"],
            "recipient_id": row["recipient_id"],
            "msg_type": row["msg_type"],
            "body": row["body"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": float(row["created_at"]),
        }

    def send_message(
        self,
        sender_id: str,
        recipient_id: str,
        body: str,
        *,
        msg_type: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sid = _wrap_protocol(validate_agent_id, sender_id)
        rid = _wrap_protocol(validate_recipient_id, recipient_id)
        mtype = _wrap_protocol(validate_msg_type, msg_type)
        if not isinstance(body, str) or not body.strip():
            raise ValidationError("validation_error", "Message body cannot be empty")
        meta_json = json.dumps(metadata or {})
        now = float(self._now())

        with self._write_tx():
            cur = self._conn.execute(
                """
                INSERT INTO messages (sender_id, recipient_id, msg_type, body, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (sid, rid, mtype, body, meta_json, now),
            )
            msg_id = int(cur.lastrowid)
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?;", (msg_id,)
            ).fetchone()
        return self._row_to_message(row)

    def _get_cursor_locked(self, agent_id: str) -> int:
        row = self._conn.execute(
            "SELECT last_read_msg_id FROM agent_cursors WHERE agent_id = ?;",
            (agent_id,),
        ).fetchone()
        return int(row["last_read_msg_id"]) if row else 0

    def poll_messages(
        self,
        agent_id: str,
        *,
        since_id: int | None = None,
        unread_only: bool = False,
        auto_ack: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        aid = _wrap_protocol(validate_agent_id, agent_id)
        lim = max(1, min(int(limit), 500))

        with self._write_tx() if auto_ack else self._lock:
            cursor = self._get_cursor_locked(aid)
            if unread_only:
                effective_since = max(cursor, int(since_id or 0))
                rows = self._conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE id > ?
                      AND (
                          recipient_id = ?
                          OR (recipient_id = '*' AND sender_id != ?)
                      )
                    ORDER BY id ASC
                    LIMIT ?;
                    """,
                    (effective_since, aid, aid, lim),
                ).fetchall()
            else:
                effective_since = int(since_id or 0)
                rows = self._conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE id > ?
                      AND (recipient_id = ? OR recipient_id = '*')
                    ORDER BY id ASC
                    LIMIT ?;
                    """,
                    (effective_since, aid, lim),
                ).fetchall()

            msgs = [self._row_to_message(r) for r in rows]
            if auto_ack and msgs:
                max_id = max(m["id"] for m in msgs)
                new_cursor = max(cursor, max_id)
                self._conn.execute(
                    """
                    INSERT INTO agent_cursors (agent_id, last_read_msg_id)
                    VALUES (?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        last_read_msg_id = MAX(agent_cursors.last_read_msg_id, excluded.last_read_msg_id);
                    """,
                    (aid, new_cursor),
                )
                cursor = new_cursor

        return {
            "agent_id": aid,
            "cursor": cursor,
            "messages": msgs,
        }

    def ack_messages(self, agent_id: str, up_to_id: int) -> dict[str, Any]:
        aid = _wrap_protocol(validate_agent_id, agent_id)
        target_id = int(up_to_id)
        if target_id < 0:
            raise ValidationError("validation_error", "up_to_id must be >= 0")
        with self._write_tx():
            self._conn.execute(
                """
                INSERT INTO agent_cursors (agent_id, last_read_msg_id)
                VALUES (?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    last_read_msg_id = MAX(agent_cursors.last_read_msg_id, excluded.last_read_msg_id);
                """,
                (aid, target_id),
            )
            cursor = self._get_cursor_locked(aid)
        return {"agent_id": aid, "last_read_msg_id": cursor}

    def _count_unread_locked(self, agent_id: str) -> int:
        cursor = self._get_cursor_locked(agent_id)
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM messages
            WHERE id > ?
              AND (
                  recipient_id = ?
                  OR (recipient_id = '*' AND sender_id != ?)
              );
            """,
            (cursor, agent_id, agent_id),
        ).fetchone()
        return int(row["cnt"])

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def _row_to_task(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "description": row["description"],
            "scope_paths": json.loads(row["scope_paths_json"]),
            "status": row["status"],
            "owner_id": row["owner_id"],
            "created_by": row["created_by"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "result_summary": row["result_summary"],
        }

    def create_task(
        self,
        title: str,
        created_by: str,
        *,
        description: str = "",
        scope_paths: list[str] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        creator = _wrap_protocol(validate_agent_id, created_by)
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("validation_error", "Task title cannot be empty")
        if scope_paths is not None and not isinstance(scope_paths, list):
            raise ValidationError("validation_error", "scope_paths must be a list of strings")
        if task_id is not None:
            if not isinstance(task_id, str):
                raise ValidationError("validation_error", "task_id must be a string")
            tid_clean: str | None = task_id.strip()
            if not tid_clean:
                raise ValidationError("validation_error", "task_id cannot be empty")
        else:
            tid_clean = None
        norm_paths = [
            _wrap_protocol(
                normalize_resource,
                p,
                scope="file",
                case_insensitive=self.case_insensitive,
            )
            for p in (scope_paths or [])
        ]
        now = float(self._now())

        with self._write_tx():
            if tid_clean is not None:
                tid = tid_clean
                existing = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?;", (tid,)
                ).fetchone()
                if existing:
                    raise ConflictError(
                        "task_exists", f"Task '{tid}' already exists"
                    )
            else:
                seq = self._next_counter("task_seq")
                tid = f"task-{seq:03d}"

            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, title, description, scope_paths_json, status,
                    owner_id, created_by, created_at, updated_at, result_summary
                ) VALUES (?, ?, ?, ?, 'open', NULL, ?, ?, ?, '');
                """,
                (
                    tid,
                    title.strip(),
                    str(description),
                    json.dumps(norm_paths),
                    creator,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
        return self._row_to_task(row)

    def list_tasks(
        self, *, status: str | None = None, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            _wrap_protocol(validate_task_status, status)
            clauses.append("status = ?")
            params.append(status)
        if owner_id is not None:
            _wrap_protocol(validate_agent_id, owner_id)
            clauses.append("owner_id = ?")
            params.append(owner_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at ASC, task_id ASC;",
                params,
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def claim_task(self, task_id: str, agent_id: str) -> dict[str, Any]:
        aid = _wrap_protocol(validate_agent_id, agent_id)
        tid = task_id.strip()
        now = float(self._now())

        with self._write_tx():
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
            if not row:
                raise NotFoundError("task_not_found", f"Task '{tid}' not found")
            current_owner = row["owner_id"]
            current_status = row["status"]
            if current_owner == aid and current_status in ("claimed", "in_progress"):
                return self._row_to_task(row)
            if current_status != "open" or (current_owner is not None and current_owner != aid):
                raise ConflictError(
                    "task_already_claimed",
                    f"Task '{tid}' cannot be claimed (status={current_status}, owner={current_owner})",
                    details={
                        "task_id": tid,
                        "current_status": current_status,
                        "current_owner": current_owner,
                    },
                )
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'claimed', owner_id = ?, updated_at = ?
                WHERE task_id = ?;
                """,
                (aid, now, tid),
            )
            updated = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
        return self._row_to_task(updated)

    def update_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        status: str | None = None,
        result_summary: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        aid = _wrap_protocol(validate_agent_id, agent_id)
        tid = task_id.strip()
        if status is not None:
            _wrap_protocol(validate_task_status, status)
        now = float(self._now())

        with self._write_tx():
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
            if not row:
                raise NotFoundError("task_not_found", f"Task '{tid}' not found")
            owner = row["owner_id"]
            if owner is not None and owner != aid and not force:
                raise ConflictError(
                    "task_not_owned",
                    f"Task '{tid}' is owned by '{owner}', not '{aid}'",
                    details={"task_id": tid, "owner_id": owner},
                )
            new_status = status if status is not None else row["status"]
            new_owner = None if new_status == "open" else (owner or aid)
            new_summary = (
                str(result_summary)
                if result_summary is not None
                else row["result_summary"]
            )
            self._conn.execute(
                """
                UPDATE tasks
                SET status = ?, owner_id = ?, result_summary = ?, updated_at = ?
                WHERE task_id = ?;
                """,
                (new_status, new_owner, new_summary, now, tid),
            )
            updated = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
        return self._row_to_task(updated)

    # ------------------------------------------------------------------
    # Leases (Exclusive Path / Directory / Symbol Locks)
    # ------------------------------------------------------------------
    def _row_to_lease(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "lease_id": row["lease_id"],
            "resource": row["resource"],
            "resource_norm": row["resource_norm"],
            "scope": row["scope"],
            "owner_id": row["owner_id"],
            "reason": row["reason"],
            "fencing_token": int(row["fencing_token"]),
            "acquired_at": float(row["acquired_at"]),
            "expires_at": float(row["expires_at"]),
        }

    def acquire_lease(
        self,
        resource: str,
        owner_id: str,
        *,
        scope: str = "file",
        ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
        reason: str = "",
    ) -> dict[str, Any]:
        oid = _wrap_protocol(validate_agent_id, owner_id)
        sc = _wrap_protocol(validate_scope, scope)
        norm = _wrap_protocol(
            normalize_resource,
            resource,
            scope=sc,
            case_insensitive=self.case_insensitive,
        )
        ttl = int(ttl_sec)
        if ttl <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        now = float(self._now())
        expires_at = now + ttl

        with self._write_tx():
            self._purge_expired_leases_locked(now)
            rows = self._conn.execute("SELECT * FROM leases;").fetchall()

            conflicts: list[dict[str, Any]] = []
            exact_own_row: sqlite3.Row | None = None
            for row in rows:
                if row["owner_id"] == oid:
                    if row["resource_norm"] == norm and row["scope"] == sc:
                        exact_own_row = row
                    continue
                if paths_overlap(norm, sc, row["resource_norm"], row["scope"]):
                    conflicts.append(self._row_to_lease(row))

            if conflicts:
                raise ConflictError(
                    "lease_conflict",
                    f"Resource '{norm}' ({sc}) conflicts with active peer lease(s)",
                    details={
                        "requested": {
                            "resource": resource,
                            "resource_norm": norm,
                            "scope": sc,
                            "owner_id": oid,
                        },
                        "conflicts": conflicts,
                    },
                )

            if exact_own_row is not None:
                new_reason = str(reason) if reason else exact_own_row["reason"]
                self._conn.execute(
                    """
                    UPDATE leases
                    SET resource = ?, reason = ?, expires_at = ?
                    WHERE lease_id = ?;
                    """,
                    (resource, new_reason, expires_at, exact_own_row["lease_id"]),
                )
                updated = self._conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ?;",
                    (exact_own_row["lease_id"],),
                ).fetchone()
                return {"lease": self._row_to_lease(updated), "renewed": True}

            token = self._next_counter("fencing_seq")
            lid = f"lease-{uuid.uuid4().hex[:10]}"
            self._conn.execute(
                """
                INSERT INTO leases (
                    lease_id, resource, resource_norm, scope, owner_id,
                    reason, fencing_token, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (lid, resource, norm, sc, oid, str(reason), token, now, expires_at),
            )
            created = self._conn.execute(
                "SELECT * FROM leases WHERE lease_id = ?;", (lid,)
            ).fetchone()
            return {"lease": self._row_to_lease(created), "renewed": False}

    def renew_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
    ) -> dict[str, Any]:
        oid = _wrap_protocol(validate_agent_id, owner_id)
        ttl = int(ttl_sec)
        if ttl <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        if not lease_id and not resource:
            raise ValidationError(
                "validation_error", "Provide either lease_id or resource"
            )
        now = float(self._now())
        expires_at = now + ttl

        with self._write_tx():
            self._purge_expired_leases_locked(now)
            if lease_id:
                row = self._conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ?;", (lease_id,)
                ).fetchone()
            else:
                sc = _wrap_protocol(validate_scope, scope)
                norm = _wrap_protocol(
                    normalize_resource,
                    resource or "",
                    scope=sc,
                    case_insensitive=self.case_insensitive,
                )
                row = self._conn.execute(
                    "SELECT * FROM leases WHERE resource_norm = ? AND scope = ?;",
                    (norm, sc),
                ).fetchone()
            if not row:
                raise NotFoundError("lease_not_found", "Active lease not found")
            if row["owner_id"] != oid:
                raise ConflictError(
                    "lease_not_owned",
                    f"Lease '{row['lease_id']}' is owned by '{row['owner_id']}'",
                    details={"lease": self._row_to_lease(row)},
                )
            self._conn.execute(
                "UPDATE leases SET expires_at = ? WHERE lease_id = ?;",
                (expires_at, row["lease_id"]),
            )
            updated = self._conn.execute(
                "SELECT * FROM leases WHERE lease_id = ?;", (row["lease_id"],)
            ).fetchone()
        return self._row_to_lease(updated)

    def release_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        release_all: bool = False,
    ) -> dict[str, Any]:
        oid = _wrap_protocol(validate_agent_id, owner_id)
        now = float(self._now())

        with self._write_tx():
            self._purge_expired_leases_locked(now)
            if release_all:
                rows = self._conn.execute(
                    "SELECT lease_id FROM leases WHERE owner_id = ?;", (oid,)
                ).fetchall()
                ids = [r["lease_id"] for r in rows]
                self._conn.execute(
                    "DELETE FROM leases WHERE owner_id = ?;", (oid,)
                )
                return {"released_count": len(ids), "released_ids": ids}

            if not lease_id and not resource:
                raise ValidationError(
                    "validation_error",
                    "Provide lease_id, resource, or release_all=True",
                )
            if lease_id:
                row = self._conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ?;", (lease_id,)
                ).fetchone()
            else:
                sc = _wrap_protocol(validate_scope, scope)
                norm = _wrap_protocol(
                    normalize_resource,
                    resource or "",
                    scope=sc,
                    case_insensitive=self.case_insensitive,
                )
                row = self._conn.execute(
                    "SELECT * FROM leases WHERE resource_norm = ? AND scope = ? AND owner_id = ?;",
                    (norm, sc, oid),
                ).fetchone()
                if not row:
                    peer_row = self._conn.execute(
                        "SELECT * FROM leases WHERE resource_norm = ? AND scope = ?;",
                        (norm, sc),
                    ).fetchone()
                    if peer_row:
                        row = peer_row
            if not row:
                raise NotFoundError("lease_not_found", "Active lease not found")
            if row["owner_id"] != oid:
                raise ConflictError(
                    "lease_not_owned",
                    f"Cannot release lease owned by '{row['owner_id']}'",
                    details={"lease": self._row_to_lease(row)},
                )
            self._conn.execute(
                "DELETE FROM leases WHERE lease_id = ?;", (row["lease_id"],)
            )
            return {
                "released_count": 1,
                "released_ids": [row["lease_id"]],
                "released": self._row_to_lease(row),
            }

    def list_leases(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        if owner_id is not None:
            _wrap_protocol(validate_agent_id, owner_id)
        now = float(self._now())
        with self._write_tx():
            self._purge_expired_leases_locked(now)
            if owner_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM leases WHERE owner_id = ? ORDER BY acquired_at ASC;",
                    (owner_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM leases ORDER BY acquired_at ASC;"
                ).fetchall()
        return [self._row_to_lease(r) for r in rows]

    def check_paths(
        self,
        paths: list[str],
        *,
        agent_id: str | None = None,
        scope: str = "file",
    ) -> dict[str, Any]:
        if not paths:
            raise ValidationError("validation_error", "paths cannot be empty")
        sc = _wrap_protocol(validate_scope, scope)
        aid = _wrap_protocol(validate_agent_id, agent_id) if agent_id else None
        norm_items = [
            (
                p,
                _wrap_protocol(
                    normalize_resource,
                    p,
                    scope=sc,
                    case_insensitive=self.case_insensitive,
                ),
            )
            for p in paths
        ]
        now = float(self._now())

        with self._write_tx():
            self._purge_expired_leases_locked(now)
            active = [
                self._row_to_lease(r)
                for r in self._conn.execute("SELECT * FROM leases;").fetchall()
            ]

        blocked: list[dict[str, Any]] = []
        owned_by_self: list[dict[str, Any]] = []
        free: list[str] = []

        for orig, norm in norm_items:
            peer_hits = []
            self_hits = []
            for lease in active:
                if paths_overlap(norm, sc, lease["resource_norm"], lease["scope"]):
                    if aid is not None and lease["owner_id"] == aid:
                        self_hits.append(lease)
                    else:
                        peer_hits.append(lease)
            if peer_hits:
                blocked.append(
                    {
                        "path": orig,
                        "resource_norm": norm,
                        "conflicts": peer_hits,
                    }
                )
            elif self_hits:
                owned_by_self.append(
                    {
                        "path": orig,
                        "resource_norm": norm,
                        "leases": self_hits,
                    }
                )
            else:
                free.append(norm)

        return {
            "all_clear": len(blocked) == 0,
            "agent_id": aid,
            "blocked": blocked,
            "owned_by_self": owned_by_self,
            "free": free,
        }

    # ------------------------------------------------------------------
    # Shared Context KV
    # ------------------------------------------------------------------
    def _row_to_context(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "key": row["key"],
            "value": json.loads(row["value_json"]),
            "updated_by": row["updated_by"],
            "version": int(row["version"]),
            "updated_at": float(row["updated_at"]),
        }

    def put_context(
        self,
        key: str,
        value: Any,
        updated_by: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("validation_error", "Context key cannot be empty")
        k = key.strip()
        actor = _wrap_protocol(validate_agent_id, updated_by)
        val_json = json.dumps(value)
        now = float(self._now())

        with self._write_tx():
            existing = self._conn.execute(
                "SELECT * FROM context_kv WHERE key = ?;", (k,)
            ).fetchone()
            current_ver = int(existing["version"]) if existing else 0
            if expected_version is not None and int(expected_version) != current_ver:
                raise ConflictError(
                    "version_conflict",
                    f"Context key '{k}' is at version {current_ver}, expected {expected_version}",
                    details={
                        "key": k,
                        "current_version": current_ver,
                        "expected_version": int(expected_version),
                    },
                )
            new_ver = current_ver + 1
            self._conn.execute(
                """
                INSERT INTO context_kv (key, value_json, updated_by, version, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_by = excluded.updated_by,
                    version = excluded.version,
                    updated_at = excluded.updated_at;
                """,
                (k, val_json, actor, new_ver, now),
            )
            row = self._conn.execute(
                "SELECT * FROM context_kv WHERE key = ?;", (k,)
            ).fetchone()
        return self._row_to_context(row)

    def get_context(self, key: str) -> dict[str, Any]:
        k = key.strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM context_kv WHERE key = ?;", (k,)
            ).fetchone()
        if not row:
            raise NotFoundError("context_not_found", f"Context key '{k}' not found")
        return self._row_to_context(row)

    def list_context(self, *, prefix: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if prefix:
                escaped = (
                    str(prefix)
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                rows = self._conn.execute(
                    "SELECT * FROM context_kv WHERE key LIKE ? ESCAPE '\\' ORDER BY key ASC;",
                    (f"{escaped}%",),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM context_kv ORDER BY key ASC;"
                ).fetchall()
        return [self._row_to_context(r) for r in rows]

    # ------------------------------------------------------------------
    # Snapshot & Health Counts
    # ------------------------------------------------------------------
    def snapshot(self, *, agent_id: str | None = None) -> dict[str, Any]:
        aid = _wrap_protocol(validate_agent_id, agent_id) if agent_id else None
        now = float(self._now())
        with self._write_tx():
            self._purge_expired_leases_locked(now)
            agent_rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY registered_at ASC;"
            ).fetchall()
            lease_rows = self._conn.execute(
                "SELECT * FROM leases ORDER BY acquired_at ASC;"
            ).fetchall()
            task_rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at ASC, task_id ASC;"
            ).fetchall()
            ctx_rows = self._conn.execute(
                "SELECT key, version, updated_by, updated_at FROM context_kv ORDER BY key ASC;"
            ).fetchall()
            unread = self._count_unread_locked(aid) if aid else None

        agents = [self._row_to_agent(r, now) for r in agent_rows]
        return {
            "server_time": now,
            "case_insensitive": self.case_insensitive,
            "agents": agents,
            "online_agents": [a["agent_id"] for a in agents if a["is_online"]],
            "active_leases": [self._row_to_lease(r) for r in lease_rows],
            "tasks": [self._row_to_task(r) for r in task_rows],
            "context_keys": [
                {
                    "key": r["key"],
                    "version": int(r["version"]),
                    "updated_by": r["updated_by"],
                    "updated_at": float(r["updated_at"]),
                }
                for r in ctx_rows
            ],
            "unread_count": unread,
        }

    # ------------------------------------------------------------------
    # Full State Export / Import (Host <-> Peer Handoff)
    # ------------------------------------------------------------------
    def export_state(self) -> dict[str, Any]:
        """Export full database tables inside a serialized transaction for host handoff."""
        now = float(self._now())
        table_names = (
            "agents",
            "messages",
            "agent_cursors",
            "tasks",
            "leases",
            "context_kv",
            "counters",
        )
        tables: dict[str, list[dict[str, Any]]] = {}
        with self._write_tx():
            self._purge_expired_leases_locked(now)
            for name in table_names:
                rows = self._conn.execute(f"SELECT * FROM {name};").fetchall()
                tables[name] = [dict(r) for r in rows]
        return {
            "format_version": 1,
            "exported_at": now,
            "case_insensitive": self.case_insensitive,
            "tables": tables,
        }

    def import_state(
        self,
        bundle: dict[str, Any],
        *,
        replace: bool = True,
    ) -> dict[str, Any]:
        """Import a state bundle produced by export_state() into this store."""
        if not isinstance(bundle, dict) or bundle.get("format_version") != 1:
            raise ValidationError(
                "validation_error", "Invalid state bundle: expected format_version == 1"
            )
        tables = bundle.get("tables")
        if not isinstance(tables, dict):
            raise ValidationError("validation_error", "Invalid state bundle: missing 'tables' dict")

        columns_map: dict[str, tuple[str, ...]] = {
            "agents": (
                "agent_id",
                "machine_name",
                "role",
                "branch",
                "status",
                "capabilities_json",
                "registered_at",
                "last_heartbeat",
                "ttl_sec",
            ),
            "messages": (
                "id",
                "sender_id",
                "recipient_id",
                "msg_type",
                "body",
                "metadata_json",
                "created_at",
            ),
            "agent_cursors": ("agent_id", "last_read_msg_id"),
            "tasks": (
                "task_id",
                "title",
                "description",
                "scope_paths_json",
                "status",
                "owner_id",
                "created_by",
                "created_at",
                "updated_at",
                "result_summary",
            ),
            "leases": (
                "lease_id",
                "resource",
                "resource_norm",
                "scope",
                "owner_id",
                "reason",
                "fencing_token",
                "acquired_at",
                "expires_at",
            ),
            "context_kv": ("key", "value_json", "updated_by", "version", "updated_at"),
            "counters": ("name", "val"),
        }

        counts: dict[str, int] = {}
        with self._write_tx():
            if replace:
                for name in columns_map:
                    self._conn.execute(f"DELETE FROM {name};")
            for name, cols in columns_map.items():
                raw_rows = tables.get(name) or []
                if not isinstance(raw_rows, list):
                    raise ValidationError(
                        "validation_error", f"Table '{name}' in bundle must be a list"
                    )
                placeholders = ", ".join("?" for _ in cols)
                col_list = ", ".join(cols)
                verb = "INSERT OR REPLACE"
                sql = f"{verb} INTO {name} ({col_list}) VALUES ({placeholders});"
                inserted = 0
                for row in raw_rows:
                    if not isinstance(row, dict):
                        raise ValidationError(
                            "validation_error", f"Invalid row in table '{name}'"
                        )
                    vals = [row.get(c) for c in cols]
                    self._conn.execute(sql, vals)
                    inserted += 1
                counts[name] = inserted

            # Ensure essential counter rows always exist
            self._conn.execute("INSERT OR IGNORE INTO counters(name, val) VALUES ('fencing_seq', 0);")
            self._conn.execute("INSERT OR IGNORE INTO counters(name, val) VALUES ('task_seq', 0);")

        return {"imported": True, "replace": replace, "counts": counts}
