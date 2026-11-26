"""Zero-dependency HTTP JSON client for the agsync coordination server."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agsync.adapters.network import build_opener_for_url


class SyncClientError(Exception):
    """Raised when an HTTP request fails or the server returns an error status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
        unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}
        self.unreachable = unreachable

    @property
    def error_code(self) -> str:
        return str(self.payload.get("error", "client_error"))


class SyncClient:
    """Thin urllib client wrapping all agsync server REST endpoints with LAN proxy bypass."""

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8765",
        *,
        token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token if (token and token.strip()) else None
        self.timeout = float(timeout)
        self._opener = build_opener_for_url(self.server_url)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.server_url}{path}"
        if query:
            clean_q = {k: v for k, v in query.items() if v is not None}
            if clean_q:
                url = f"{url}?{urllib.parse.urlencode(clean_q)}"

        data: bytes | None = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw_err = exc.read().decode("utf-8", errors="replace")
            try:
                err_payload = json.loads(raw_err) if raw_err else {}
            except json.JSONDecodeError:
                err_payload = {"error": f"http_{exc.code}", "message": raw_err}
            msg = err_payload.get("message") or f"HTTP {exc.code}"
            raise SyncClientError(
                msg, status_code=exc.code, payload=err_payload, unreachable=False
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncClientError(
                f"Cannot reach agsync server at {self.server_url}: {exc}",
                status_code=None,
                payload={"error": "server_unreachable", "message": str(exc)},
                unreachable=True,
            ) from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def snapshot(self, *, agent_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/snapshot", query={"agent_id": agent_id})

    def register(
        self,
        agent_id: str,
        *,
        machine_name: str = "",
        role: str = "agent",
        branch: str = "",
        status: str = "online",
        capabilities: list[str] | None = None,
        ttl_sec: int = 120,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/agents/register",
            body={
                "agent_id": agent_id,
                "machine_name": machine_name,
                "role": role,
                "branch": branch,
                "status": status,
                "capabilities": capabilities or [],
                "ttl_sec": ttl_sec,
            },
        )

    def heartbeat(
        self,
        agent_id: str,
        *,
        status: str | None = None,
        branch: str | None = None,
        ttl_sec: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if status is not None:
            body["status"] = status
        if branch is not None:
            body["branch"] = branch
        if ttl_sec is not None:
            body["ttl_sec"] = ttl_sec
        return self._request(
            "POST",
            f"/agents/{urllib.parse.quote(agent_id, safe='')}/heartbeat",
            body=body,
        )

    def list_agents(self, *, include_offline: bool = True) -> dict[str, Any]:
        return self._request(
            "GET",
            "/agents",
            query={"include_offline": "1" if include_offline else "0"},
        )

    def send_message(
        self,
        sender_id: str,
        recipient_id: str,
        body: str,
        *,
        msg_type: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/messages",
            body={
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "body": body,
                "msg_type": msg_type,
                "metadata": metadata or {},
            },
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
        res = self._request(
            "GET",
            "/messages",
            query={
                "agent_id": agent_id,
                "since_id": since_id,
                "unread": "1" if unread_only else "0",
                "limit": limit,
            },
        )
        if auto_ack and res.get("messages"):
            max_id = max(int(m["id"]) for m in res["messages"])
            ack_res = self.ack_messages(agent_id, max_id)
            res["cursor"] = ack_res.get("last_read_msg_id", max_id)
        return res

    def ack_messages(self, agent_id: str, up_to_id: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/messages/ack",
            body={"agent_id": agent_id, "up_to_id": up_to_id},
        )

    def create_task(
        self,
        title: str,
        created_by: str,
        *,
        description: str = "",
        scope_paths: list[str] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "created_by": created_by,
            "description": description,
            "scope_paths": scope_paths or [],
        }
        if task_id:
            payload["task_id"] = task_id
        return self._request("POST", "/tasks", body=payload)

    def list_tasks(
        self, *, status: str | None = None, owner_id: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET", "/tasks", query={"status": status, "owner_id": owner_id}
        )

    def claim_task(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/tasks/{urllib.parse.quote(task_id, safe='')}/claim",
            body={"agent_id": agent_id},
        )

    def update_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        status: str | None = None,
        result_summary: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"agent_id": agent_id, "force": force}
        if status is not None:
            payload["status"] = status
        if result_summary is not None:
            payload["result_summary"] = result_summary
        return self._request(
            "PATCH",
            f"/tasks/{urllib.parse.quote(task_id, safe='')}",
            body=payload,
        )

    def acquire_lease(
        self,
        resource: str,
        owner_id: str,
        *,
        scope: str = "file",
        ttl_sec: int = 600,
        reason: str = "",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/leases/acquire",
            body={
                "resource": resource,
                "owner_id": owner_id,
                "scope": scope,
                "ttl_sec": ttl_sec,
                "reason": reason,
            },
        )

    def renew_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        ttl_sec: int = 600,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "owner_id": owner_id,
            "scope": scope,
            "ttl_sec": ttl_sec,
        }
        if lease_id:
            payload["lease_id"] = lease_id
        if resource:
            payload["resource"] = resource
        return self._request("POST", "/leases/renew", body=payload)

    def release_lease(
        self,
        owner_id: str,
        *,
        lease_id: str | None = None,
        resource: str | None = None,
        scope: str = "file",
        release_all: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "owner_id": owner_id,
            "scope": scope,
            "release_all": release_all,
        }
        if lease_id:
            payload["lease_id"] = lease_id
        if resource:
            payload["resource"] = resource
        return self._request("POST", "/leases/release", body=payload)

    def check_paths(
        self,
        paths: list[str],
        *,
        agent_id: str | None = None,
        scope: str = "file",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/leases/check",
            body={"paths": paths, "agent_id": agent_id, "scope": scope},
        )

    def list_leases(self, *, owner_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/leases", query={"owner_id": owner_id})

    def put_context(
        self,
        key: str,
        value: Any,
        updated_by: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"value": value, "updated_by": updated_by}
        if expected_version is not None:
            payload["expected_version"] = expected_version
        return self._request(
            "PUT",
            f"/context/{urllib.parse.quote(key, safe='')}",
            body=payload,
        )

    def get_context(self, key: str) -> dict[str, Any]:
        return self._request("GET", f"/context/{urllib.parse.quote(key, safe='')}")

    def list_context(self, *, prefix: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/context", query={"prefix": prefix})

    def export_state(self) -> dict[str, Any]:
        return self._request("GET", "/state/export")

    def import_state(
        self, bundle: dict[str, Any], *, replace: bool = True
    ) -> dict[str, Any]:
        payload = dict(bundle)
        payload["replace"] = replace
        return self._request("POST", "/state/import", body=payload)
