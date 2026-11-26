"""Exclusive path/directory/symbol lease service backed by SyncDatabase."""

from __future__ import annotations

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

    def __init__(self, db: SyncDatabase) -> None:
        self.db = db

    def purge_expired_locked(self, now: float) -> int:
        cur = self.db.conn.execute("DELETE FROM leases WHERE expires_at <= ?;", (now,))
        return int(cur.rowcount)

    def purge_expired(self) -> int:
        now = float(self.db.now_fn())
        with self.db.write_tx():
            return self.purge_expired_locked(now)

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

        with self.db.write_tx():
            self.purge_expired_locked(now)
            rows = self.db.conn.execute("SELECT * FROM leases WHERE expires_at > ?;", (now,)).fetchall()

            conflicts: list[dict[str, Any]] = []
            exact_own_row: sqlite3.Row | None = None
            for row in rows:
                if row["owner_id"] == oid:
                    if row["resource_norm"] == norm and row["scope"] == sc:
                        exact_own_row = row
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
                return {"lease": row_to_lease(updated), "renewed": True}

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
            return {"lease": row_to_lease(created), "renewed": False}

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
        return row_to_lease(updated)

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

        with self.db.write_tx():
            self.purge_expired_locked(now)
            if release_all:
                rows = self.db.conn.execute(
                    "SELECT lease_id FROM leases WHERE owner_id = ?;", (oid,)
                ).fetchall()
                ids = [r["lease_id"] for r in rows]
                self.db.conn.execute("DELETE FROM leases WHERE owner_id = ?;", (oid,))
                return {"released_count": len(ids), "released_ids": ids}

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
            return {
                "released_count": 1,
                "released_ids": [row["lease_id"]],
                "released": row_to_lease(row),
            }

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
                blocked.append({"path": orig, "resource_norm": norm, "conflicts": peer_hits})
            elif self_hits:
                owned_by_self.append(
                    {"path": orig, "resource_norm": norm, "leases": self_hits}
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
