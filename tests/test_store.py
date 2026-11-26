"""Unit tests for protocol normalization, overlap detection, and SQLite WAL SyncStore."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agsync.protocol import ProtocolError, normalize_resource, paths_overlap
from agsync.store import ConflictError, NotFoundError, SyncStore, ValidationError


class TestProtocol(unittest.TestCase):
    def test_normalize_strips_slashes_and_rejects_traversal(self) -> None:
        self.assertEqual(normalize_resource(r".\src\\api\auth.py"), "src/api/auth.py")
        self.assertEqual(normalize_resource("/src/api/"), "src/api")
        self.assertEqual(
            normalize_resource(r"Src\API\Auth.py", case_insensitive=True),
            "src/api/auth.py",
        )
        with self.assertRaises(ProtocolError):
            normalize_resource("../secret.py")
        with self.assertRaises(ProtocolError):
            normalize_resource("src/../../secret.py")
        with self.assertRaises(ProtocolError):
            normalize_resource(r"C:\repo\src\app.py")
        with self.assertRaises(ProtocolError):
            normalize_resource("c:relative.py")
        with self.assertRaises(ProtocolError):
            normalize_resource("C:")
        with self.assertRaises(ProtocolError):
            normalize_resource(r"\\server\share\file.py")
        with self.assertRaises(ProtocolError):
            normalize_resource("//server/share/file.py")
        with self.assertRaises(ProtocolError):
            normalize_resource(r"\/server/share/file.py")
        with self.assertRaises(ProtocolError):
            normalize_resource(".")

    def test_hierarchical_overlap_matrix(self) -> None:
        self.assertTrue(paths_overlap("src/api", "dir", "src/api/auth.py", "file"))
        self.assertTrue(paths_overlap("src/api/auth.py", "file", "src/api", "dir"))
        self.assertTrue(paths_overlap("src/api", "dir", "src/api/v1", "dir"))
        self.assertFalse(paths_overlap("src/api", "dir", "src/api_v2/auth.py", "file"))
        self.assertFalse(paths_overlap("src/a.py", "file", "src/b.py", "file"))
        # Symbol vs symbol & symbol vs file/dir
        self.assertFalse(
            paths_overlap("src/auth.py::login", "symbol", "src/auth.py::logout", "symbol")
        )
        self.assertTrue(
            paths_overlap("src/auth.py::login", "symbol", "src/auth.py", "file")
        )
        self.assertTrue(
            paths_overlap("src", "dir", "src/auth.py::login", "symbol")
        )


class TestSyncStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_700_000_000.0
        self.store = SyncStore(
            Path(self.tmp.name) / "state.db",
            case_insensitive=True,
            time_fn=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_agent_registration_and_ttl_expiry(self) -> None:
        self.store.register_agent("machine-a", machine_name="pc-a", branch="agent-a/api", ttl_sec=30)
        self.store.register_agent("machine-b", machine_name="pc-b", branch="agent-b/ui", ttl_sec=120)
        online = self.store.list_agents(include_offline=False)
        self.assertEqual({a["agent_id"] for a in online}, {"machine-a", "machine-b"})

        self.now += 45.0
        online_after = self.store.list_agents(include_offline=False)
        self.assertEqual([a["agent_id"] for a in online_after], ["machine-b"])

        self.store.heartbeat_agent("machine-a", status="busy")
        online_restored = self.store.list_agents(include_offline=False)
        self.assertEqual({a["agent_id"] for a in online_restored}, {"machine-a", "machine-b"})

    def test_messages_direct_broadcast_and_cursors(self) -> None:
        self.store.send_message("machine-a", "machine-b", "hello b", msg_type="info")
        self.store.send_message("machine-a", "*", "all hands", msg_type="alert")
        self.store.send_message("machine-b", "machine-a", "ack a", msg_type="info")

        b_unread = self.store.poll_messages("machine-b", unread_only=True, auto_ack=True)
        self.assertEqual([m["body"] for m in b_unread["messages"]], ["hello b", "all hands"])

        b_unread_again = self.store.poll_messages("machine-b", unread_only=True)
        self.assertEqual(b_unread_again["messages"], [])

        # machine-a unread should not include its own broadcast, only direct from b
        a_unread = self.store.poll_messages("machine-a", unread_only=True)
        self.assertEqual([m["body"] for m in a_unread["messages"]], ["ack a"])

        # history without unread_only still includes broadcasts
        a_hist = self.store.poll_messages("machine-a", unread_only=False)
        self.assertEqual(len(a_hist["messages"]), 2)

    def test_leases_hierarchical_case_insensitive_renewal_and_ttl(self) -> None:
        res = self.store.acquire_lease("src/api", "machine-a", scope="dir", ttl_sec=60, reason="refactor")
        self.assertFalse(res["renewed"])
        token1 = res["lease"]["fencing_token"]

        # Hierarchical & Windows case-insensitive conflict
        with self.assertRaises(ConflictError) as ctx:
            self.store.acquire_lease(r"SRC\API\Auth.py", "machine-b", scope="file")
        self.assertEqual(ctx.exception.error_code, "lease_conflict")

        # Same owner idempotent renewal
        renewed = self.store.acquire_lease("SRC/API", "machine-a", scope="dir", ttl_sec=120)
        self.assertTrue(renewed["renewed"])
        self.assertEqual(renewed["lease"]["fencing_token"], token1)

        # Peer cannot release A's lease
        with self.assertRaises(ConflictError):
            self.store.release_lease("machine-b", resource="src/api", scope="dir")

        # TTL expiry allows peer to acquire
        self.now += 121.0
        b_res = self.store.acquire_lease("src/api/auth.py", "machine-b", scope="file")
        self.assertGreater(b_res["lease"]["fencing_token"], token1)

        # Check paths distinguishes owned_by_self vs blocked vs free
        chk = self.store.check_paths(
            ["src/api/auth.py", "src/api", "src/ui/app.py"],
            agent_id="machine-a",
        )
        self.assertFalse(chk["all_clear"])
        self.assertEqual(len(chk["blocked"]), 1)
        self.assertIn("src/ui/app.py", chk["free"])

    def test_tasks_lifecycle_and_context_cas(self) -> None:
        t = self.store.create_task("Build parser", "machine-a", scope_paths=[r"agsync\protocol.py"])
        claimed = self.store.claim_task(t["task_id"], "machine-a")
        self.assertEqual(claimed["status"], "claimed")
        with self.assertRaises(ConflictError):
            self.store.claim_task(t["task_id"], "machine-b")
        with self.assertRaises(ConflictError):
            self.store.update_task(t["task_id"], "machine-b", status="done")

        done = self.store.update_task(t["task_id"], "machine-a", status="done", result_summary="merged")
        self.assertEqual(done["status"], "done")

        # Context optimistic concurrency
        v1 = self.store.put_context("contract:db", {"port": 5432}, "machine-a", expected_version=0)
        self.assertEqual(v1["version"], 1)
        with self.assertRaises(ConflictError):
            self.store.put_context("contract:db", {"port": 5433}, "machine-b", expected_version=0)
        v2 = self.store.put_context("contract:db", {"port": 5433}, "machine-b", expected_version=1)
        self.assertEqual(v2["version"], 2)
        with self.assertRaises(NotFoundError):
            self.store.get_context("missing:key")
        with self.assertRaises(ValidationError):
            self.store.register_agent("*")
        with self.assertRaises(ValidationError):
            self.store.create_task("Bad whitespace ID", "machine-a", task_id="   ")
        with self.assertRaises(ValidationError):
            self.store.create_task("Bad empty ID", "machine-a", task_id="")

    def test_list_context_escapes_like_wildcards(self) -> None:
        self.store.put_context("a_1", 1, "machine-a")
        self.store.put_context("ab1", 2, "machine-a")
        self.store.put_context("100%real", 3, "machine-a")
        self.store.put_context("1000", 4, "machine-a")
        self.store.put_context(r"ns\key", 5, "machine-a")
        self.store.put_context("nsxkey", 6, "machine-a")

        self.assertEqual(
            [e["key"] for e in self.store.list_context(prefix="a_")],
            ["a_1"],
        )
        self.assertEqual(
            [e["key"] for e in self.store.list_context(prefix="100%")],
            ["100%real"],
        )
        self.assertEqual(
            [e["key"] for e in self.store.list_context(prefix="ns\\")],
            [r"ns\key"],
        )


if __name__ == "__main__":
    unittest.main()
