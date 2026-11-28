"""Tier 4 Opaque-Box Real-World Multi-Agent Workload E2E Scenarios.

Covers all 8 scenarios in TEST_INFRA.md § 4 plus Scenario 9 (hybrid stdio MCP + REST +
--chain-timeout tree-kill + templates/bilingual docs).
Zero third-party dependencies (Python 3.12 stdlib only).
Zero top-level imports of unimplemented Phase 2 symbols (`agsync.*` / `scripts.*`).
Zero mocks (`unittest.mock`) and zero `skipTest` calls.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any

from tests_e2e.helpers import (
    REPO_ROOT,
    TempWorkspace,
    auth_headers,
    create_v1_sqlite_db,
    drain_http_events,
    http_json,
    http_request,
    inspect_sqlite,
    inspect_sqlite_schema,
    is_pid_alive,
    mcp_call,
    mcp_initialize,
)


def _tk_driver_preamble() -> str:
    """Return stdlib-only Tkinter initialization + modal neutralizer + headless TclError shim."""
    return '''
import queue as _q
import sys as _sys
import types as _types

_HEADLESS_TK = False
_MODAL_LOG = []

def _install_modal_neutralizers(mb_mod, sd_mod, fd_mod):
    def _info(title=None, message=None, **kw):
        _MODAL_LOG.append(("showinfo", title, message))
        return "ok"
    def _err(title=None, message=None, **kw):
        _MODAL_LOG.append(("showerror", title, message))
        return "ok"
    def _warn(title=None, message=None, **kw):
        _MODAL_LOG.append(("showwarning", title, message))
        return "ok"
    def _yesno(title=None, message=None, **kw):
        _MODAL_LOG.append(("askyesno", title, message))
        return True
    def _askstr(title=None, prompt=None, **kw):
        _MODAL_LOG.append(("askstring", title, prompt))
        return ""
    def _askdir(**kw):
        _MODAL_LOG.append(("askdirectory", kw.get("title")))
        return ""
    mb_mod.showinfo = _info
    mb_mod.showerror = _err
    mb_mod.showwarning = _warn
    mb_mod.askyesno = _yesno
    mb_mod.askokcancel = _yesno
    sd_mod.askstring = _askstr
    fd_mod.askdirectory = _askdir

def _install_headless_shim():
    class TclError(Exception):
        pass

    class _Var:
        def __init__(self, master=None, value=None, name=None):
            self._val = value
        def get(self):
            return self._val
        def set(self, value):
            self._val = value

    class StringVar(_Var):
        def __init__(self, master=None, value="", name=None):
            super().__init__(master, "" if value is None else str(value), name)
        def get(self):
            return "" if self._val is None else str(self._val)
        def set(self, value):
            self._val = "" if value is None else str(value)

    class BooleanVar(_Var):
        def __init__(self, master=None, value=False, name=None):
            super().__init__(master, bool(value), name)
        def get(self):
            return bool(self._val)
        def set(self, value):
            self._val = bool(value)

    class IntVar(_Var):
        def __init__(self, master=None, value=0, name=None):
            super().__init__(master, int(value), name)
        def get(self):
            return int(self._val)
        def set(self, value):
            self._val = int(value)

    class _Widget:
        def __init__(self, master=None, *args, **kwargs):
            self.master = master
            self._opts = dict(kwargs)
            self._text_buf = ""
        def pack(self, *a, **kw): return self
        def grid(self, *a, **kw): return self
        def place(self, *a, **kw): return self
        def pack_forget(self, *a, **kw): return None
        def grid_forget(self, *a, **kw): return None
        def config(self, *a, **kw):
            self._opts.update(kw)
        configure = config
        def cget(self, key):
            return self._opts.get(key, "")
        def __getitem__(self, key):
            return self._opts.get(key, "")
        def __setitem__(self, key, val):
            self._opts[key] = val
        def insert(self, idx, text="", *a, **kw):
            self._text_buf += str(text)
        def delete(self, *a, **kw):
            self._text_buf = ""
        def index(self, spec):
            lines = max(1, self._text_buf.count("\\n") + 1)
            return f"{lines}.0"
        def get(self, *a, **kw):
            return self._text_buf
        def see(self, *a, **kw): return None
        def bind(self, *a, **kw): return None
        def set(self, *a, **kw): return None
        def current(self, *a, **kw): return 0
        def invoke(self, *a, **kw):
            cmd = self._opts.get("command")
            if callable(cmd):
                return cmd()
            return None
        def destroy(self): return None
        def winfo_children(self): return []
        def yview(self, *a, **kw): return None
        def xview(self, *a, **kw): return None

    class Tk(_Widget):
        def __init__(self, *a, **kw):
            super().__init__(None, *a, **kw)
            self._after_q = _q.Queue()
            self._protocols = {}
        def title(self, *a, **kw): return ""
        def geometry(self, *a, **kw): return ""
        def minsize(self, *a, **kw): return None
        def withdraw(self): return None
        def deiconify(self): return None
        def protocol(self, name, func=None):
            if func is not None:
                self._protocols[name] = func
        def after(self, ms, func=None, *args):
            if callable(func):
                self._after_q.put((func, args))
            return "after#1"
        def after_cancel(self, *a): return None
        def update_idletasks(self):
            self._drain_after()
        def update(self):
            self._drain_after()
        def _drain_after(self):
            for _ in range(200):
                try:
                    fn, args = self._after_q.get_nowait()
                except _q.Empty:
                    break
                try:
                    fn(*args)
                except Exception:
                    pass
        def mainloop(self):
            self._drain_after()
        def destroy(self):
            self._drain_after()

    class _DynMod(_types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return _Widget

    tk_mod = _DynMod("tkinter")
    tk_mod.Tk = Tk
    tk_mod.Toplevel = Tk
    tk_mod.TclError = TclError
    tk_mod.StringVar = StringVar
    tk_mod.BooleanVar = BooleanVar
    tk_mod.IntVar = IntVar
    tk_mod.DoubleVar = _Var
    tk_mod.Label = _Widget
    tk_mod.Frame = _Widget
    tk_mod.Button = _Widget
    tk_mod.Entry = _Widget
    tk_mod.Text = _Widget
    tk_mod.END = "end"

    ttk_mod = _DynMod("tkinter.ttk")
    scrolled_mod = _DynMod("tkinter.scrolledtext")
    scrolled_mod.ScrolledText = _Widget
    mb_mod = _types.ModuleType("tkinter.messagebox")
    sd_mod = _types.ModuleType("tkinter.simpledialog")
    fd_mod = _types.ModuleType("tkinter.filedialog")
    _install_modal_neutralizers(mb_mod, sd_mod, fd_mod)

    tk_mod.ttk = ttk_mod
    tk_mod.scrolledtext = scrolled_mod
    tk_mod.messagebox = mb_mod
    tk_mod.simpledialog = sd_mod
    tk_mod.filedialog = fd_mod

    _sys.modules["tkinter"] = tk_mod
    _sys.modules["tkinter.ttk"] = ttk_mod
    _sys.modules["tkinter.scrolledtext"] = scrolled_mod
    _sys.modules["tkinter.messagebox"] = mb_mod
    _sys.modules["tkinter.simpledialog"] = sd_mod
    _sys.modules["tkinter.filedialog"] = fd_mod
    return tk_mod

try:
    import tkinter as tk
    from tkinter import filedialog as _fd, messagebox as _mb, simpledialog as _sd
    root = tk.Tk()
    root.withdraw()
    _install_modal_neutralizers(_mb, _sd, _fd)
except Exception as _exc:
    if type(_exc).__name__ == "TclError" or isinstance(_exc, Exception):
        _HEADLESS_TK = True
        tk = _install_headless_shim()
        root = tk.Tk()
        root.withdraw()
'''


class TestTier4RealWorldWorkloads(unittest.TestCase):
    """End-to-end multi-agent developer workflow scenarios."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_workload_01_multi_ide_pair_programming_handoff(self) -> None:
        """Scenario 1 [F3, F4, F10, F12]: Antigravity + Claude Code + Cursor pair programming & handoff."""
        hub = self.ws.start_hub(token="pair-secret")
        repo = self.ws.create_git_repo("pair_repo")

        for ide in ("antigravity", "claude", "cursor"):
            r_mcp = self.ws.run_cli(
                "setup-mcp",
                "--ide",
                ide,
                "--repo",
                str(repo),
                "--server",
                hub.base_url,
                "--agent",
                f"{ide}-dev",
                "--token",
                "pair-secret",
                cwd=repo,
            )
            self.assertEqual(r_mcp.returncode, 0, r_mcp.stderr or r_mcp.stdout)

        r_hooks = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "ag-dev",
            "--with-all-hooks",
        )
        self.assertEqual(r_hooks.returncode, 0, r_hooks.stderr or r_hooks.stdout)

        mcp_initialize(hub.base_url, token="pair-secret", agent_id="ag-dev")
        for aid, res_path in (
            ("ag-dev", "src/app.py"),
            ("claude-dev", "src/api.py"),
            ("cursor-dev", "src/locked.py"),
        ):
            st_r, rpc_r = mcp_call(
                hub.base_url,
                "sync_register",
                {"agent_id": aid, "role": "developer"},
                token="pair-secret",
                agent_id=aid,
            )
            self.assertEqual(st_r, 200, rpc_r)
            self.assertFalse(rpc_r["result"]["isError"], rpc_r)

            st_l, rpc_l = mcp_call(
                hub.base_url,
                "sync_acquire_lease",
                {"owner_id": aid, "resource": res_path, "reason": "pair work"},
                token="pair-secret",
                agent_id=aid,
            )
            self.assertEqual(st_l, 200, rpc_l)
            self.assertFalse(rpc_l["result"]["isError"], rpc_l)

        # Cross-IDE hook-proxy checks
        ag_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--token",
            "pair-secret",
            "--agent",
            "ag-dev",
            stdin_text=json.dumps(
                {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "api.py")}}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(ag_deny.returncode, 0, ag_deny.stderr)
        self.assertEqual(json.loads(ag_deny.stdout)["decision"], "deny")

        ag_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            "--token",
            "pair-secret",
            "--agent",
            "ag-dev",
            stdin_text=json.dumps(
                {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "app.py")}}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(ag_allow.returncode, 0, ag_allow.stderr)
        self.assertEqual(json.loads(ag_allow.stdout)["decision"], "allow")

        cl_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--token",
            "pair-secret",
            "--agent",
            "claude-dev",
            stdin_text=json.dumps(
                {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "app.py")}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(cl_deny.returncode, 2)
        self.assertIn("ag-dev", cl_deny.stderr)

        cur_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--server",
            hub.base_url,
            "--token",
            "pair-secret",
            "--agent",
            "cursor-dev",
            stdin_text=json.dumps(
                {
                    "hook_event_name": "preToolUse",
                    "tool_name": "edit_file",
                    "tool_input": {"file_path": str(repo / "src" / "app.py")},
                    "cwd": str(repo),
                }
            ),
            cwd=repo,
        )
        self.assertEqual(cur_deny.returncode, 0, cur_deny.stderr)
        cur_out = json.loads(cur_deny.stdout)
        self.assertEqual(cur_out.get("decision") or cur_out.get("permission"), "deny")

        cur_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--server",
            hub.base_url,
            "--token",
            "pair-secret",
            "--agent",
            "cursor-dev",
            stdin_text=json.dumps(
                {
                    "hook_event_name": "preToolUse",
                    "tool_name": "edit_file",
                    "tool_input": {"file_path": str(repo / "src" / "locked.py")},
                    "cwd": str(repo),
                }
            ),
            cwd=repo,
        )
        self.assertEqual(cur_allow.returncode, 0, cur_allow.stderr)
        cur_allow_out = json.loads(cur_allow.stdout)
        self.assertEqual(cur_allow_out.get("decision") or cur_allow_out.get("permission"), "allow")

        _, _, ev0 = http_json(
            "GET",
            f"{hub.base_url}/events?since_id=0&wait=0",
            headers=auth_headers(token="pair-secret"),
        )
        baseline_id = int(ev0["latest_id"])

        holder: dict[str, Any] = {}

        def _claude_wait() -> None:
            t0 = time.monotonic()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {"since_id": baseline_id, "timeout_sec": 10, "agent_id": "claude-dev"},
                token="pair-secret",
                agent_id="claude-dev",
                timeout=20.0,
            )
            holder["elapsed"] = time.monotonic() - t0
            holder["status"] = st
            holder["rpc"] = rpc

        th = threading.Thread(target=_claude_wait, daemon=True)
        th.start()
        time.sleep(0.25)

        mcp_call(
            hub.base_url,
            "sync_release_lease",
            {"owner_id": "ag-dev", "resource": "src/app.py"},
            token="pair-secret",
            agent_id="ag-dev",
        )
        mcp_call(
            hub.base_url,
            "sync_send_message",
            {
                "sender_id": "ag-dev",
                "recipient_id": "claude-dev",
                "msg_type": "handoff",
                "body": "src/app.py ready for claude-dev",
            },
            token="pair-secret",
            agent_id="ag-dev",
        )

        th.join(timeout=6.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(holder["status"], 200)
        self.assertLess(holder["elapsed"], 3.0)
        sc = holder["rpc"]["result"]["structuredContent"]
        self.assertFalse(sc["timed_out"])

        st_poll, rpc_poll = mcp_call(
            hub.base_url,
            "sync_poll_messages",
            {"agent_id": "claude-dev", "unread_only": True, "auto_ack": True},
            token="pair-secret",
            agent_id="claude-dev",
        )
        self.assertEqual(st_poll, 200)
        msgs = rpc_poll["result"]["structuredContent"]["messages"]
        self.assertTrue(any(m["msg_type"] == "handoff" and "app.py" in m["body"] for m in msgs), msgs)

        st_acq, rpc_acq = mcp_call(
            hub.base_url,
            "sync_acquire_lease",
            {"owner_id": "claude-dev", "resource": "src/app.py"},
            token="pair-secret",
            agent_id="claude-dev",
        )
        self.assertEqual(st_acq, 200)
        self.assertFalse(rpc_acq["result"]["isError"], rpc_acq)

        cl_now_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            "--token",
            "pair-secret",
            "--agent",
            "claude-dev",
            stdin_text=json.dumps(
                {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "app.py")}, "cwd": str(repo)}
            ),
            cwd=repo,
        )
        self.assertEqual(cl_now_allow.returncode, 0, cl_now_allow.stderr)

    def test_workload_02_parallel_worktrees_with_shared_junctions(self) -> None:
        """Scenario 2 [F10, F12, F13, F14]: Parallel worktrees with junctions, git pre-commit, and identity guard."""
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("wt_proj")
        (proj / "node_modules" / "shared-lib").mkdir(parents=True, exist_ok=True)
        (proj / "node_modules" / "shared-lib" / "index.js").write_text("export default 1;\n", encoding="utf-8")
        (proj / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (proj / ".venv" / "Scripts" / "pyvenv.cfg").write_text("home = C:\\Python312\n", encoding="utf-8")

        r_wt1 = self.ws.run_cli(
            "worktree",
            "create",
            "wt-agent-1",
            "--repo",
            str(proj),
            "--branch",
            "feat/wt1",
            "--server",
            hub.base_url,
            "--install-hooks",
        )
        r_wt2 = self.ws.run_cli(
            "worktree",
            "create",
            "wt-agent-2",
            "--repo",
            str(proj),
            "--branch",
            "feat/wt2",
            "--server",
            hub.base_url,
            "--install-hooks",
        )
        self.assertEqual(r_wt1.returncode, 0, r_wt1.stderr or r_wt1.stdout)
        self.assertEqual(r_wt2.returncode, 0, r_wt2.stderr or r_wt2.stdout)

        wt1 = self.ws.ws_dir / "wt_proj-wt-1"
        wt2 = self.ws.ws_dir / "wt_proj-wt-2"
        self.assertTrue(wt1.is_dir() and wt2.is_dir())
        self.assertEqual((wt1 / ".agsync-agent").read_text(encoding="utf-8").strip(), "wt-agent-1")
        self.assertEqual((wt2 / ".agsync-agent").read_text(encoding="utf-8").strip(), "wt-agent-2")
        if os.name == "nt":
            self.assertTrue(os.path.isjunction(wt1 / "node_modules"))
            self.assertTrue(os.path.isjunction(wt2 / ".venv"))

        r_list = self.ws.run_cli("worktree", "list", "--repo", str(proj), "--json")
        self.assertEqual(r_list.returncode, 0, r_list.stderr or r_list.stdout)
        wt_data = json.loads(r_list.stdout)
        items = wt_data.get("worktrees", wt_data) if isinstance(wt_data, dict) else wt_data
        self.assertGreaterEqual(len(items), 3)

        # Multi-worktree guard blocks fallback on main repo when identity files absent
        (proj / ".agsync-agent").unlink(missing_ok=True)
        (proj / ".agsync" / "agent_id").unlink(missing_ok=True)
        r_bad = self.ws.run_cli("lock", "src/free.py", "--server", hub.base_url, cwd=proj)
        self.assertNotEqual(r_bad.returncode, 0)
        (proj / ".agsync-agent").write_text("main-dev\n", encoding="utf-8")

        # Per-worktree locking omitting --agent
        self.assertEqual(
            self.ws.run_cli("lock", "src/app.py", "--server", hub.base_url, cwd=wt1).returncode,
            0,
        )
        self.assertEqual(
            self.ws.run_cli("lock", "src/api.py", "--server", hub.base_url, cwd=wt2).returncode,
            0,
        )

        # Hook-proxy in wt2 denies src/app.py citing wt-agent-1
        r_hp = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            stdin_text=json.dumps(
                {"toolCall": {"name": "edit_file", "args": {"Path": str(wt2 / "src" / "app.py")}}, "cwd": str(wt2)}
            ),
            cwd=wt2,
        )
        self.assertEqual(r_hp.returncode, 0, r_hp.stderr)
        hp_out = json.loads(r_hp.stdout)
        self.assertEqual(hp_out["decision"], "deny")
        self.assertIn("wt-agent-1", hp_out.get("reason", ""))

        # Committing own leased file passes pre-commit; committing wt-agent-1's file from wt2 fails pre-commit
        git_env = self.ws.isolated_env({"AGSYNC_SERVER": hub.base_url})
        (wt1 / "src" / "app.py").write_text("def main():\n    return 'wt1'\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/app.py"], cwd=str(wt1), env=git_env, check=True)
        c1 = subprocess.run(
            ["git", "commit", "-m", "feat: update app in wt1"],
            cwd=str(wt1),
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(c1.returncode, 0, c1.stderr or c1.stdout)

        (wt2 / "src" / "api.py").write_text("API_VERSION = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/api.py"], cwd=str(wt2), env=git_env, check=True)
        c2 = subprocess.run(
            ["git", "commit", "-m", "feat: update api in wt2"],
            cwd=str(wt2),
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(c2.returncode, 0, c2.stderr or c2.stdout)

        (wt2 / "src" / "app.py").write_text("# conflicting edit\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/app.py"], cwd=str(wt2), env=git_env, check=True)
        c_conflict = subprocess.run(
            ["git", "commit", "-m", "chore: conflict"],
            cwd=str(wt2),
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(c_conflict.returncode, 0, "Expected pre-commit hook to block committing wt-agent-1's leased file")

        # Remove wt1 and verify shared main caches & wt2 junctions remain intact
        r_rm = self.ws.run_cli("worktree", "remove", "wt-agent-1", "--repo", str(proj), "--force")
        self.assertEqual(r_rm.returncode, 0, r_rm.stderr or r_rm.stdout)
        self.assertFalse(wt1.exists())
        self.assertEqual(
            (proj / "node_modules" / "shared-lib" / "index.js").read_text(encoding="utf-8"),
            "export default 1;\n",
        )
        self.assertEqual(
            (proj / ".venv" / "Scripts" / "pyvenv.cfg").read_text(encoding="utf-8"),
            "home = C:\\Python312\n",
        )
        self.assertTrue((wt2 / ".venv" / "Scripts" / "pyvenv.cfg").is_file())

    def test_workload_03_remote_peer_onboarding_tls_proxy(self) -> None:
        """Scenario 3 [F4, F7, F8, F9, F10]: Simulated remote tunnel onboarding with TLS headers & token scoping."""
        from agsync.adapters.network import build_opener_for_url
        from agsync.adapters.tunnel import build_cloudflared_command

        cmd = build_cloudflared_command("http://127.0.0.1:8765")
        self.assertEqual(
            cmd,
            ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8765", "--protocol", "http2"],
        )

        hub = self.ws.start_hub(
            token="remote-admin-tok",
            base_url="https://devhub.trycloudflare.com",
        )
        local_opener = build_opener_for_url(hub.base_url)
        self.assertEqual(http_json("GET", f"{hub.base_url}/health", opener=local_opener)[0], 200)
        remote_opener = build_opener_for_url("https://devhub.trycloudflare.com")
        proxy_handlers = [h for h in remote_opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(not proxy_handlers or any(h.proxies for h in proxy_handlers))

        r_inv = self.ws.run_cli(
            "invite",
            "--server",
            hub.base_url,
            "--token",
            "remote-admin-tok",
            "--agent",
            "remote-cursor",
            "--json",
        )
        self.assertEqual(r_inv.returncode, 0, r_inv.stderr or r_inv.stdout)
        inv = json.loads(r_inv.stdout)
        self.assertTrue(str(inv["join_url"]).startswith("https://devhub.trycloudflare.com"), inv)

        peer_repo = self.ws.create_git_repo("remote_peer_repo")
        r_join = self.ws.run_cli(
            "join",
            inv["token"],
            "--server",
            hub.base_url,
            "--repo",
            str(peer_repo),
            "--json",
        )
        self.assertEqual(r_join.returncode, 0, r_join.stderr or r_join.stdout)
        joined = json.loads(r_join.stdout)
        peer_token = str(joined.get("token") or (peer_repo / ".agsync" / "token").read_text(encoding="utf-8").strip())

        st_rep, _, _ = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
        )
        self.assertIn(st_rep, (401, 409))

        tls_hdrs = {
            "Host": "devhub.trycloudflare.com",
            "Origin": "https://devhub.trycloudflare.com",
            **auth_headers(token=peer_token, agent_id="remote-cursor"),
        }
        self.assertEqual(http_json("GET", f"{hub.base_url}/snapshot", headers=tls_hdrs)[0], 200)
        self.assertEqual(http_json("GET", f"{hub.base_url}/state/export", headers=tls_hdrs)[0], 403)
        self.assertEqual(http_json("GET", f"{hub.base_url}/invites", headers=tls_hdrs)[0], 403)
        self.assertEqual(
            http_json("POST", f"{hub.base_url}/invites", body={"agent_id": "x"}, headers=tls_hdrs)[0],
            403,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/leases/acquire",
                body={"owner_id": "other-peer", "resource": "src/stolen.py"},
                headers=tls_hdrs,
            )[0],
            403,
        )
        self.assertEqual(
            mcp_call(
                hub.base_url,
                "sync_acquire_lease",
                {"resource": "src/mcp_stolen.py"},
                token=peer_token,
                agent_id="other-peer",
            )[0],
            403,
        )

        # Admin holds src/locked.py; remote-cursor works legitimately
        http_json(
            "POST",
            f"{hub.base_url}/leases/acquire",
            body={"owner_id": "hub-admin", "resource": "src/locked.py"},
            headers=auth_headers(token="remote-admin-tok", agent_id="hub-admin"),
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/agents/register",
                body={"agent_id": "remote-cursor", "role": "remote"},
                headers=tls_hdrs,
            )[0],
            200,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/leases/acquire",
                body={"owner_id": "remote-cursor", "resource": "src/api.py"},
                headers=tls_hdrs,
            )[0],
            200,
        )

        cur_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--server",
            hub.base_url,
            "--token",
            peer_token,
            "--agent",
            "remote-cursor",
            stdin_text=json.dumps(
                {
                    "hook_event_name": "preToolUse",
                    "tool_name": "edit_file",
                    "tool_input": {"file_path": str(peer_repo / "src" / "api.py")},
                    "cwd": str(peer_repo),
                }
            ),
            cwd=peer_repo,
        )
        self.assertEqual(cur_allow.returncode, 0, cur_allow.stderr)
        ca_out = json.loads(cur_allow.stdout)
        self.assertEqual(ca_out.get("decision") or ca_out.get("permission"), "allow")

        cur_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--server",
            hub.base_url,
            "--token",
            peer_token,
            "--agent",
            "remote-cursor",
            stdin_text=json.dumps(
                {
                    "hook_event_name": "preToolUse",
                    "tool_name": "edit_file",
                    "tool_input": {"file_path": str(peer_repo / "src" / "locked.py")},
                    "cwd": str(peer_repo),
                }
            ),
            cwd=peer_repo,
        )
        self.assertEqual(cur_deny.returncode, 0, cur_deny.stderr)
        cd_out = json.loads(cur_deny.stdout)
        self.assertEqual(cd_out.get("decision") or cd_out.get("permission"), "deny")

        st_w, rpc_w = mcp_call(
            hub.base_url,
            "sync_wait",
            {"since_id": 999999, "timeout_sec": 0.3},
            token=peer_token,
            agent_id="remote-cursor",
            query="agent=remote-cursor",
            timeout=10.0,
        )
        self.assertEqual(st_w, 200)
        self.assertFalse(rpc_w["result"]["isError"], rpc_w)

    def test_workload_04_realtime_task_pipeline_sync_wait_and_events(self) -> None:
        """Scenario 4 [F3, F4, F5, F6]: Real-time task pipeline across MCP sync_wait, /events, and /dashboard."""
        hub = self.ws.start_hub()
        st_d, _, raw_d = http_request("GET", f"{hub.base_url}/dashboard")
        self.assertEqual(st_d, 200)
        html = raw_d.decode("utf-8", errors="replace")
        for kw in ("since_id", "boot_id", "Live Activity", "<svg"):
            self.assertIn(kw, html)
        for emoji in ("🎨", "⚙️", "🚀", "🔔", "🔒", "✅", "❌", "🟢", "🔴"):
            self.assertNotIn(emoji, html)

        _, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        boot_id = ev0["boot_id"]
        baseline_id = int(ev0["latest_id"])

        mcp_box: dict[str, Any] = {}
        http_box: dict[str, Any] = {}

        def _mcp_consumer() -> None:
            t0 = time.monotonic()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {
                    "since_id": baseline_id,
                    "boot_id": boot_id,
                    "timeout_sec": 10,
                    "event_types": ["task_created", "context_updated"],
                },
                timeout=20.0,
            )
            mcp_box["elapsed"] = time.monotonic() - t0
            mcp_box["status"] = st
            mcp_box["rpc"] = rpc

        def _http_watcher() -> None:
            t0 = time.monotonic()
            st, _, body = http_json(
                "GET",
                f"{hub.base_url}/events?since_id={baseline_id}&boot_id={boot_id}&wait=10",
                timeout=20.0,
            )
            http_box["elapsed"] = time.monotonic() - t0
            http_box["status"] = st
            http_box["body"] = body

        t_mcp = threading.Thread(target=_mcp_consumer, daemon=True)
        t_http = threading.Thread(target=_http_watcher, daemon=True)
        t_mcp.start()
        t_http.start()
        time.sleep(0.25)

        st_t, rpc_t = mcp_call(
            hub.base_url,
            "sync_create_task",
            {
                "title": "Implement real-time pipeline",
                "created_by": "producer",
                "scope_paths": ["src/app.py"],
            },
            agent_id="producer",
        )
        self.assertEqual(st_t, 200, rpc_t)
        task_id = rpc_t["result"]["structuredContent"]["task_id"]

        st_c, rpc_c = mcp_call(
            hub.base_url,
            "sync_publish_context",
            {"key": "arch_spec", "value": {"pipeline": "v2"}, "updated_by": "producer"},
            agent_id="producer",
        )
        self.assertEqual(st_c, 200, rpc_c)

        t_mcp.join(timeout=6.0)
        t_http.join(timeout=6.0)
        self.assertFalse(t_mcp.is_alive())
        self.assertFalse(t_http.is_alive())
        self.assertLess(mcp_box["elapsed"], 3.0)
        self.assertLess(http_box["elapsed"], 3.0)

        sc_mcp = mcp_box["rpc"]["result"]["structuredContent"]
        self.assertFalse(sc_mcp["timed_out"])
        self.assertFalse(sc_mcp["reset"])
        self.assertFalse(http_box["body"]["reset"])

        # Drain any second event if threads woke on the first mutation
        phase1_events = list(http_box["body"]["events"])
        cursor = int(http_box["body"]["latest_id"])
        if len(phase1_events) < 2:
            extra = drain_http_events(hub.base_url, since_id=cursor)
            phase1_events.extend(extra)
            if extra:
                cursor = int(extra[-1]["id"])
        p1_types = {e["event_type"] for e in phase1_events}
        self.assertIn("task_created", p1_types)
        self.assertIn("context_updated", p1_types)

        # Consumer completes full task lifecycle
        for tool_name, args in (
            ("sync_claim_task", {"task_id": task_id, "agent_id": "consumer"}),
            ("sync_acquire_lease", {"owner_id": "consumer", "resource": "src/app.py"}),
            ("sync_update_task", {"task_id": task_id, "agent_id": "consumer", "status": "done", "result_summary": "completed"}),
            ("sync_release_lease", {"owner_id": "consumer", "resource": "src/app.py"}),
            ("sync_send_message", {"sender_id": "consumer", "recipient_id": "producer", "body": f"{task_id} done"}),
        ):
            st_op, rpc_op = mcp_call(hub.base_url, tool_name, args, agent_id="consumer")
            self.assertEqual(st_op, 200, rpc_op)
            self.assertFalse(rpc_op["result"]["isError"], rpc_op)

        st_fin, _, fin = http_json(
            "GET",
            f"{hub.base_url}/events?since_id={cursor}&boot_id={boot_id}&wait=0&limit=100",
        )
        self.assertEqual(st_fin, 200)
        self.assertFalse(fin["reset"])
        ids = [int(e["id"]) for e in fin["events"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        fin_types = {e["event_type"] for e in fin["events"]}
        for expected in ("task_claimed", "lease_acquired", "task_updated", "lease_released", "message_sent"):
            self.assertIn(expected, fin_types, fin_types)

    def test_workload_05_hub_restart_recovery_boot_id_and_v1_upgrade(self) -> None:
        """Scenario 5 [F2, F3, F4, F5]: v1 DB upgrade + hub restart + boot_id reset + snapshot resync."""
        db_path = self.ws.db_dir / "w05_v1.db"
        create_v1_sqlite_db(db_path, seed_sample_data=True)
        hub = self.ws.start_hub(db_path=db_path)

        st_s1, _, snap1 = http_json("GET", f"{hub.base_url}/snapshot")
        self.assertEqual(st_s1, 200)
        self.assertIn("legacy-agent", snap1["online_agents"])
        self.assertTrue(any(t["task_id"] == "T-0001" for t in snap1["tasks"]))

        st_put, _, _ = http_json(
            "PUT",
            f"{hub.base_url}/context/pre_crash",
            body={"value": {"checkpoint": 1}, "updated_by": "legacy-agent"},
        )
        self.assertEqual(st_put, 200)

        _, _, ev1 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        boot_1 = ev1["boot_id"]
        latest_1 = int(ev1["latest_id"])

        waiter_box: dict[str, Any] = {}

        def _bg_waiter() -> None:
            t0 = time.monotonic()
            try:
                st, _, body = http_json(
                    "GET",
                    f"{hub.base_url}/events?since_id={latest_1}&boot_id={boot_1}&wait=10",
                    timeout=20.0,
                )
                waiter_box["status"] = st
                waiter_box["body"] = body
            except Exception as exc:
                waiter_box["exc"] = str(exc)
            waiter_box["elapsed"] = time.monotonic() - t0

        th = threading.Thread(target=_bg_waiter, daemon=True)
        th.start()
        time.sleep(0.25)

        hub.restart()
        new_base = hub.base_url
        th.join(timeout=6.0)
        self.assertFalse(th.is_alive(), "Waiter thread hung across hub.restart()")
        self.assertLess(waiter_box["elapsed"], 5.0)

        st_rst, _, ev_rst = http_json(
            "GET",
            f"{new_base}/events?since_id={latest_1}&boot_id={boot_1}&wait=0",
        )
        self.assertEqual(st_rst, 200)
        self.assertTrue(ev_rst["reset"])
        boot_2 = ev_rst["boot_id"]
        self.assertNotEqual(boot_2, boot_1)

        st_mcp, rpc_mcp = mcp_call(
            new_base,
            "sync_wait",
            {"since_id": latest_1, "boot_id": boot_1, "timeout_sec": 5},
            timeout=15.0,
        )
        self.assertEqual(st_mcp, 200)
        sc = rpc_mcp["result"]["structuredContent"]
        self.assertTrue(sc["reset"])
        self.assertFalse(sc["timed_out"])
        self.assertEqual(sc["boot_id"], boot_2)

        st_s2, _, snap2 = http_json("GET", f"{new_base}/snapshot")
        self.assertEqual(st_s2, 200)
        self.assertTrue(any(t["task_id"] == "T-0001" for t in snap2["tasks"]))
        self.assertEqual(http_json("GET", f"{new_base}/context/pre_crash")[2]["value"], {"checkpoint": 1})

        _, _, ev_cur = http_json("GET", f"{new_base}/events?since_id=0&wait=0")
        resync_id = int(ev_cur["latest_id"])

        self.assertEqual(
            http_json("POST", f"{new_base}/tasks/T-0001/claim", body={"agent_id": "recovery-agent"})[0],
            200,
        )
        self.assertEqual(
            http_json(
                "PATCH",
                f"{new_base}/tasks/T-0001",
                body={"agent_id": "recovery-agent", "status": "done", "result_summary": "recovered"},
            )[0],
            200,
        )

        st_inc, _, ev_inc = http_json(
            "GET",
            f"{new_base}/events?since_id={resync_id}&boot_id={boot_2}&wait=0",
        )
        self.assertEqual(st_inc, 200)
        self.assertFalse(ev_inc["reset"])
        inc_types = {e["event_type"] for e in ev_inc["events"]}
        self.assertIn("task_claimed", inc_types)
        self.assertIn("task_updated", inc_types)

        hub.stop()
        self.assertEqual(inspect_sqlite_schema(db_path)["user_version"], 2)

    def test_workload_06_cursor_claude_hook_dedup_and_chained_linter(self) -> None:
        """Scenario 6 [F10, F11, F12]: Cursor + .claude/ dedup, chained user linter, and --record redaction."""
        hub = self.ws.start_hub(token="hook-secret")
        repo = self.ws.create_git_repo("w06_dedup")
        linter_log = repo / "linter_runs.log"
        linter_script = repo / "user_linter.py"
        linter_script.write_text(
            f"from pathlib import Path\nwith Path({str(linter_log)!r}).open('a', encoding='utf-8') as f:\n    f.write('linted\\n')\n",
            encoding="utf-8",
        )
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [{"type": "command", "command": f'"{sys.executable}" "{linter_script}"'}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        r_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "cur-dev",
            "--token",
            "hook-secret",
            "--with-claude-hook",
            "--with-cursor-hook",
        )
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        cur_payload = json.dumps(
            {
                "hook_event_name": "preToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
                "cursor_version": "0.50.0",
                "authorization": "Bearer hook-secret",
            }
        )
        cl_payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
                "cursor_version": "0.50.0",
                "authorization": "Bearer hook-secret",
            }
        )
        r1 = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--record",
            "--server",
            hub.base_url,
            "--agent",
            "cur-dev",
            "--token",
            "hook-secret",
            stdin_text=cur_payload,
            cwd=repo,
        )
        r2 = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--record",
            "--server",
            hub.base_url,
            "--agent",
            "cur-dev",
            "--token",
            "hook-secret",
            stdin_text=cl_payload,
            cwd=repo,
        )
        self.assertEqual((r1.returncode, r2.returncode), (0, 0), f"{r1.stderr} | {r2.stderr}")

        dedup_dir = repo / ".agsync" / "hook-dedup"
        self.assertTrue(dedup_dir.is_dir() and any(dedup_dir.iterdir()))
        if linter_log.exists():
            runs = [ln for ln in linter_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(runs), 1, f"Linter ran {len(runs)} times")

        samples = list((repo / ".agsync" / "hook-samples").glob("*.json"))
        self.assertGreaterEqual(len(samples), 1)
        for s in samples:
            txt = s.read_text(encoding="utf-8")
            self.assertNotIn("hook-secret", txt)
            self.assertIn("decision", json.loads(txt))

        # Lease conflict causes deny and must not invoke chained linter
        st_l, _, _ = http_json(
            "POST",
            f"{hub.base_url}/leases/acquire",
            body={"owner_id": "peer-holder", "resource": "src/locked.py"},
        )
        self.assertEqual(st_l, 200)
        deny_payload = json.dumps(
            {
                "hook_event_name": "preToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "locked.py")},
                "cwd": str(repo),
                "cursor_version": "0.50.0",
            }
        )
        r_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "cursor",
            "--record",
            "--server",
            hub.base_url,
            "--agent",
            "cur-dev",
            "--token",
            "hook-secret",
            stdin_text=deny_payload,
            cwd=repo,
        )
        self.assertEqual(r_deny.returncode, 0, r_deny.stderr)
        d_out = json.loads(r_deny.stdout)
        self.assertEqual(d_out.get("decision") or d_out.get("permission"), "deny")
        self.assertIn("peer-holder", json.dumps(d_out))
        if linter_log.exists():
            runs_after = [ln for ln in linter_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(runs_after), 1)

    def test_workload_07_high_throughput_burst_and_ring_buffer(self) -> None:
        """Scenario 7 [F3, F5]: 1200 mixed concurrent mutations + live /events drainer + 1000-row ring buffer."""
        hub = self.ws.start_hub()
        assert hub.port is not None
        host = "127.0.0.1"
        port = int(hub.port)

        for idx in range(4):
            aid = f"burst-{idx}"
            st, _, _ = http_json(
                "POST",
                f"{hub.base_url}/agents/register",
                body={"agent_id": aid, "role": "burst", "branch": "main", "status": "online"},
            )
            self.assertEqual(st, 200)

        errors: list[str] = []
        stop_drain = threading.Event()
        drainer_stats: dict[str, Any] = {"latest_ids": [], "batches": 0}

        def _req_json(
            conn: http.client.HTTPConnection,
            method: str,
            path: str,
            body_obj: dict[str, Any],
        ) -> tuple[int, dict[str, Any]]:
            payload = json.dumps(body_obj).encode("utf-8")
            hdrs = {
                "Host": f"{host}:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "Connection": "keep-alive",
            }
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            return int(resp.status), parsed

        def _producer(worker_idx: int) -> None:
            aid = f"burst-{worker_idx}"
            conn = http.client.HTTPConnection(host, port, timeout=20.0)
            try:
                for cycle in range(50):
                    # 6 mutations per cycle * 50 cycles = 300 mutations per thread (1200 total)
                    st1, _ = _req_json(conn, "PUT", f"/context/b_{worker_idx}_{cycle}", {"value": cycle, "updated_by": aid})
                    if st1 != 200:
                        errors.append(f"PUT status {st1}")
                    st2, _ = _req_json(conn, "POST", "/messages", {"sender_id": aid, "recipient_id": "*", "body": f"m{cycle}"})
                    if st2 != 201:
                        errors.append(f"MSG status {st2}")
                    res_name = f"src/burst_{worker_idx}.py"
                    st3, _ = _req_json(conn, "POST", "/leases/acquire", {"owner_id": aid, "resource": res_name})
                    if st3 != 200:
                        errors.append(f"ACQ status {st3}")
                    st4, _ = _req_json(conn, "POST", "/leases/release", {"owner_id": aid, "resource": res_name})
                    if st4 != 200:
                        errors.append(f"REL status {st4}")
                    st5, t_body = _req_json(conn, "POST", "/tasks", {"title": f"Task {worker_idx}-{cycle}", "created_by": aid})
                    if st5 != 201:
                        errors.append(f"TASK status {st5}")
                    tid = t_body["task_id"]
                    st6, _ = _req_json(conn, "POST", f"/tasks/{tid}/claim", {"agent_id": aid})
                    if st6 != 200:
                        errors.append(f"CLAIM status {st6}")
            except Exception as exc:
                errors.append(f"producer {worker_idx} exc: {exc}")
            finally:
                conn.close()

        def _drainer() -> None:
            conn = http.client.HTTPConnection(host, port, timeout=20.0)
            cursor = 0
            try:
                while not stop_drain.is_set():
                    conn.request(
                        "GET",
                        f"/events?since_id={cursor}&wait=1&limit=200",
                        headers={"Host": f"{host}:{port}", "Connection": "keep-alive"},
                    )
                    resp = conn.getresponse()
                    raw = resp.read()
                    if resp.status != 200:
                        errors.append(f"drainer status {resp.status}")
                        break
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    latest = int(data["latest_id"])
                    if drainer_stats["latest_ids"] and latest < drainer_stats["latest_ids"][-1]:
                        errors.append(f"latest_id regressed: {latest} < {drainer_stats['latest_ids'][-1]}")
                    drainer_stats["latest_ids"].append(latest)
                    batch_ids = [int(e["id"]) for e in data.get("events", [])]
                    if batch_ids != sorted(batch_ids) or len(batch_ids) != len(set(batch_ids)):
                        errors.append(f"non-increasing batch ids: {batch_ids[:10]}")
                    cursor = latest
                    drainer_stats["batches"] += 1
            except Exception as exc:
                if not stop_drain.is_set():
                    errors.append(f"drainer exc: {exc}")
            finally:
                conn.close()

        d_thread = threading.Thread(target=_drainer, daemon=True)
        d_thread.start()
        p_threads = [threading.Thread(target=_producer, args=(i,), daemon=True) for i in range(4)]
        for t in p_threads:
            t.start()
        for t in p_threads:
            t.join(timeout=45.0)
            self.assertFalse(t.is_alive(), "Producer thread timed out")

        stop_drain.set()
        d_thread.join(timeout=6.0)
        self.assertEqual(errors, [], f"Burst errors: {errors}")
        self.assertGreaterEqual(drainer_stats["batches"], 1)

        hub.stop()
        row = inspect_sqlite(
            hub.db_path,
            "SELECT COUNT(*) AS cnt, MIN(id) AS min_id, MAX(id) AS max_id FROM events;",
        )[0]
        self.assertEqual(row["cnt"], 1000)
        self.assertGreaterEqual(row["max_id"], 1200)
        self.assertGreater(row["min_id"], 200)

    def test_workload_08_control_panel_gui_host_peer_lifecycle(self) -> None:
        """Scenario 8 [F7, F9, F12, F13, F15]: Full Control Panel GUI host/peer lifecycle without UI blocking."""
        hub = self.ws.start_hub(token="gui-admin-tok")
        proj = self.ws.create_git_repo("gui_lifecycle")
        wt_path = self.ws.ws_dir / "gui_lifecycle-wt-1"

        driver = self.ws.root / "w08_tk_driver.py"
        script = _tk_driver_preamble() + f"""
import json, time
from pathlib import Path
try:
    from agsync.config import load_config
    from scripts import control_panel as cp_mod
    if not _HEADLESS_TK:
        _install_modal_neutralizers(cp_mod.messagebox, cp_mod.simpledialog, cp_mod.filedialog)

    ok_health, _ = cp_mod.check_health({hub.base_url!r})
    assert ok_health, "check_health failed"

    panel = cp_mod.AgsyncControlPanel(root)
    panel.host_ip_var.set("127.0.0.1")
    panel.port_var.set({str(hub.port)!r})
    panel.token_var.set("gui-admin-tok")
    panel.repo_var.set({str(proj)!r})
    panel.agent_var.set("gui-host-agent")

    base_attr = next((a for a in ("base_url_var", "remote_url_var", "public_url_var") if hasattr(panel, a)), None)
    assert base_attr is not None, "Missing base_url var"
    getattr(panel, base_attr).set("https://gui-hub.trycloudflare.com")
    panel._save_current_config()

    for attr in ("ide_var", "target_ide_var", "setup_ide_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("claude")
            break
    ide_fn = next((getattr(panel, n) for n in ("setup_selected_ide", "connect_ide_mcp", "setup_multi_ide", "setup_ide", "connect_mcp") if hasattr(panel, n)), None)
    assert ide_fn is not None, "Missing IDE setup handler"
    t0 = time.monotonic()
    ide_fn()
    ide_elapsed = time.monotonic() - t0
    assert ide_elapsed < 0.5, f"IDE handler blocked UI thread ({{ide_elapsed:.2f}}s)"
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and not (Path({str(proj)!r}) / ".mcp.json").is_file():
        root.update()
        time.sleep(0.05)

    for attr in ("wt_agent_var", "worktree_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-wt-peer")
    create_fn = next((getattr(panel, n) for n in ("create_worktree", "worktree_create", "on_create_worktree") if hasattr(panel, n)), None)
    list_fn = next((getattr(panel, n) for n in ("list_worktrees", "worktree_list", "refresh_worktrees") if hasattr(panel, n)), None)
    remove_fn = next((getattr(panel, n) for n in ("remove_worktree", "worktree_remove", "on_remove_worktree") if hasattr(panel, n)), None)
    assert create_fn and list_fn and remove_fn, "Missing worktree handlers"

    t0 = time.monotonic()
    create_fn()
    wt_create_elapsed = time.monotonic() - t0
    assert wt_create_elapsed < 0.5, f"create_worktree blocked UI thread ({{wt_create_elapsed:.2f}}s)"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not Path({str(wt_path)!r}).is_dir():
        root.update()
        time.sleep(0.05)
    assert Path({str(wt_path)!r}).is_dir(), "Worktree not created"

    t0 = time.monotonic()
    list_fn()
    wt_list_elapsed = time.monotonic() - t0
    assert wt_list_elapsed < 0.5, f"list_worktrees blocked UI thread ({{wt_list_elapsed:.2f}}s)"
    root.update()

    for attr in ("wt_target_var", "worktree_target_var", "wt_agent_var", "worktree_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-wt-peer")
    t0 = time.monotonic()
    remove_fn()
    wt_rm_elapsed = time.monotonic() - t0
    assert wt_rm_elapsed < 0.5, f"remove_worktree blocked UI thread ({{wt_rm_elapsed:.2f}}s)"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and Path({str(wt_path)!r}).exists():
        root.update()
        time.sleep(0.05)
    assert not Path({str(wt_path)!r}).exists(), "Worktree not removed"

    for attr in ("invite_agent_var", "peer_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-remote-peer")
    inv_fn = next((getattr(panel, n) for n in ("create_invite", "generate_invite", "on_create_invite") if hasattr(panel, n)), None)
    join_fn = next((getattr(panel, n) for n in ("join_invite", "join_hub", "on_join_invite") if hasattr(panel, n)), None)
    assert inv_fn and join_fn, "Missing invite/join handlers"

    t0 = time.monotonic()
    inv_fn()
    inv_elapsed = time.monotonic() - t0
    assert inv_elapsed < 0.5, f"create_invite blocked UI thread ({{inv_elapsed:.2f}}s)"
    deadline = time.monotonic() + 6.0
    inv_tok = ""
    while time.monotonic() < deadline:
        root.update()
        for attr in ("invite_link_var", "join_token_var", "invite_token_var", "join_link_var"):
            if hasattr(panel, attr) and getattr(panel, attr).get().strip():
                inv_tok = getattr(panel, attr).get().strip()
                break
        if inv_tok:
            break
        time.sleep(0.05)
    assert inv_tok, "Invite token/link var empty"

    t0 = time.monotonic()
    join_fn()
    join_elapsed = time.monotonic() - t0
    assert join_elapsed < 0.5, f"join_invite blocked UI thread ({{join_elapsed:.2f}}s)"
    tok_file = Path({str(proj)!r}) / ".agsync" / "token"
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and not tok_file.is_file():
        root.update()
        time.sleep(0.05)
    assert tok_file.is_file(), "Joined repo token missing"

    print(json.dumps({{
        "headless": _HEADLESS_TK,
        "cfg": load_config(),
        "peer_token": tok_file.read_text(encoding="utf-8").strip(),
    }}))
finally:
    try:
        panel._job.close()
    except Exception:
        pass
    root.destroy()
"""
        driver.write_text(script, encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(driver)],
            cwd=str(REPO_ROOT),
            env=self.ws.isolated_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30.0,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"GUI driver failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        out = json.loads(res.stdout)
        self.assertIn("https://gui-hub.trycloudflare.com", json.dumps(out["cfg"]))

        st_snap, _, _ = http_json(
            "GET",
            f"{hub.base_url}/snapshot",
            headers=auth_headers(token=out["peer_token"], agent_id="gui-remote-peer"),
        )
        self.assertEqual(st_snap, 200)

    def test_workload_09_hybrid_stdio_mcp_rest_and_chain_timeout_recovery(self) -> None:
        """Scenario 9 [F4, F11, F12, F16]: Templates + bilingual docs + stdio mcp_bridge woken by REST + tree-kill."""
        # Verify templates and bilingual docs
        tdir = REPO_ROOT / "templates"
        tfiles = [p for p in tdir.rglob("*") if p.is_file()]
        self.assertGreater(len(tfiles), 4)
        combined = ""
        for f in tfiles:
            txt = f.read_text(encoding="utf-8")
            combined += f"\n{f.as_posix()}\n{txt}\n"
            if f.suffix.lower() == ".json":
                json.loads(txt)
                self.assertNotIn("/sse", txt, f"Legacy /sse in {f}")
        low = combined.lower()
        for target in ("windsurf", "codex", "gemini"):
            self.assertIn(target, low)
        for ep in ("/leases/acquire", "/tasks", "/messages", "/events"):
            self.assertIn(ep, combined)

        for fname in ("README.md", "README.fa.md"):
            txt = (REPO_ROOT / fname).read_text(encoding="utf-8")
            for kw in ("hook-proxy", "worktree", "invite", "join", "sync_wait", "base-url"):
                self.assertIn(kw, txt, f"{fname} missing {kw}")
        for fname in ("docs/SETUP_GUIDE.md", "docs/SETUP_GUIDE.fa.md", "docs/ARCHITECTURE.md", "docs/ARCHITECTURE.fa.md"):
            self.assertTrue((REPO_ROOT / fname).is_file(), f"Missing {fname}")

        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("w09_hybrid")
        pids_file = repo / "w09_pids.json"
        hang_script = repo / "w09_hang.py"
        hang_script.write_text(
            (
                "import json, os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"Path({str(pids_file)!r}).write_text(json.dumps([os.getpid(), child.pid]), encoding='utf-8')\n"
                "time.sleep(30)\n"
            ),
            encoding="utf-8",
        )
        (repo / ".agents").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "hooks.json").write_text(
            json.dumps(
                {"hooks": {"PreToolUse": [{"command": f'"{sys.executable}" "{hang_script}"', "show_output": False}]}}
            ),
            encoding="utf-8",
        )
        r_inst = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "windsurf-cli",
            "--with-antigravity-hook",
        )
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        _, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        baseline_id = int(ev0["latest_id"])

        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "agsync.mcp_bridge",
                "--server",
                hub.base_url,
                "--agent",
                "windsurf-cli",
            ],
            cwd=str(REPO_ROOT),
            env=self.ws.isolated_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.ws.register_proc(proc)
        assert proc.stdin is not None and proc.stdout is not None

        def _send(req: dict[str, Any]) -> None:
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()

        def _recv(timeout_sec: float = 12.0) -> dict[str, Any]:
            box: dict[str, Any] = {}
            def _r() -> None:
                box["line"] = proc.stdout.readline()
            rt = threading.Thread(target=_r, daemon=True)
            rt.start()
            rt.join(timeout=timeout_sec)
            self.assertFalse(rt.is_alive(), "Timed out reading from mcp_bridge")
            raw = box.get("line", "")
            self.assertTrue(raw.strip(), "mcp_bridge closed stdout")
            return json.loads(raw)

        _send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            }
        )
        self.assertIn("result", _recv())

        _send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "sync_acquire_lease", "arguments": {"resource": "src/app.py"}},
            }
        )
        r_acq = _recv()
        self.assertFalse(r_acq["result"]["isError"], r_acq)

        _, _, ev_mid = http_json("GET", f"{hub.base_url}/events?since_id={baseline_id}&wait=0")
        wait_cursor = int(ev_mid["latest_id"])

        t0 = time.monotonic()
        _send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "sync_wait", "arguments": {"since_id": wait_cursor, "timeout_sec": 6.0}},
            }
        )
        time.sleep(0.25)
        st_m, _, _ = http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "rest-partner", "recipient_id": "windsurf-cli", "body": "wake stdio"},
        )
        self.assertEqual(st_m, 201)

        r_wait = _recv(timeout_sec=10.0)
        elapsed_wait = time.monotonic() - t0
        self.assertLess(elapsed_wait, 3.5)
        self.assertFalse(r_wait["result"]["isError"], r_wait)
        self.assertFalse(r_wait["result"]["structuredContent"]["timed_out"])

        proc.stdin.close()
        rc = proc.wait(timeout=8.0)
        err_txt = proc.stderr.read() if proc.stderr is not None else ""
        proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        self.assertEqual(rc, 0, err_txt)

        # Verify hook-proxy recovers via --chain-timeout and kills entire process tree
        t_hp = time.monotonic()
        r_hp = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--chain-timeout",
            "0.5",
            "--server",
            hub.base_url,
            "--agent",
            "windsurf-cli",
            stdin_text=json.dumps(
                {"toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "free.py")}}, "cwd": str(repo)}
            ),
            cwd=repo,
            timeout=15.0,
        )
        elapsed_hp = time.monotonic() - t_hp
        self.assertEqual(r_hp.returncode, 0, r_hp.stderr)
        self.assertLess(elapsed_hp, 6.0)
        self.assertEqual(json.loads(r_hp.stdout)["decision"], "allow")
        self.assertTrue(pids_file.is_file())
        pids = json.loads(pids_file.read_text(encoding="utf-8"))
        self.assertEqual(len(pids), 2)
        time.sleep(0.3)
        for pid in pids:
            self.assertFalse(is_pid_alive(int(pid)), f"Orphan PID {pid} survived tree kill")


if __name__ == "__main__":
    unittest.main()
