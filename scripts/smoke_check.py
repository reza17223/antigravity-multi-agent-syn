"""Fresh-setup smoke verification for agsync (--spawn-local or --server URL)."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agsync.client import SyncClient, SyncClientError
from agsync.server import create_server


def run_smoke(server_url: str, *, token: str | None = None, expect_clean_leases: bool = False) -> None:
    run_tag = uuid.uuid4().hex[:6]
    agent_a = f"machine-a-{run_tag}"
    agent_b = f"machine-b-{run_tag}"

    client = SyncClient(server_url, token=token, timeout=5.0)

    # 1. Health
    health = client.health()
    assert health["status"] == "ok", f"Unexpected health: {health}"
    print(f"[1/7] /health OK (version={health['version']})")

    # 2. Register both agents
    client.register(agent_a, machine_name="host-a", role="backend", branch="agent-a/api")
    client.register(agent_b, machine_name="host-b", role="frontend", branch="agent-b/ui")
    snap = client.snapshot(agent_id=agent_b)
    assert agent_a in snap["online_agents"] and agent_b in snap["online_agents"]
    print(f"[2/7] Presence OK ({agent_a} & {agent_b} online)")

    # 3. Task splitting & conflict prevention
    t1 = client.create_task(
        "Implement auth endpoint",
        agent_a,
        scope_paths=["src/api/auth.py"],
        task_id=f"smoke-t1-{run_tag}",
    )
    t2 = client.create_task(
        "Build login form",
        agent_a,
        scope_paths=["src/ui/login.tsx"],
        task_id=f"smoke-t2-{run_tag}",
    )
    client.claim_task(t1["task_id"], agent_a)
    try:
        client.claim_task(t1["task_id"], agent_b)
        raise AssertionError("Expected 409 Conflict when machine-b claimed machine-a's task")
    except SyncClientError as exc:
        assert exc.status_code == 409, f"Expected 409, got {exc.status_code}"
    client.claim_task(t2["task_id"], agent_b)
    print("[3/7] Atomic task split & duplicate-claim rejection OK")

    # 4. Shared live context / API contract
    ctx_key = f"contract:auth_api:{run_tag}"
    client.put_context(
        ctx_key,
        {"endpoint": "/api/login", "method": "POST", "fields": ["email", "password"]},
        agent_a,
    )
    fetched = client.get_context(ctx_key)
    assert fetched["value"]["endpoint"] == "/api/login"
    assert fetched["version"] == 1
    print("[4/7] Shared live context contract OK")

    # 5. Live messaging & unread cursors
    client.send_message(agent_a, agent_b, "Auth contract published!", msg_type="contract")
    client.send_message(agent_a, "*", "Starting parallel work", msg_type="info")
    inbox = client.poll_messages(agent_b, unread_only=True, auto_ack=True)
    bodies = [m["body"] for m in inbox["messages"]]
    assert "Auth contract published!" in bodies and "Starting parallel work" in bodies
    inbox_empty = client.poll_messages(agent_b, unread_only=True)
    assert len(inbox_empty["messages"]) == 0
    print("[5/7] Live messaging & unread cursor ack OK")

    # 6. Lease locking + conflict rejection + parallel disjoint work
    api_path = f"smoke_{run_tag}/src/api/auth.py"
    api_dir = f"smoke_{run_tag}/src/api"
    ui_path = f"smoke_{run_tag}/src/ui/login.tsx"

    client.acquire_lease(api_path, agent_a, scope="file", reason="Implementing login route")
    try:
        client.acquire_lease(api_path.replace("/", "\\"), agent_b, scope="file")
        raise AssertionError("Expected 409 lease_conflict on overlapping file")
    except SyncClientError as exc:
        assert exc.status_code == 409 and exc.error_code == "lease_conflict"

    try:
        client.acquire_lease(api_dir, agent_b, scope="dir")
        raise AssertionError("Expected 409 lease_conflict on ancestor directory")
    except SyncClientError as exc:
        assert exc.status_code == 409 and exc.error_code == "lease_conflict"

    client.acquire_lease(ui_path, agent_b, scope="file", reason="Building login form")
    print("[6/7] Exclusive leases, hierarchical conflict rejection & parallel editing OK")

    # 7. Cleanup & handoff
    client.release_lease(agent_a, release_all=True)
    client.release_lease(agent_b, release_all=True)
    client.update_task(t1["task_id"], agent_a, status="done", result_summary="Auth route ready")
    client.update_task(t2["task_id"], agent_b, status="done", result_summary="Login UI ready")

    final_health = client.health()
    if expect_clean_leases:
        assert final_health["active_leases"] == 0, f"Expected 0 active leases: {final_health}"
    print("[7/7] Release, task completion & final /health OK")
    print("SMOKE CHECK PASSED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run end-to-end agsync smoke check.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--spawn-local",
        action="store_true",
        help="Start a temporary local server on 127.0.0.1:0 and run the smoke check.",
    )
    group.add_argument(
        "--server",
        help="URL of an already-running agsync server (e.g. http://192.168.1.10:8765).",
    )
    parser.add_argument("--token", default=os.environ.get("AGSYNC_TOKEN", ""))
    args = parser.parse_args(argv)

    token = args.token or None
    if args.spawn_local:
        with tempfile.TemporaryDirectory(prefix="agsync-smoke-") as tmp:
            db_path = Path(tmp) / "smoke.db"
            srv = create_server(host="127.0.0.1", port=0, db_path=db_path, token=token)
            port = srv.server_address[1]
            thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            try:
                run_smoke(f"http://127.0.0.1:{port}", token=token, expect_clean_leases=True)
            finally:
                srv.shutdown()
                srv.server_close()
                srv.store.close()
                thread.join(timeout=2.0)
        return 0

    run_smoke(args.server, token=token, expect_clean_leases=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
