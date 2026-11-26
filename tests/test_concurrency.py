"""Concurrency & race-condition tests over live HTTP server using threading.Barrier."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from agsync.client import SyncClient, SyncClientError
from agsync.server import create_server


class TestConcurrencyHTTP(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = create_server(
            host="127.0.0.1",
            port=0,
            db_path=Path(self.tmp.name) / "race.db",
            token="secret-test-token",
            case_insensitive=True,
        )
        self.port = self.srv.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.srv.store.close()
        self.thread.join(timeout=2.0)
        self.tmp.cleanup()

    def test_bearer_auth_enforced_except_health(self) -> None:
        no_auth = SyncClient(self.base_url)
        self.assertEqual(no_auth.health()["status"], "ok")
        with self.assertRaises(SyncClientError) as ctx:
            no_auth.snapshot()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_concurrent_task_claim_barrier(self) -> None:
        admin = SyncClient(self.base_url, token="secret-test-token")
        task = admin.create_task("Shared critical migration", "machine-a")

        barrier = threading.Barrier(2)
        results: list[tuple[str, int, str]] = []
        lock = threading.Lock()

        def worker(agent_id: str) -> None:
            c = SyncClient(self.base_url, token="secret-test-token")
            barrier.wait(timeout=5.0)
            try:
                res = c.claim_task(task["task_id"], agent_id)
                with lock:
                    results.append((agent_id, 200, res["owner_id"]))
            except SyncClientError as exc:
                with lock:
                    results.append((agent_id, exc.status_code or 0, exc.error_code))

        t1 = threading.Thread(target=worker, args=("machine-a",))
        t2 = threading.Thread(target=worker, args=("machine-b",))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        statuses = sorted(r[1] for r in results)
        self.assertEqual(statuses, [200, 409], f"Unexpected race results: {results}")

    def test_concurrent_lease_acquisition_barrier(self) -> None:
        barrier = threading.Barrier(2)
        results: list[tuple[str, int, str]] = []
        lock = threading.Lock()

        def worker(agent_id: str, path: str, scope: str) -> None:
            c = SyncClient(self.base_url, token="secret-test-token")
            barrier.wait(timeout=5.0)
            try:
                res = c.acquire_lease(path, agent_id, scope=scope)
                with lock:
                    results.append((agent_id, 200, res["lease"]["owner_id"]))
            except SyncClientError as exc:
                with lock:
                    results.append((agent_id, exc.status_code or 0, exc.error_code))

        # One agent locks directory, other concurrently locks file inside it with mixed case/slashes
        t1 = threading.Thread(target=worker, args=("machine-a", "src/api", "dir"))
        t2 = threading.Thread(target=worker, args=("machine-b", r"SRC\API\auth.py", "file"))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        statuses = sorted(r[1] for r in results)
        self.assertEqual(statuses, [200, 409], f"Unexpected lease race results: {results}")


if __name__ == "__main__":
    unittest.main()
