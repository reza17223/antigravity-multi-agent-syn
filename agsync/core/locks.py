"""Exclusive path/directory/symbol lease service backed by SyncDatabase."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from agsync.core.errors import ConflictError, NotFoundError, ValidationError, wrap_protocol
from agsync.db import SyncDatabase
from agsync.protocol import (
    DEFAULT_LEASE_TTL_SEC,
    normalize_resource,
    paths_overlap,
    validate_agent_id,
    validate_scope,
)


def row_to_lease(row: sqlite3.Row) -> dict[str, Any]:
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


class LockService:
    """Exclusive TTL lease acquisition, renewal, release, and path checking."""

    def __init__(self, db: SyncDatabase, *, events: Any = None) -> None:
        self.db = db
        self.events = events

    def _get_online_agent_ids_locked(self, now: float) -> set[str]:
        """Return the set of agent_ids currently online (active heartbeats) without starting a nested transaction."""
        try:
            rows = self.db.conn.execute("SELECT agent_id, last_heartbeat, ttl_sec FROM agents;").fetchall()
            return {
                r["agent_id"]
                for r in rows
                if now <= (float(r["last_heartbeat"]) + int(r["ttl_sec"]))
            }
        except Exception:
            return set()

    def _record_event_locked(
        self,
        event_type: str,
        actor_id: str,
        *,
        target_id: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> int:
        if self.events is not None:
            return int(
                self.events.record_event_locked(
                    event_type,
                    actor_id,
                    target_id=target_id,
                    summary=summary,
                    payload=payload,
                    created_at=created_at,
                )
            )
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

    def purge_expired_locked(self, now: float) -> int:
        expired_rows = self.db.conn.execute(
            "SELECT lease_id, resource_norm, scope, owner_id FROM leases WHERE expires_at <= ?;",
            (now,),
        ).fetchall()
        if not expired_rows:
            return 0
        cur = self.db.conn.execute("DELETE FROM leases WHERE expires_at <= ?;", (now,))
        count = int(cur.rowcount)
        for er in expired_rows:
            self._record_event_locked(
                "lease_expired",
                er["owner_id"],
                target_id=er["resource_norm"],
                summary=f"Lease {er['lease_id']} on {er['resource_norm']} expired",
                payload={
                    "lease_id": er["lease_id"],
                    "resource_norm": er["resource_norm"],
                    "scope": er["scope"],
                },
                created_at=now,
            )
        return count

    def purge_expired(self) -> int:
        now = float(self.db.now_fn())
        with self.db.write_tx():
            count = self.purge_expired_locked(now)
        if count > 0:
            self.db.notify_events()
        return count

    def acquire_lease(
        self,
        resource: str,
        owner_id: str,
        *,
        scope: str = "file",
        ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
        reason: str = "",
    ) -> dict[str, Any]:
        oid = wrap_protocol(validate_agent_id, owner_id)
        sc = wrap_protocol(validate_scope, scope)
        norm = wrap_protocol(
            normalize_resource,
            resource,
            scope=sc,
            case_insensitive=self.db.case_insensitive,
        )
        ttl = int(ttl_sec)
        if ttl <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        now = float(self.db.now_fn())
        expires_at = now + ttl

        result: dict[str, Any]
        with self.db.write_tx():
            self.purge_expired_locked(now)
            rows = self.db.conn.execute("SELECT * FROM leases WHERE expires_at > ?;", (now,)).fetchall()

            online_agents = self._get_online_agent_ids_locked(now)
            conflicts: list[dict[str, Any]] = []
            exact_own_row: sqlite3.Row | None = None
            for row in rows:
                if row["owner_id"] == oid:
                    if row["resource_norm"] == norm and row["scope"] == sc:
                        exact_own_row = row
                    continue
                # If online tracking has registered peers and the lock owner is OFFLINE:
                # The lease is considered abandoned/stale by the offline developer and does not block!
                if online_agents and row["owner_id"] not in online_agents:
                    continue
                if paths_overlap(norm, sc, row["resource_norm"], row["scope"]):
                    conflicts.append(row_to_lease(row))

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
                self.db.conn.execute(
                    """
                    UPDATE leases
                    SET resource = ?, reason = ?, expires_at = ?
                    WHERE lease_id = ?;
                    """,
                    (resource, new_reason, expires_at, exact_own_row["lease_id"]),
                )
                updated = self.db.conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ?;",
                    (exact_own_row["lease_id"],),
                ).fetchone()
                lease_dict = row_to_lease(updated)
                self._record_event_locked(
                    "lease_renewed",
                    oid,
                    target_id=norm,
                    summary=f"Lease renewed on {norm} ({sc}) by {oid}",
                    payload={
                        "lease_id": lease_dict["lease_id"],
                        "resource_norm": norm,
                        "scope": sc,
                        "fencing_token": lease_dict["fencing_token"],
                    },
                    created_at=now,
                )
                result = {"lease": lease_dict, "renewed": True}
            else:
                token = self.db.next_counter("fencing_seq")
                lid = f"lease-{uuid.uuid4().hex[:10]}"
                self.db.conn.execute(
                    """
                    INSERT INTO leases (
                        lease_id, resource, resource_norm, scope, owner_id,
                        reason, fencing_token, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (lid, resource, norm, sc, oid, str(reason), token, now, expires_at),
                )
                created = self.db.conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ?;", (lid,)
                ).fetchone()
                lease_dict = row_to_lease(created)
                self._record_event_locked(
                    "lease_acquired",
                    oid,
                    target_id=norm,
                    summary=f"Lease acquired on {norm} ({sc}) by {oid}",
                    payload={
                        "lease_id": lid,
                        "resource_norm": norm,
                        "scope": sc,
                        "fencing_token": lease_dict["fencing_token"],
                    },
                    created_at=now,
                )
                result = {"lease": lease_dict, "renewed": False}

        self.db.notify_events()
        return result

    def renew_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
    ) -> dict[str, Any]:
        oid = wrap_protocol(validate_agent_id, owner_id)
        ttl = int(ttl_sec)
        if ttl <= 0:
            raise ValidationError("validation_error", "ttl_sec must be > 0")
        if not lease_id and not resource:
            raise ValidationError("validation_error", "Provide either lease_id or resource")
        now = float(self.db.now_fn())
        expires_at = now + ttl

        with self.db.write_tx():
            self.purge_expired_locked(now)
            if lease_id:
                row = self.db.conn.execute(
                    "SELECT * FROM leases WHERE lease_id = ? AND expires_at > ?;",
                    (lease_id, now),
                ).fetchone()
            else:
                sc = wrap_protocol(validate_scope, scope)
                norm = wrap_protocol(
                    normalize_resource,
                    resource or "",
                    scope=sc,
                    case_insensitive=self.db.case_insensitive,
                )
                row = self.db.conn.execute(
                    "SELECT * FROM leases WHERE resource_norm = ? AND scope = ? AND expires_at > ?;",
                    (norm, sc, now),
                ).fetchone()
            if not row:
                raise NotFoundError("lease_not_found", "Active lease not found")
            if row["owner_id"] != oid:
                raise ConflictError(
                    "lease_not_owned",
                    f"Lease '{row['lease_id']}' is owned by '{row['owner_id']}'",
                    details={"lease": row_to_lease(row)},
                )
            self.db.conn.execute(
                "UPDATE leases SET expires_at = ? WHERE lease_id = ?;",
                (expires_at, row["lease_id"]),
            )
            updated = self.db.conn.execute(
                "SELECT * FROM leases WHERE lease_id = ?;", (row["lease_id"],)
            ).fetchone()
            lease_dict = row_to_lease(updated)
            self._record_event_locked(
                "lease_renewed",
                oid,
                target_id=lease_dict["resource_norm"],
                summary=f"Lease renewed on {lease_dict['resource_norm']} ({lease_dict['scope']}) by {oid}",
                payload={
                    "lease_id": lease_dict["lease_id"],
                    "resource_norm": lease_dict["resource_norm"],
                    "scope": lease_dict["scope"],
                    "fencing_token": lease_dict["fencing_token"],
                },
                created_at=now,
            )
        self.db.notify_events()
        return lease_dict

    def release_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        release_all: bool = False,
    ) -> dict[str, Any]:
        oid = wrap_protocol(validate_agent_id, owner_id)
        now = float(self.db.now_fn())

        result: dict[str, Any]
        notify = False
        with self.db.write_tx():
            expired_count = self.purge_expired_locked(now)
            if expired_count > 0:
                notify = True

            if release_all:
                rows = self.db.conn.execute(
                    "SELECT lease_id, resource_norm, scope FROM leases WHERE owner_id = ?;",
                    (oid,),
                ).fetchall()
                ids = [r["lease_id"] for r in rows]
                if ids:
                    self.db.conn.execute("DELETE FROM leases WHERE owner_id = ?;", (oid,))
                    for r in rows:
                        self._record_event_locked(
                            "lease_released",
                            oid,
                            target_id=r["resource_norm"],
                            summary=f"Lease {r['lease_id']} on {r['resource_norm']} released by {oid}",
                            payload={
                                "lease_id": r["lease_id"],
                                "resource_norm": r["resource_norm"],
                                "scope": r["scope"],
                            },
                            created_at=now,
                        )
                    notify = True
                result = {"released_count": len(ids), "released_ids": ids}
            else:
                if not lease_id and not resource:
                    raise ValidationError(
                        "validation_error",
                        "Provide lease_id, resource, or release_all=True",
                    )
                if lease_id:
                    row = self.db.conn.execute(
                        "SELECT * FROM leases WHERE lease_id = ? AND expires_at > ?;",
                        (lease_id, now),
                    ).fetchone()
                else:
                    sc = wrap_protocol(validate_scope, scope)
                    norm = wrap_protocol(
                        normalize_resource,
                        resource or "",
                        scope=sc,
                        case_insensitive=self.db.case_insensitive,
                    )
                    row = self.db.conn.execute(
                        "SELECT * FROM leases WHERE resource_norm = ? AND scope = ? AND owner_id = ? AND expires_at > ?;",
                        (norm, sc, oid, now),
                    ).fetchone()
                    if not row:
                        peer_row = self.db.conn.execute(
                            "SELECT * FROM leases WHERE resource_norm = ? AND scope = ? AND expires_at > ?;",
                            (norm, sc, now),
                        ).fetchone()
                        if peer_row:
                            row = peer_row
                if not row:
                    raise NotFoundError("lease_not_found", "Active lease not found")
                if row["owner_id"] != oid:
                    raise ConflictError(
                        "lease_not_owned",
                        f"Cannot release lease owned by '{row['owner_id']}'",
                        details={"lease": row_to_lease(row)},
                    )
                self.db.conn.execute("DELETE FROM leases WHERE lease_id = ?;", (row["lease_id"],))
                lease_dict = row_to_lease(row)
                self._record_event_locked(
                    "lease_released",
                    oid,
                    target_id=lease_dict["resource_norm"],
                    summary=f"Lease {lease_dict['lease_id']} on {lease_dict['resource_norm']} released by {oid}",
                    payload={
                        "lease_id": lease_dict["lease_id"],
                        "resource_norm": lease_dict["resource_norm"],
                        "scope": lease_dict["scope"],
                    },
                    created_at=now,
                )
                notify = True
                result = {
                    "released_count": 1,
                    "released_ids": [row["lease_id"]],
                    "released": lease_dict,
                }
        if notify:
            self.db.notify_events()
        return result

    def list_leases(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        if owner_id is not None:
            wrap_protocol(validate_agent_id, owner_id)
        now = float(self.db.now_fn())
        with self.db.read_tx():
            if owner_id is not None:
                rows = self.db.conn.execute(
                    "SELECT * FROM leases WHERE owner_id = ? AND expires_at > ? ORDER BY acquired_at ASC;",
                    (owner_id, now),
                ).fetchall()
            else:
                rows = self.db.conn.execute(
                    "SELECT * FROM leases WHERE expires_at > ? ORDER BY acquired_at ASC;",
                    (now,),
                ).fetchall()
        return [row_to_lease(r) for r in rows]

    def check_paths(
        self,
        paths: list[str],
        *,
        agent_id: str | None = None,
        scope: str = "file",
    ) -> dict[str, Any]:
        if not paths:
            raise ValidationError("validation_error", "paths cannot be empty")
        sc = wrap_protocol(validate_scope, scope)
        aid = wrap_protocol(validate_agent_id, agent_id) if agent_id else None
        norm_items = [
            (
                p,
                wrap_protocol(
                    normalize_resource,
                    p,
                    scope=sc,
                    case_insensitive=self.db.case_insensitive,
                ),
            )
            for p in paths
        ]
        now = float(self.db.now_fn())

        with self.db.read_tx():
            active = [
                row_to_lease(r)
                for r in self.db.conn.execute(
                    "SELECT * FROM leases WHERE expires_at > ?;", (now,)
                ).fetchall()
            ]

        online_agents = self._get_online_agent_ids_locked(now)
        # Solo mode is only active when at least one agent is registered and NO other peers are online
        other_peers = [a for a in online_agents if a != aid] if online_agents else None
        solo_mode = bool(online_agents and other_peers is not None and len(other_peers) == 0)

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
                        # Only consider conflict if lease owner is currently ONLINE!
                        if not online_agents or lease["owner_id"] in online_agents:
                            peer_hits.append(lease)
            if peer_hits and not solo_mode:
                blocked.append({"path": orig, "resource_norm": norm, "conflicts": peer_hits})
            elif self_hits:
                owned_by_self.append(
                    {"path": orig, "resource_norm": norm, "leases": self_hits}
                )
            else:
                free.append(norm)

        return {
            "all_clear": len(blocked) == 0,
            "solo_mode": solo_mode,
            "online_peers_count": len(other_peers) if other_peers is not None else None,
            "agent_id": aid,
            "blocked": blocked,
            "owned_by_self": owned_by_self,
            "free": free,
        }
