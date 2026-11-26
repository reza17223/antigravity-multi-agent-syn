"""Tests for multi-peer handoff, web dashboard, MCP/repo config, and LAN discovery."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agsync.client import SyncClient, SyncClientError
from agsync.config import configure_antigravity_mcp, copy_project_templates
from agsync.discovery import DiscoveryBeacon, discover_hubs
from agsync.server import create_server


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestExtensions(unittest.TestCase):
    def test_three_agents_and_host_state_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            srv1 = create_server(host="127.0.0.1", port=0, db_path=tmp_path / "host1.db")
            srv2 = create_server(host="127.0.0.1", port=0, db_path=tmp_path / "host2.db")
            t1 = threading.Thread(target=srv1.serve_forever, daemon=True)
            t2 = threading.Thread(target=srv2.serve_forever, daemon=True)
            t1.start()
            t2.start()
            try:
                url1 = f"http://127.0.0.1:{srv1.server_address[1]}"
                url2 = f"http://127.0.0.1:{srv2.server_address[1]}"
                c1 = SyncClient(url1)
                c2 = SyncClient(url2)

                # Three distinct machines collaborate on host 1
                for aid in ("machine-a", "machine-b", "machine-c"):
                    c1.register(aid, machine_name=f"pc-{aid}", branch=f"feat/{aid}")
                c1.create_task("Implement auth", "machine-a", scope_paths=["auth.py"], task_id="t1")
                c1.claim_task("t1", "machine-b")
                c1.acquire_lease("auth.py", "machine-b", reason="editing auth")
                c1.send_message("machine-a", "machine-c", "Please review tests")
                c1.put_context("api_port", 9000, "machine-a")

                # Export from old host and import into new host (simulating host switch)
                bundle = c1.export_state()
                res = c2.import_state(bundle, replace=True)
                self.assertTrue(res["imported"])
                self.assertEqual(res["counts"]["agents"], 3)
                self.assertEqual(res["counts"]["leases"], 1)

                snap2 = c2.snapshot(agent_id="machine-c")
                self.assertEqual(len(snap2["online_agents"]), 3)
                self.assertEqual(snap2["active_leases"][0]["owner_id"], "machine-b")
                self.assertEqual(snap2["tasks"][0]["status"], "claimed")
                self.assertEqual(c2.get_context("api_port")["value"], 9000)

                # Leases remain enforced on the new host against a 3rd peer
                with self.assertRaises(SyncClientError) as ctx:
                    c2.acquire_lease("auth.py", "machine-c")
                self.assertEqual(ctx.exception.status_code, 409)
            finally:
                srv1.shutdown()
                srv1.server_close()
                srv1.store.close()
                srv2.shutdown()
                srv2.server_close()
                srv2.store.close()

    def test_dashboard_served_and_auth_preserved_on_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            srv = create_server(
                host="127.0.0.1",
                port=0,
                db_path=Path(tmp) / "dash.db",
                token="secret-token",
            )
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                with urllib.request.urlopen(f"{base}/dashboard", timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    html = resp.read().decode("utf-8")
                    self.assertIn("agsync Coordination Hub", html)
                    self.assertIn("/snapshot", html)

                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(f"{base}/state/export", timeout=3)
                self.assertEqual(err.exception.code, 401)
            finally:
                srv.shutdown()
                srv.server_close()
                srv.store.close()

    def test_configure_antigravity_mcp_and_attach_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mcp_path = tmp_path / "mcp_config.json"
            mcp_path.write_text(
                json.dumps({"mcpServers": {"existing-tool": {"url": "http://localhost:9999"}}}),
                encoding="utf-8",
            )

            res = configure_antigravity_mcp(
                server_url="http://192.168.1.182:8765/",
                agent_id="machine-c",
                mode="http",
                token="tok-123",
                mcp_config_path=mcp_path,
            )
            self.assertTrue(Path(res["backup_path"]).is_file())
            updated = json.loads(mcp_path.read_text(encoding="utf-8"))
            self.assertIn("existing-tool", updated["mcpServers"])
            self.assertEqual(
                updated["mcpServers"]["agsync"]["url"],
                "http://192.168.1.182:8765/mcp?agent=machine-c",
            )
            self.assertEqual(
                updated["mcpServers"]["agsync"]["headers"]["Authorization"],
                "Bearer tok-123",
            )

            # Verify repo template + hook attachment
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            copied = copy_project_templates(repo)
            self.assertTrue((repo / "AGENTS.md").is_file())
            self.assertTrue((repo / ".agents" / "skills" / "sync-start" / "SKILL.md").is_file())
            self.assertGreaterEqual(len(copied), 4)

    def test_discovery_beacon_roundtrip(self) -> None:
        port = _free_udp_port()
        beacon = DiscoveryBeacon(
            http_port=8765,
            host_agent="machine-a",
            discovery_port=port,
            interval_sec=0.2,
            bind_ip="127.0.0.1",
        )
        beacon.start()
        try:
            hubs = discover_hubs(timeout_sec=1.5, discovery_port=port)
            self.assertTrue(len(hubs) >= 1)
            self.assertEqual(hubs[0]["url"], "http://127.0.0.1:8765")
            self.assertEqual(hubs[0]["host_agent"], "machine-a")
        finally:
            beacon.stop()


if __name__ == "__main__":
    unittest.main()
