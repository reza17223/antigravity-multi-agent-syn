"""Facade composing SyncDatabase, LockService, and EventService for backward compatibility."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agsync.core.errors import ConflictError, NotFoundError, SyncError, ValidationError
from agsync.core.events import EventService
from agsync.core.locks import LockService
from agsync.db import SyncDatabase
from agsync.protocol import DEFAULT_AGENT_TTL_SEC, DEFAULT_LEASE_TTL_SEC

__all__ = [
    "ConflictError",
    "NotFoundError",
    "SyncError",
    "SyncStore",
    "ValidationError",
]


class SyncStore:
    """Thread-safe, crash-safe SQLite WAL store for multi-agent coordination."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        case_insensitive: bool | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.db = SyncDatabase(
            db_path,
            case_insensitive=case_insensitive,
            time_fn=time_fn,
        )
        self.db_path = self.db.db_path
        self.case_insensitive = self.db.case_insensitive
        self._conn = self.db.conn
        self._lock = self.db.lock
        self.locks = LockService(self.db)
        self.events = EventService(self.db)

    def close(self) -> None:
        self.db.close()

    def purge_expired_leases(self) -> int:
        return self.locks.purge_expired()

    # Agents
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
        return self.events.register_agent(
            agent_id,
            machine_name=machine_name,
            role=role,
            branch=branch,
            status=status,
            capabilities=capabilities,
            ttl_sec=ttl_sec,
        )

    def heartbeat_agent(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        branch: str | None = None,
        ttl_sec: int | None = None,
    ) -> dict[str, Any]:
        return self.events.heartbeat_agent(
            agent_id, status=status, branch=branch, ttl_sec=ttl_sec
        )

    def list_agents(self, *, include_offline: bool = True) -> list[dict[str, Any]]:
        return self.events.list_agents(include_offline=include_offline)

    # Messages
    def send_message(
        self,
        sender_id: str,
        recipient_id: str,
        body: str,
        *,
        msg_type: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.events.send_message(
            sender_id, recipient_id, body, msg_type=msg_type, metadata=metadata
        )

    def poll_messages(
        self,
        agent_id: str,
        *,
        since_id: int | None = None,
        unread_only: bool = False,
        auto_ack: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.events.poll_messages(
            agent_id,
            since_id=since_id,
            unread_only=unread_only,
            auto_ack=auto_ack,
            limit=limit,
        )

    def ack_messages(self, agent_id: str, up_to_id: int) -> dict[str, Any]:
        return self.events.ack_messages(agent_id, up_to_id)

    # Tasks
    def create_task(
        self,
        title: str,
        created_by: str,
        *,
        description: str = "",
        scope_paths: list[str] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return self.events.create_task(
            title,
            created_by,
            description=description,
            scope_paths=scope_paths,
            task_id=task_id,
        )

    def list_tasks(
        self, *, status: str | None = None, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self.events.list_tasks(status=status, owner_id=owner_id)

    def claim_task(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return self.events.claim_task(task_id, agent_id)

    def update_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        status: str | None = None,
        result_summary: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.events.update_task(
            task_id,
            agent_id,
            status=status,
            result_summary=result_summary,
            force=force,
        )

    # Leases
    def acquire_lease(
        self,
        resource: str,
        owner_id: str,
        *,
        scope: str = "file",
        ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
        reason: str = "",
    ) -> dict[str, Any]:
        return self.locks.acquire_lease(
            resource, owner_id, scope=scope, ttl_sec=ttl_sec, reason=reason
        )

    def renew_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        ttl_sec: int = DEFAULT_LEASE_TTL_SEC,
    ) -> dict[str, Any]:
        return self.locks.renew_lease(
            owner_id,
            lease_id=lease_id,
            resource=resource,
            scope=scope,
            ttl_sec=ttl_sec,
        )

    def release_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        release_all: bool = False,
    ) -> dict[str, Any]:
        return self.locks.release_lease(
            owner_id,
            lease_id=lease_id,
            resource=resource,
            scope=scope,
            release_all=release_all,
        )

    def list_leases(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        return self.locks.list_leases(owner_id=owner_id)

    def check_paths(
        self,
        paths: list[str],
        *,
        agent_id: str | None = None,
        scope: str = "file",
    ) -> dict[str, Any]:
        return self.locks.check_paths(paths, agent_id=agent_id, scope=scope)

    # Context KV
    def put_context(
        self,
        key: str,
        value: Any,
        updated_by: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return self.events.put_context(
            key, value, updated_by, expected_version=expected_version
        )

    def get_context(self, key: str) -> dict[str, Any]:
        return self.events.get_context(key)

    def list_context(self, *, prefix: str | None = None) -> list[dict[str, Any]]:
        return self.events.list_context(prefix=prefix)

    # Snapshot & State Handoff
    def snapshot(self, *, agent_id: str | None = None) -> dict[str, Any]:
        return self.events.snapshot(agent_id=agent_id)

    def export_state(self) -> dict[str, Any]:
        return self.events.export_state()

    def import_state(
        self,
        bundle: dict[str, Any],
        *,
        replace: bool = True,
    ) -> dict[str, Any]:
        return self.events.import_state(bundle, replace=replace)
