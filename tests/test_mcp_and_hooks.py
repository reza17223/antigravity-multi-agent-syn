"""Tests for Dual Stdio MCP bridge subprocesses, Streamable HTTP /mcp, and PreToolUse hook_guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agsync.client import SyncClient
from agsync.protocol import MCP_TOOLS
from agsync.server import create_server

REPO_ROOT = Path(__file__).resolve().parent.parent


def _rpc_send(proc: subprocess.Popen[str], msg: dict) -> dict | None:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if "id" not in msg or msg.get("method", "").startswith("notifications/"):
        return None
    line = proc.stdout.readline()
    if not line:
        stderr_out = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(f"EOF from mcp_bridge stdout. stderr={stderr_out!r}")
    return json.loads(line)


def _http_mcp(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict | None]:
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=hdrs,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, (json.loads(raw) if raw else None)


class TestMCPAndHooks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = create_server(
            host="127.0.0.1",
            port=0,
            db_path=Path(self.tmp.name) / "mcp.db",
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

    def test_dual_stdio_mcp_subprocesses_end_to_end(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)

        proc_a = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agsync.mcp_bridge",
                "--server",
                self.base_url,
                "--agent",
                "machine-a",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(REPO_ROOT),
            env=env,
        )
        proc_b = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agsync.mcp_bridge",
                "--server",
                self.base_url,
                "--agent",
                "machine-b",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(REPO_ROOT),
            env=env,
        )
        try:
            for p in (proc_a, proc_b):
                init_resp = _rpc_send(
                    p,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                )
                self.assertEqual(init_resp["result"]["serverInfo"]["name"], "agsync")
                _rpc_send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                ping_resp = _rpc_send(p, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
                self.assertEqual(ping_resp["result"], {})

            tools_resp = _rpc_send(proc_a, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
            self.assertEqual(len(tool_names), len(MCP_TOOLS))
            self.assertIn("sync_acquire_lease", tool_names)

            # Register both agents
            reg_a = _rpc_send(
                proc_a,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_register",
                        "arguments": {"role": "backend", "branch": "agent-a/api"},
                    },
                },
            )
            self.assertFalse(reg_a["result"]["isError"])
            reg_b = _rpc_send(
                proc_b,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_register",
                        "arguments": {"role": "frontend", "branch": "agent-b/ui"},
                    },
                },
            )
            self.assertFalse(reg_b["result"]["isError"])

            # Task create & atomic claim
            t_resp = _rpc_send(
                proc_a,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_create_task",
                        "arguments": {
                            "title": "Build auth API",
                            "scope_paths": ["src/api/auth.py"],
                        },
                    },
                },
            )
            tid = t_resp["result"]["structuredContent"]["task_id"]
            c_a = _rpc_send(
                proc_a,
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "sync_claim_task", "arguments": {"task_id": tid}},
                },
            )
            self.assertFalse(c_a["result"]["isError"])
            c_b = _rpc_send(
                proc_b,
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "sync_claim_task", "arguments": {"task_id": tid}},
                },
            )
            self.assertTrue(c_b["result"]["isError"])
            self.assertEqual(c_b["result"]["structuredContent"]["error"], "task_already_claimed")

            # Lease acquire conflict across stdio bridges
            l_a = _rpc_send(
                proc_a,
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_acquire_lease",
                        "arguments": {"resource": "src/api", "scope": "dir"},
                    },
                },
            )
            self.assertFalse(l_a["result"]["isError"])
            l_b = _rpc_send(
                proc_b,
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_acquire_lease",
                        "arguments": {"resource": r"src\api\auth.py", "scope": "file"},
                    },
                },
            )
            self.assertTrue(l_b["result"]["isError"])
            self.assertEqual(l_b["result"]["structuredContent"]["error"], "lease_conflict")

            # Messaging A -> B
            _rpc_send(
                proc_a,
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_send_message",
                        "arguments": {"recipient_id": "machine-b", "body": "Ready for UI"},
                    },
                },
            )
            poll_b = _rpc_send(
                proc_b,
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_poll_messages",
                        "arguments": {"unread_only": True, "auto_ack": True},
                    },
                },
            )
            msgs = poll_b["result"]["structuredContent"]["messages"]
            self.assertEqual([m["body"] for m in msgs], ["Ready for UI"])

            # Release lease
            rel_a = _rpc_send(
                proc_a,
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "sync_release_lease",
                        "arguments": {"release_all": True},
                    },
                },
            )
            self.assertEqual(rel_a["result"]["structuredContent"]["released_count"], 1)
        finally:
            for p in (proc_a, proc_b):
                if p.stdin:
                    p.stdin.close()
                p.wait(timeout=5.0)
                if p.stdout:
                    p.stdout.close()
                if p.stderr:
                    p.stderr.close()

    def test_streamable_http_mcp_endpoint(self) -> None:
        mcp_url = f"{self.base_url}/mcp?agent=http-agent-a"
        status, init_resp = _http_mcp(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "agsync")

        status_notif, notif_resp = _http_mcp(
            mcp_url, {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(status_notif, 202)
        self.assertIsNone(notif_resp)

        _, tools_resp = _http_mcp(mcp_url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(tools_resp["result"]["tools"]), len(MCP_TOOLS))

        _, acq_resp = _http_mcp(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sync_acquire_lease",
                    "arguments": {"resource": "docs/api.md", "scope": "file"},
                },
            },
        )
        self.assertFalse(acq_resp["result"]["isError"])
        self.assertEqual(
            acq_resp["result"]["structuredContent"]["lease"]["owner_id"],
            "http-agent-a",
        )

        # Peer via X-Agent-Id header hits conflict
        _, conflict_resp = _http_mcp(
            f"{self.base_url}/mcp",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "sync_acquire_lease",
                    "arguments": {"resource": "docs/api.md", "scope": "file"},
                },
            },
            headers={"X-Agent-Id": "http-agent-b"},
        )
        self.assertTrue(conflict_resp["result"]["isError"])
        self.assertEqual(
            conflict_resp["result"]["structuredContent"]["error"], "lease_conflict"
        )

        # GET /mcp must return 405 per Streamable HTTP when no SSE stream offered
        with self.assertRaises(urllib.error.HTTPError) as http_err:
            urllib.request.urlopen(f"{self.base_url}/mcp", timeout=5.0)
        self.assertEqual(http_err.exception.code, 405)

    def test_pre_tool_use_hook_guard_subprocess(self) -> None:
        client = SyncClient(self.base_url)
        client.acquire_lease("src/locked.py", "machine-b", scope="file", reason="editing")
        client.acquire_lease("src/mine.py", "machine-a", scope="file", reason="my file")

        repo_dir = Path(self.tmp.name) / "fake_repo"
        (repo_dir / ".git").mkdir(parents=True)
        (repo_dir / "src").mkdir()

        def run_hook(payload: dict, extra_args: list[str] | None = None) -> dict:
            cmd = [
                sys.executable,
                str(REPO_ROOT / "agsync" / "hook_guard.py"),
                "--server",
                self.base_url,
                "--agent",
                "machine-a",
                *(extra_args or []),
            ]
            # Simulate Antigravity CWD = .agents/ folder while stdin cwd = repo_dir
            agents_dir = repo_dir / ".agents"
            agents_dir.mkdir(exist_ok=True)
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                cwd=str(agents_dir),
                check=True,
            )
            return json.loads(proc.stdout)

        # 1. Read-only tool -> allow
        res_read = run_hook(
            {
                "toolCall": {"name": "read_file", "args": {"Path": str(repo_dir / "src" / "locked.py")}},
                "cwd": str(repo_dir),
            }
        )
        self.assertEqual(res_read["decision"], "allow")

        # 2. edit_file on peer-locked file (including UTF-8 BOM stdin) -> deny
        res_deny = run_hook(
            {
                "toolCall": {"name": "edit_file", "args": {"Path": str(repo_dir / "src" / "locked.py")}},
                "cwd": str(repo_dir),
            }
        )
        self.assertEqual(res_deny["decision"], "deny")
        self.assertIn("machine-b", res_deny["reason"])

        bom_proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "agsync" / "hook_guard.py"),
                "--server",
                self.base_url,
                "--agent",
                "machine-a",
            ],
            input="\ufeff" + json.dumps(
                {
                    "toolCall": {"name": "edit_file", "args": {"Path": "src/locked.py"}},
                    "cwd": str(repo_dir),
                }
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(bom_proc.stdout)["decision"], "deny")

        # 3. replace_file_content on own-leased file -> allow
        res_own = run_hook(
            {
                "toolCall": {
                    "name": "replace_file_content",
                    "args": {"TargetFile": "src/mine.py"},
                },
                "cwd": str(repo_dir),
            }
        )
        self.assertEqual(res_own["decision"], "allow")

        # 4. --require-lease on unleased file -> deny
        res_req = run_hook(
            {
                "toolCall": {"name": "write_file", "args": {"Path": "src/unleased.py"}},
                "cwd": str(repo_dir),
            },
            extra_args=["--require-lease"],
        )
        self.assertEqual(res_req["decision"], "deny")

        # 5. External path outside repo -> allow
        res_ext = run_hook(
            {
                "toolCall": {"name": "write_file", "args": {"Path": str(Path(self.tmp.name) / "outside.txt")}},
                "cwd": str(repo_dir),
            }
        )
        self.assertEqual(res_ext["decision"], "allow")

        # 6. Unreachable server: fail-open vs fail-closed
        dead_cmd = [
            sys.executable,
            str(REPO_ROOT / "agsync" / "hook_guard.py"),
            "--server",
            "http://127.0.0.1:1",
            "--agent",
            "machine-a",
        ]
        payload = {
            "toolCall": {"name": "edit_file", "args": {"Path": "src/locked.py"}},
            "cwd": str(repo_dir),
        }
        p_open = subprocess.run(
            dead_cmd, input=json.dumps(payload), text=True, capture_output=True, check=True
        )
        self.assertEqual(json.loads(p_open.stdout)["decision"], "allow")

        p_closed = subprocess.run(
            [*dead_cmd, "--fail-closed"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(p_closed.stdout)["decision"], "deny")

        # 7. cwd outside repo + TargetFile/Path inside peer-locked repo -> deny
        outside_cwd = Path(self.tmp.name) / "unrelated_outside_dir"
        outside_cwd.mkdir(exist_ok=True)
        res_outside_cwd_ws = run_hook(
            {
                "toolCall": {
                    "name": "edit_file",
                    "args": {"Path": str(repo_dir / "src" / "locked.py")},
                },
                "cwd": str(outside_cwd),
                "workspacePaths": [str(repo_dir)],
            }
        )
        self.assertEqual(res_outside_cwd_ws["decision"], "deny")
        self.assertIn("machine-b", res_outside_cwd_ws["reason"])

        res_outside_cwd_no_ws = run_hook(
            {
                "toolCall": {
                    "name": "replace_file_content",
                    "args": {"TargetFile": str(repo_dir / "src" / "locked.py")},
                },
                "cwd": str(outside_cwd),
            }
        )
        self.assertEqual(res_outside_cwd_no_ws["decision"], "deny")
        self.assertIn("machine-b", res_outside_cwd_no_ws["reason"])

    def test_put_patch_validation_errors_return_400(self) -> None:
        client = SyncClient(self.base_url)
        task = client.create_task("Test PATCH validation", "machine-a")

        def raw_request(method: str, path: str, body_obj: object) -> tuple[int, dict]:
            req = urllib.request.Request(
                f"{self.base_url}{path}",
                data=json.dumps(body_obj).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        st1, p1 = raw_request(
            "PUT",
            "/context/k1",
            {"value": 1, "updated_by": "machine-a", "expected_version": "not-an-int"},
        )
        self.assertEqual(st1, 400)
        self.assertEqual(p1["error"], "validation_error")

        st2, p2 = raw_request(
            "PUT",
            "/context/k1",
            {"value": 1, "updated_by": "machine-a", "expected_version": [1]},
        )
        self.assertEqual(st2, 400)
        self.assertEqual(p2["error"], "validation_error")

        st3, p3 = raw_request(
            "PATCH",
            f"/tasks/{task['task_id']}",
            {"agent_id": "machine-a", "status": 123},
        )
        self.assertEqual(st3, 400)
        self.assertEqual(p3["error"], "validation_error")

        st4, p4 = raw_request(
            "PATCH",
            f"/tasks/{task['task_id']}",
            {"agent_id": None, "status": "done"},
        )
        self.assertEqual(st4, 400)
        self.assertEqual(p4["error"], "validation_error")

        st5, p5 = raw_request("PATCH", f"/tasks/{task['task_id']}", ["not", "an", "object"])
        self.assertEqual(st5, 400)
        self.assertEqual(p5["error"], "validation_error")

    def test_mcp_malformed_params_and_bom_http_and_stdio(self) -> None:
        # 1. Streamable HTTP /mcp with UTF-8 BOM
        bom_req = urllib.request.Request(
            f"{self.base_url}/mcp?agent=bom-agent",
            data=b"\xef\xbb\xbf" + json.dumps({"jsonrpc": "2.0", "id": 100, "method": "ping"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(bom_req, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read().decode("utf-8"))["result"], {})

        # 2. Streamable HTTP /mcp malformed params, arguments, and tool arg types
        http_cases = [
            ({"jsonrpc": "2.0", "id": 101, "method": "tools/list", "params": None}, "result"),
            ({"jsonrpc": "2.0", "id": 102, "method": "tools/call", "params": [1, 2]}, -32602),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 103,
                    "method": "tools/call",
                    "params": {"name": "sync_status", "arguments": []},
                },
                -32602,
            ),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 104,
                    "method": "tools/call",
                    "params": {"name": "sync_register", "arguments": {"ttl_sec": "abc"}},
                },
                "isError",
            ),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 105,
                    "method": "tools/call",
                    "params": {"name": "sync_register", "arguments": {"ttl_sec": None}},
                },
                "isError",
            ),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 106,
                    "method": "tools/call",
                    "params": {"name": "sync_check_paths", "arguments": {"paths": 123}},
                },
                "isError",
            ),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 107,
                    "method": "tools/call",
                    "params": {"name": "sync_poll_messages", "arguments": {"limit": "bad"}},
                },
                "isError",
            ),
        ]
        for payload, expectation in http_cases:
            st, body = _http_mcp(f"{self.base_url}/mcp?agent=http-bad", payload)
            self.assertEqual(st, 200, f"payload={payload}")
            assert body is not None
            self.assertEqual(body["id"], payload["id"])
            if expectation == "result":
                self.assertIn("result", body)
            elif expectation == "isError":
                self.assertTrue(body["result"]["isError"], f"Expected isError for {payload}: {body}")
            else:
                self.assertEqual(body["error"]["code"], expectation, f"payload={payload}, body={body}")

        # 3. Stdio mcp_bridge with identical malformed payloads — must NOT crash (rc=0)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agsync.mcp_bridge",
                "--server",
                self.base_url,
                "--agent",
                "stdio-bad",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(REPO_ROOT),
            env=env,
        )
        try:
            for payload, expectation in http_cases:
                resp = _rpc_send(proc, payload)
                assert resp is not None
                self.assertEqual(resp["id"], payload["id"])
                if expectation == "result":
                    self.assertIn("result", resp)
                elif expectation == "isError":
                    self.assertTrue(resp["result"]["isError"], f"Expected isError for {payload}: {resp}")
                else:
                    self.assertEqual(resp["error"]["code"], expectation)
            # Confirm bridge is still alive after malformed requests
            ping_resp = _rpc_send(proc, {"jsonrpc": "2.0", "id": 999, "method": "ping"})
            assert ping_resp is not None
            self.assertEqual(ping_resp["result"], {})
        finally:
            if proc.stdin:
                proc.stdin.close()
            rc = proc.wait(timeout=5.0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
