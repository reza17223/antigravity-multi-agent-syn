"""Agent presence, messages, tasks, shared context, snapshots, and state handoff."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any

from agsync.core.errors import ConflictError, NotFoundError, ValidationError, wrap_protocol
from agsync.core.locks import row_to_lease
from agsync.db import SyncDatabase
from agsync.protocol import (
    DEFAULT_AGENT_TTL_SEC,
    DEFAULT_SYNC_WAIT_SEC,
    MAX_SYNC_WAIT_SEC,
    normalize_resource,
    validate_agent_id,
    validate_agent_status,
    validate_msg_type,
    validate_recipient_id,
    validate_task_status,
)

MAX_EVENT_ROWS = 1000
DEFAULT_EVENT_TTL_SEC = 86400.0


def _row_to_agent(row: sqlite3.Row, now: float) -> dict[str, Any]:
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


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "sender_id": row["sender_id"],
        "recipient_id": row["recipient_id"],
        "msg_type": row["msg_type"],
        "body": row["body"],
        "metadata": json.loads(row["metadata_json"]),
        "created_at": float(row["created_at"]),
    }


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
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


def _row_to_context(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "value": json.loads(row["value_json"]),
        "updated_by": row["updated_by"],
        "version": int(row["version"]),
        "updated_at": float(row["updated_at"]),
    }


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "boot_id": row["boot_id"],
        "event_type": row["event_type"],
        "actor_id": row["actor_id"],
        "target_id": row["target_id"],
        "summary": row["summary"],
        "payload": json.loads(row["payload_json"]),
        "created_at": float(row["created_at"]),
    }


class EventService:
    """Coordination operations for agents, messages, tasks, context, events, and state handoff."""

    def __init__(self, db: SyncDatabase) -> None:
        self.db = db

    @property
    def boot_id(self) -> str:
        return self.db.boot_id

    def wake_waiters(self, *, closing: bool = True) -> None:
        self.db.wake_waiters(closing=closing)

    def record_event_locked(
        self,
        event_type: str,
        actor_id: str,
        *,
        target_id: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> int:
        """Insert a coordination event inside an active write_tx() and enforce the 1000-row ring buffer."""
        ts = float(self.db.now_fn()) if created_at is None else float(created_at)
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        cur = self.db.conn.execute(
            """
            INSERT INTO events (
                boot_id, event_type, actor_id, target_id, summary, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                self.db.boot_id,
                str(event_type),
                str(actor_id),
                str(target_id),
                str(summary),
                payload_json,
                ts,
            ),
        )
        event_id = int(cur.lastrowid)
        self.db.conn.execute(
            """
            DELETE FROM events
            WHERE id <= (
                SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET 1000
            );
            """
        )
        return event_id

    def purge_events(
        self,
        *,
        max_age_sec: float = DEFAULT_EVENT_TTL_SEC,
        max_rows: int = MAX_EVENT_ROWS,
    ) -> int:
        """Prune expired events by age and enforce max_rows cap."""
        now = float(self.db.now_fn())
        cutoff = now - float(max_age_sec)
        cap = max(0, int(max_rows))
        deleted = 0
        with self.db.write_tx():
            cur1 = self.db.conn.execute(
                "DELETE FROM events WHERE created_at <= ?;",
                (cutoff,),
            )
            deleted += max(0, int(cur1.rowcount))
            if cap == 0:
                cur2 = self.db.conn.execute("DELETE FROM events;")
            else:
                cur2 = self.db.conn.execute(
                    """
                    DELETE FROM events
                    WHERE id <= (
                        SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?
                    );
                    """,
                    (cap,),
                )
            deleted += max(0, int(cur2.rowcount))
        return deleted

    def list_events(
        self,
        *,
        since_id: int = 0,
        boot_id: str | None = None,
        limit: int = 100,
        event_types: list[str] | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(since_id, bool):
            raise ValidationError("validation_error", "since_id must be a non-negative integer")
        since = int(since_id)
        if since < 0:
            raise ValidationError("validation_error", "since_id must be >= 0")

        if isinstance(limit, bool):
            raise ValidationError("validation_error", "limit must be an integer between 1 and 1000")
        lim = int(limit)
        if lim < 1 or lim > 1000:
            raise ValidationError("validation_error", "limit must be between 1 and 1000")

        clean_boot = boot_id.strip() if isinstance(boot_id, str) and boot_id.strip() else None

        types_clean: list[str] | None = None
        if event_types is not None:
            if not isinstance(event_types, (list, tuple)):
                raise ValidationError("validation_error", "event_types must be a list of strings")
            parsed_types = [str(t).strip() for t in event_types if isinstance(t, str) and t.strip()]
            if len(parsed_types) != len(event_types):
                raise ValidationError("validation_error", "event_types must contain non-empty strings")
            if parsed_types:
                types_clean = parsed_types

        aid: str | None = None
        if agent_id is not None and str(agent_id).strip():
            aid = wrap_protocol(validate_agent_id, str(agent_id))

        clauses: list[str] = []
        params: list[Any] = []
        if types_clean:
            placeholders = ", ".join("?" for _ in types_clean)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(types_clean)
        if aid is not None:
            clauses.append("(actor_id = ? OR target_id = ? OR target_id = '*')")
            params.extend([aid, aid])

        with self.db.read_tx():
            bounds = self.db.conn.execute(
                "SELECT MIN(id) AS min_id, COALESCE(MAX(id), 0) AS latest_id FROM events;"
            ).fetchone()
            min_id = int(bounds["min_id"]) if bounds["min_id"] is not None else None
            latest_id = int(bounds["latest_id"])
            current_boot_id = self.db.boot_id

            reset = (
                bool(clean_boot and clean_boot != current_boot_id)
                or (since > latest_id)
                or (min_id is not None and since < (min_id - 1))
            )

            if reset:
                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = self.db.conn.execute(
                    f"""
                    SELECT * FROM (
                        SELECT * FROM events
                        {where_sql}
                        ORDER BY id DESC
                        LIMIT ?
                    ) ORDER BY id ASC;
                    """,
                    [*params, lim],
                ).fetchall()
            else:
                inc_clauses = ["id > ?", *clauses]
                where_sql = f"WHERE {' AND '.join(inc_clauses)}"
                rows = self.db.conn.execute(
                    f"""
                    SELECT * FROM events
                    {where_sql}
                    ORDER BY id ASC
                    LIMIT ?;
                    """,
                    [since, *params, lim],
                ).fetchall()

        return {
            "boot_id": current_boot_id,
            "latest_id": latest_id,
            "reset": reset,
            "events": [_row_to_event(r) for r in rows],
        }

    def wait_for_events(
        self,
        *,
        since_id: int = 0,
        boot_id: str | None = None,
        timeout_sec: float = DEFAULT_SYNC_WAIT_SEC,
        limit: int = 100,
        event_types: list[str] | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(timeout_sec, bool):
            raise ValidationError("validation_error", "timeout_sec must be a number")
        wait_val = float(timeout_sec)
        if not math.isfinite(wait_val) or wait_val < 0:
            raise ValidationError("validation_error", "timeout_sec must be a finite number >= 0")
        wait = min(wait_val, MAX_SYNC_WAIT_SEC)
        deadline = time.monotonic() + wait

        last_res: dict[str, Any] | None = None
        while True:
            with self.db.event_cond:
                if self.db._closed:
                    if last_res is not None:
                        return {**last_res, "timed_out": True}
                    return {
                        "boot_id": self.db.boot_id,
                        "latest_id": 0,
                        "reset": False,
                        "timed_out": True,
                        "events": [],
                    }
                seen_seq = self.db._event_seq

            res = self.list_events(
                since_id=since_id,
                boot_id=boot_id,
                limit=limit,
                event_types=event_types,
                agent_id=agent_id,
            )
            last_res = res
            if res["reset"] or res["events"]:
                return {**res, "timed_out": False}

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**res, "timed_out": True}

            with self.db.event_cond:
                if self.db._closed:
                    return {**res, "timed_out": True}
                if self.db._event_seq == seen_seq:
                    self.db.event_cond.wait(timeout=remaining)
                    if self.db._closed:
                        return {**res, "timed_out": True}

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
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
        aid = wrap_protocol(validate_agent_id, agent_id)
        st = wrap_protocol(validate_agent_status, status)
        ttl = int(ttl_sec)
        if ttl <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        caps_json = json.dumps(list(capabilities or []))
        now = float(self.db.now_fn())

        with self.db.write_tx():
            existing = self.db.conn.execute(
                "SELECT registered_at FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
            is_new = existing is None
            reg_at = float(existing["registered_at"]) if existing else now
            self.db.conn.execute(
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
            row = self.db.conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
            self.record_event_locked(
                "agent_registered",
                aid,
                target_id=aid,
                summary=f"Agent {aid} registered ({st})",
                payload={
                    "role": str(role),
                    "branch": str(branch),
                    "status": st,
                    "is_new": is_new,
                },
                created_at=now,
            )
        self.db.notify_events()
        return _row_to_agent(row, now)

    def heartbeat_agent(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        branch: str | None = None,
        ttl_sec: int | None = None,
    ) -> dict[str, Any]:
        aid = wrap_protocol(validate_agent_id, agent_id)
        if status is not None:
            wrap_protocol(validate_agent_status, status)
        if ttl_sec is not None and int(ttl_sec) <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        now = float(self.db.now_fn())

        with self.db.write_tx():
            row = self.db.conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
            if not row:
                raise NotFoundError("agent_not_found", f"Agent '{aid}' is not registered")
            new_status = status if status is not None else row["status"]
            new_branch = branch if branch is not None else row["branch"]
            new_ttl = int(ttl_sec) if ttl_sec is not None else int(row["ttl_sec"])
            self.db.conn.execute(
                """
                UPDATE agents
                SET status = ?, branch = ?, ttl_sec = ?, last_heartbeat = ?
                WHERE agent_id = ?;
                """,
                (new_status, str(new_branch), new_ttl, now, aid),
            )
            updated = self.db.conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?;", (aid,)
            ).fetchone()
            self.record_event_locked(
                "agent_heartbeat",
                aid,
                target_id=aid,
                summary=f"Agent {aid} heartbeat ({new_status})",
                payload={"status": new_status, "branch": str(new_branch)},
                created_at=now,
            )
        self.db.notify_events()
        return _row_to_agent(updated, now)

    def list_agents(self, *, include_offline: bool = True) -> list[dict[str, Any]]:
        now = float(self.db.now_fn())
        with self.db.read_tx():
            rows = self.db.conn.execute(
                "SELECT * FROM agents ORDER BY registered_at ASC;"
            ).fetchall()
        agents = [_row_to_agent(r, now) for r in rows]
        if not include_offline:
            agents = [a for a in agents if a["is_online"]]
        return agents

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def send_message(
        self,
        sender_id: str,
        recipient_id: str,
        body: str,
        *,
        msg_type: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sid = wrap_protocol(validate_agent_id, sender_id)
        rid = wrap_protocol(validate_recipient_id, recipient_id)
        mtype = wrap_protocol(validate_msg_type, msg_type)
        if not isinstance(body, str) or not body.strip():
            raise ValidationError("validation_error", "Message body cannot be empty")
        meta_json = json.dumps(metadata or {})
        now = float(self.db.now_fn())

        with self.db.write_tx():
            cur = self.db.conn.execute(
                """
                INSERT INTO messages (sender_id, recipient_id, msg_type, body, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (sid, rid, mtype, body, meta_json, now),
            )
            msg_id = int(cur.lastrowid)
            row = self.db.conn.execute(
                "SELECT * FROM messages WHERE id = ?;", (msg_id,)
            ).fetchone()
            self.record_event_locked(
                "message_sent",
                sid,
                target_id=rid,
                summary=f"Message from {sid} to {rid} ({mtype})",
                payload={"message_id": msg_id, "msg_type": mtype},
                created_at=now,
            )
        self.db.notify_events()
        return _row_to_message(row)

    def _get_cursor_locked(self, agent_id: str) -> int:
        row = self.db.conn.execute(
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
        aid = wrap_protocol(validate_agent_id, agent_id)
        lim = max(1, min(int(limit), 500))

        acked_event = False
        ctx = self.db.write_tx() if auto_ack else self.db.read_tx()
        with ctx:
            cursor = self._get_cursor_locked(aid)
            if unread_only:
                effective_since = max(cursor, int(since_id or 0))
                rows = self.db.conn.execute(
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
                rows = self.db.conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE id > ?
                      AND (recipient_id = ? OR recipient_id = '*')
                    ORDER BY id ASC
                    LIMIT ?;
                    """,
                    (effective_since, aid, lim),
                ).fetchall()

            msgs = [_row_to_message(r) for r in rows]
            if auto_ack and msgs:
                max_id = max(m["id"] for m in msgs)
                new_cursor = max(cursor, max_id)
                self.db.conn.execute(
                    """
                    INSERT INTO agent_cursors (agent_id, last_read_msg_id)
                    VALUES (?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        last_read_msg_id = MAX(agent_cursors.last_read_msg_id, excluded.last_read_msg_id);
                    """,
                    (aid, new_cursor),
                )
                cursor = new_cursor
                self.record_event_locked(
                    "messages_acked",
                    aid,
                    target_id=aid,
                    summary=f"Agent {aid} acknowledged {len(msgs)} message(s) up to {cursor}",
                    payload={"last_read_msg_id": cursor, "count": len(msgs)},
                )
                acked_event = True

        if acked_event:
            self.db.notify_events()

        return {
            "agent_id": aid,
            "cursor": cursor,
            "messages": msgs,
        }

    def ack_messages(self, agent_id: str, up_to_id: int) -> dict[str, Any]:
        aid = wrap_protocol(validate_agent_id, agent_id)
        target_id = int(up_to_id)
        if target_id < 0:
            raise ValidationError("validation_error", "up_to_id must be >= 0")
        with self.db.write_tx():
            self.db.conn.execute(
                """
                INSERT INTO agent_cursors (agent_id, last_read_msg_id)
                VALUES (?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    last_read_msg_id = MAX(agent_cursors.last_read_msg_id, excluded.last_read_msg_id);
                """,
                (aid, target_id),
            )
            cursor = self._get_cursor_locked(aid)
            self.record_event_locked(
                "messages_acked",
                aid,
                target_id=aid,
                summary=f"Agent {aid} acknowledged messages up to {cursor}",
                payload={"last_read_msg_id": cursor},
            )
        self.db.notify_events()
        return {"agent_id": aid, "last_read_msg_id": cursor}

    def _count_unread_locked(self, agent_id: str) -> int:
        cursor = self._get_cursor_locked(agent_id)
        row = self.db.conn.execute(
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
    def create_task(
        self,
        title: str,
        created_by: str,
        *,
        description: str = "",
        scope_paths: list[str] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        creator = wrap_protocol(validate_agent_id, created_by)
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
            wrap_protocol(
                normalize_resource,
                p,
                scope="file",
                case_insensitive=self.db.case_insensitive,
            )
            for p in (scope_paths or [])
        ]
        now = float(self.db.now_fn())

        with self.db.write_tx():
            if tid_clean is not None:
                tid = tid_clean
                existing = self.db.conn.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?;", (tid,)
                ).fetchone()
                if existing:
                    raise ConflictError("task_exists", f"Task '{tid}' already exists")
            else:
                while True:
                    seq = self.db.next_counter("task_seq")
                    tid = f"task-{seq:03d}"
                    existing = self.db.conn.execute(
                        "SELECT 1 FROM tasks WHERE task_id = ?;", (tid,)
                    ).fetchone()
                    if not existing:
                        break

            self.db.conn.execute(
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
            row = self.db.conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
            self.record_event_locked(
                "task_created",
                creator,
                target_id=tid,
                summary=f"Task {tid} created: {title.strip()}",
                payload={
                    "task_id": tid,
                    "title": title.strip(),
                    "scope_paths": norm_paths,
                },
                created_at=now,
            )
        self.db.notify_events()
        return _row_to_task(row)

    def list_tasks(
        self, *, status: str | None = None, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            wrap_protocol(validate_task_status, status)
            clauses.append("status = ?")
            params.append(status)
        if owner_id is not None:
            wrap_protocol(validate_agent_id, owner_id)
            clauses.append("owner_id = ?")
            params.append(owner_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.read_tx():
            rows = self.db.conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at ASC, task_id ASC;",
                params,
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def claim_task(self, task_id: str, agent_id: str) -> dict[str, Any]:
        aid = wrap_protocol(validate_agent_id, agent_id)
        tid = task_id.strip()
        now = float(self.db.now_fn())

        result: dict[str, Any]
        did_claim = False
        with self.db.write_tx():
            row = self.db.conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
            if not row:
                raise NotFoundError("task_not_found", f"Task '{tid}' not found")
            current_owner = row["owner_id"]
            current_status = row["status"]
            if current_owner == aid and current_status in ("claimed", "in_progress"):
                result = _row_to_task(row)
            elif current_status != "open" or (current_owner is not None and current_owner != aid):
                raise ConflictError(
                    "task_already_claimed",
                    f"Task '{tid}' cannot be claimed (status={current_status}, owner={current_owner})",
                    details={
                        "task_id": tid,
                        "current_status": current_status,
                        "current_owner": current_owner,
                    },
                )
            else:
                self.db.conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'claimed', owner_id = ?, updated_at = ?
                    WHERE task_id = ?;
                    """,
                    (aid, now, tid),
                )
                updated = self.db.conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
                ).fetchone()
                self.record_event_locked(
                    "task_claimed",
                    aid,
                    target_id=tid,
                    summary=f"Task {tid} claimed by {aid}",
                    payload={"task_id": tid, "status": "claimed"},
                    created_at=now,
                )
                did_claim = True
                result = _row_to_task(updated)
        if did_claim:
            self.db.notify_events()
        return result

    def update_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        status: str | None = None,
        result_summary: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        aid = wrap_protocol(validate_agent_id, agent_id)
        tid = task_id.strip()
        if status is not None:
            wrap_protocol(validate_task_status, status)
        now = float(self.db.now_fn())

        with self.db.write_tx():
            row = self.db.conn.execute(
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
                str(result_summary) if result_summary is not None else row["result_summary"]
            )
            self.db.conn.execute(
                """
                UPDATE tasks
                SET status = ?, owner_id = ?, result_summary = ?, updated_at = ?
                WHERE task_id = ?;
                """,
                (new_status, new_owner, new_summary, now, tid),
            )
            updated = self.db.conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
            ).fetchone()
            self.record_event_locked(
                "task_updated",
                aid,
                target_id=tid,
                summary=f"Task {tid} updated to {new_status}",
                payload={
                    "task_id": tid,
                    "status": new_status,
                    "owner_id": new_owner,
                },
                created_at=now,
            )
        self.db.notify_events()
        return _row_to_task(updated)

    # ------------------------------------------------------------------
    # Shared Context KV
    # ------------------------------------------------------------------
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
        actor = wrap_protocol(validate_agent_id, updated_by)
        val_json = json.dumps(value)
        now = float(self.db.now_fn())

        with self.db.write_tx():
            existing = self.db.conn.execute(
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
            self.db.conn.execute(
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
            row = self.db.conn.execute(
                "SELECT * FROM context_kv WHERE key = ?;", (k,)
            ).fetchone()
            self.record_event_locked(
                "context_updated",
                actor,
                target_id=k,
                summary=f"Context '{k}' updated to v{new_ver}",
                payload={"key": k, "version": new_ver},
                created_at=now,
            )
        self.db.notify_events()
        return _row_to_context(row)

    def get_context(self, key: str) -> dict[str, Any]:
        k = key.strip()
        with self.db.read_tx():
            row = self.db.conn.execute(
                "SELECT * FROM context_kv WHERE key = ?;", (k,)
            ).fetchone()
        if not row:
            raise NotFoundError("context_not_found", f"Context key '{k}' not found")
        return _row_to_context(row)

    def list_context(self, *, prefix: str | None = None) -> list[dict[str, Any]]:
        with self.db.read_tx():
            if prefix:
                escaped = (
                    str(prefix)
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                rows = self.db.conn.execute(
                    "SELECT * FROM context_kv WHERE key LIKE ? ESCAPE '\\' ORDER BY key ASC;",
                    (f"{escaped}%",),
                ).fetchall()
            else:
                rows = self.db.conn.execute(
                    "SELECT * FROM context_kv ORDER BY key ASC;"
                ).fetchall()
        return [_row_to_context(r) for r in rows]

    # ------------------------------------------------------------------
    # Snapshot & State Export / Import
    # ------------------------------------------------------------------
    def snapshot(self, *, agent_id: str | None = None) -> dict[str, Any]:
        aid = wrap_protocol(validate_agent_id, agent_id) if agent_id else None
        now = float(self.db.now_fn())
        with self.db.read_tx():
            agent_rows = self.db.conn.execute(
                "SELECT * FROM agents ORDER BY registered_at ASC;"
            ).fetchall()
            lease_rows = self.db.conn.execute(
                "SELECT * FROM leases WHERE expires_at > ? ORDER BY acquired_at ASC;",
                (now,),
            ).fetchall()
            task_rows = self.db.conn.execute(
                "SELECT * FROM tasks ORDER BY created_at ASC, task_id ASC;"
            ).fetchall()
            ctx_rows = self.db.conn.execute(
                "SELECT key, version, updated_by, updated_at FROM context_kv ORDER BY key ASC;"
            ).fetchall()
            unread = self._count_unread_locked(aid) if aid else None
            ev_bounds = self.db.conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest_id FROM events;"
            ).fetchone()
            latest_event_id = int(ev_bounds["latest_id"]) if ev_bounds else 0
            tail_rows = self.db.conn.execute(
                "SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT 30) ORDER BY id ASC;"
            ).fetchall()

        agents = [_row_to_agent(r, now) for r in agent_rows]
        return {
            "server_time": now,
            "boot_id": self.db.boot_id,
            "latest_event_id": latest_event_id,
            "latest_events": [_row_to_event(r) for r in tail_rows],
            "case_insensitive": self.db.case_insensitive,
            "agents": agents,
            "online_agents": [a["agent_id"] for a in agents if a["is_online"]],
            "active_leases": [row_to_lease(r) for r in lease_rows],
            "tasks": [_row_to_task(r) for r in task_rows],
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

    def export_state(self) -> dict[str, Any]:
        """Export full database tables inside a consistent read transaction."""
        now = float(self.db.now_fn())
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
        with self.db.read_tx():
            for name in table_names:
                if name == "leases":
                    rows = self.db.conn.execute(
                        "SELECT * FROM leases WHERE expires_at > ?;", (now,)
                    ).fetchall()
                else:
                    rows = self.db.conn.execute(f"SELECT * FROM {name};").fetchall()
                tables[name] = [dict(r) for r in rows]
        return {
            "format_version": 1,
            "exported_at": now,
            "case_insensitive": self.db.case_insensitive,
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

        backup_path: str | None = None
        if replace and self.db.db_path != ":memory:":
            bak = self.db.backup_to(f"{self.db.db_path}.bak")
            if bak is not None:
                backup_path = str(bak)

        counts: dict[str, int] = {}
        with self.db.write_tx():
            existing_counters = {
                r["name"]: int(r["val"])
                for r in self.db.conn.execute("SELECT name, val FROM counters;").fetchall()
            }
            if replace:
                for name in columns_map:
                    if name == "counters":
                        continue
                    self.db.conn.execute(f"DELETE FROM {name};")

            max_lease_fencing = 0
            for name, cols in columns_map.items():
                raw_rows = tables.get(name) or []
                if not isinstance(raw_rows, list):
                    raise ValidationError(
                        "validation_error", f"Table '{name}' in bundle must be a list"
                    )
                if name == "counters":
                    inserted = 0
                    for row in raw_rows:
                        if not isinstance(row, dict):
                            raise ValidationError(
                                "validation_error", f"Invalid row in table '{name}'"
                            )
                        cname = row.get("name")
                        cval = int(row.get("val") or 0)
                        prev = existing_counters.get(str(cname), 0)
                        merged_val = max(prev, cval)
                        self.db.conn.execute(
                            """
                            INSERT INTO counters (name, val) VALUES (?, ?)
                            ON CONFLICT(name) DO UPDATE SET val = MAX(counters.val, excluded.val);
                            """,
                            (cname, merged_val),
                        )
                        inserted += 1
                    counts[name] = inserted
                    continue

                placeholders = ", ".join("?" for _ in cols)
                col_list = ", ".join(cols)
                sql = f"INSERT OR REPLACE INTO {name} ({col_list}) VALUES ({placeholders});"
                inserted = 0
                for row in raw_rows:
                    if not isinstance(row, dict):
                        raise ValidationError(
                            "validation_error", f"Invalid row in table '{name}'"
                        )
                    if name == "leases":
                        try:
                            max_lease_fencing = max(
                                max_lease_fencing, int(row.get("fencing_token") or 0)
                            )
                        except (ValueError, TypeError):
                            pass
                    vals = [row.get(c) for c in cols]
                    self.db.conn.execute(sql, vals)
                    inserted += 1
                counts[name] = inserted

            self.db.conn.execute(
                "INSERT OR IGNORE INTO counters(name, val) VALUES ('fencing_seq', 0);"
            )
            self.db.conn.execute(
                "INSERT OR IGNORE INTO counters(name, val) VALUES ('task_seq', 0);"
            )
            if max_lease_fencing > 0:
                self.db.conn.execute(
                    "UPDATE counters SET val = MAX(val, ?) WHERE name = 'fencing_seq';",
                    (max_lease_fencing,),
                )
            self.record_event_locked(
                "state_imported",
                "system",
                target_id="",
                summary="State bundle imported",
                payload={"replace": replace, "counts": counts},
            )

        self.db.notify_events()
        result: dict[str, Any] = {"imported": True, "replace": replace, "counts": counts}
        if backup_path is not None:
            result["backup_path"] = backup_path
        return result
