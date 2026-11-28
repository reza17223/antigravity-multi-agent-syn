"""Tier 3 Opaque-Box Pairwise Cross-Feature E2E Tests covering F2-F16.

Zero third-party dependencies (Python 3.12 stdlib only).
Zero top-level imports of unimplemented Phase 2 symbols (`agsync.*` / `scripts.*`).
Zero mocks (`unittest.mock`) and zero `skipTest` calls.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
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
    send_persistent_put_batch,
)


def _register_agent(
    base_url: str,
    agent_id: str,
    *,
    token: str | None = None,
    role: str = "agent",
) -> dict[str, Any]:
    st, _, body = http_json(
        "POST",
        f"{base_url}/agents/register",
        body={"agent_id": agent_id, "role": role, "branch": "main", "status": "online"},
        headers=auth_headers(token=token, agent_id=agent_id),
    )
    if st != 200 or not isinstance(body, dict):
        raise AssertionError(f"Register {agent_id} failed ({st}): {body}")
    return body


def _acquire_lease(
    base_url: str,
    owner_id: str,
    resource: str,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    st, _, body = http_json(
        "POST",
        f"{base_url}/leases/acquire",
        body={"owner_id": owner_id, "resource": resource, "scope": "file", "reason": "pairwise e2e"},
        headers=auth_headers(token=token, agent_id=owner_id),
    )
    if st != 200 or not isinstance(body, dict):
        raise AssertionError(f"Acquire lease {resource} for {owner_id} failed ({st}): {body}")
    return body


def _onboard_peer(base_url: str, admin_token: str, agent_id: str) -> str:
    st_i, _, inv = http_json(
        "POST",
        f"{base_url}/invites",
        body={"agent_id": agent_id, "ttl_sec": 300, "role": "agent"},
        headers=auth_headers(token=admin_token),
    )
    if st_i not in (200, 201) or not isinstance(inv, dict):
        raise AssertionError(f"POST /invites for {agent_id} failed ({st_i}): {inv}")
    st_j, _, joined = http_json(
        "POST",
        f"{base_url}/invites/join",
        body={"invite_token": inv["token"]},
    )
    if st_j != 200 or not isinstance(joined, dict):
        raise AssertionError(f"POST /invites/join for {agent_id} failed ({st_j}): {joined}")
    return str(joined["token"])


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


class TestPairwiseRealtimeCoreAndAuth(unittest.TestCase):
    """Pairwise interactions across F2, F3, F4, F5, F7, F8."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_p01_v1_migration_emits_events_and_wakes_get_events_long_poll(self) -> None:
        """[F2 x F3 x F5] Seeded v1 DB migrates to v2 and wakes GET /events long-poll on v1 task claim."""
        db_path = self.ws.db_dir / "p01_v1_mig.db"
        create_v1_sqlite_db(db_path, seed_sample_data=True)
        self.assertEqual(inspect_sqlite_schema(db_path)["user_version"], 1)

        hub = self.ws.start_hub(db_path=db_path)
        st_s, _, snap = http_json("GET", f"{hub.base_url}/snapshot")
        self.assertEqual(st_s, 200)
        self.assertIn("legacy-agent", snap["online_agents"])
        self.assertTrue(any(t["task_id"] == "T-0001" for t in snap["tasks"]))
        self.assertTrue(snap.get("boot_id"))
        self.assertIn("latest_event_id", snap)

        st_k, _, ctx = http_json("GET", f"{hub.base_url}/context/v1_key")
        self.assertEqual(st_k, 200)
        self.assertEqual(ctx["value"], {"migrated": True})

        st_e0, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        self.assertEqual(st_e0, 200)
        boot_id = ev0["boot_id"]
        baseline_id = int(ev0["latest_id"])

        holder: dict[str, Any] = {}

        def _poll() -> None:
            t0 = time.monotonic()
            st, _, body = http_json(
                "GET",
                f"{hub.base_url}/events?since_id={baseline_id}&boot_id={boot_id}&wait=10",
                timeout=20.0,
            )
            holder["elapsed"] = time.monotonic() - t0
            holder["status"] = st
            holder["body"] = body

        th = threading.Thread(target=_poll, daemon=True)
        th.start()
        time.sleep(0.25)

        st_c, _, claimed = http_json(
            "POST",
            f"{hub.base_url}/tasks/T-0001/claim",
            body={"agent_id": "migrated-worker"},
        )
        self.assertEqual(st_c, 200, claimed)
        th.join(timeout=6.0)
        self.assertFalse(th.is_alive(), "GET /events long-poll did not wake up")
        self.assertEqual(holder["status"], 200)
        self.assertLess(holder["elapsed"], 3.0)
        body = holder["body"]
        self.assertFalse(body["reset"])
        self.assertTrue(
            any(
                ev["event_type"] == "task_claimed"
                and ev["target_id"] == "T-0001"
                and ev["actor_id"] == "migrated-worker"
                for ev in body["events"]
            ),
            body["events"],
        )

        hub.stop()
        schema = inspect_sqlite_schema(db_path)
        self.assertEqual(schema["user_version"], 2)
        rows = inspect_sqlite(
            db_path,
            "SELECT event_type, actor_id, target_id FROM events WHERE event_type = ?;",
            ("task_claimed",),
        )
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_id"], "T-0001")

    def test_p02_sync_wait_condition_wakeup_on_send_message_and_lease_release(self) -> None:
        """[F4 x F3 x F5] Condition wakes sync_wait on lease_released and message_sent with filters."""
        hub = self.ws.start_hub()
        _register_agent(hub.base_url, "agent-a")
        _register_agent(hub.base_url, "agent-b")
        _acquire_lease(hub.base_url, "agent-b", "src/shared.py")

        _, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        boot_id = ev0["boot_id"]
        latest_id = int(ev0["latest_id"])

        h1: dict[str, Any] = {}

        def _wait_release() -> None:
            t0 = time.monotonic()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {
                    "since_id": latest_id,
                    "boot_id": boot_id,
                    "timeout_sec": 10,
                    "event_types": ["message_sent", "lease_released"],
                },
                timeout=20.0,
            )
            h1["elapsed"] = time.monotonic() - t0
            h1["status"] = st
            h1["rpc"] = rpc

        th1 = threading.Thread(target=_wait_release, daemon=True)
        th1.start()
        time.sleep(0.25)

        st_rel, rpc_rel = mcp_call(
            hub.base_url,
            "sync_release_lease",
            {"resource": "src/shared.py"},
            agent_id="agent-b",
        )
        self.assertEqual(st_rel, 200, rpc_rel)
        self.assertFalse(rpc_rel["result"]["isError"], rpc_rel)

        th1.join(timeout=6.0)
        self.assertFalse(th1.is_alive())
        self.assertEqual(h1["status"], 200)
        self.assertLess(h1["elapsed"], 3.0)
        sc1 = h1["rpc"]["result"]["structuredContent"]
        self.assertFalse(sc1["timed_out"])
        self.assertFalse(sc1["reset"])
        self.assertTrue(any(ev["event_type"] == "lease_released" for ev in sc1["events"]), sc1)

        cursor = int(sc1["latest_id"])
        h2: dict[str, Any] = {}

        def _wait_msg() -> None:
            t0 = time.monotonic()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {
                    "since_id": cursor,
                    "boot_id": boot_id,
                    "timeout_sec": 10,
                    "agent_id": "agent-a",
                    "event_types": ["message_sent"],
                },
                timeout=20.0,
            )
            h2["elapsed"] = time.monotonic() - t0
            h2["status"] = st
            h2["rpc"] = rpc

        th2 = threading.Thread(target=_wait_msg, daemon=True)
        th2.start()
        time.sleep(0.25)

        st_msg, rpc_msg = mcp_call(
            hub.base_url,
            "sync_send_message",
            {"recipient_id": "agent-a", "body": "src/shared.py released"},
            agent_id="agent-b",
        )
        self.assertEqual(st_msg, 200, rpc_msg)
        self.assertFalse(rpc_msg["result"]["isError"], rpc_msg)

        th2.join(timeout=6.0)
        self.assertFalse(th2.is_alive())
        self.assertEqual(h2["status"], 200)
        self.assertLess(h2["elapsed"], 3.0)
        sc2 = h2["rpc"]["result"]["structuredContent"]
        self.assertFalse(sc2["timed_out"])
        self.assertTrue(any(ev["event_type"] == "message_sent" for ev in sc2["events"]), sc2)

        acq = _acquire_lease(hub.base_url, "agent-a", "src/shared.py")
        lease_obj = acq.get("lease", acq)
        self.assertEqual(lease_obj["owner_id"], "agent-a")

    def test_p03_hub_restart_boot_id_change_triggers_events_reset_and_snapshot_resync(self) -> None:
        """[F3 x F5 x F2] Hub restart rotates boot_id, triggers reset=True, and preserves DB state."""
        hub = self.ws.start_hub(token="adm-tok")
        hdrs = auth_headers(token="adm-tok")
        _register_agent(hub.base_url, "r-agent", token="adm-tok")
        st_t, _, task = http_json(
            "POST",
            f"{hub.base_url}/tasks",
            body={"title": "R-1", "created_by": "r-agent"},
            headers=hdrs,
        )
        self.assertEqual(st_t, 201, task)
        st_c, _, _ = http_json(
            "PUT",
            f"{hub.base_url}/context/cfg_v",
            body={"value": {"v": 1}, "updated_by": "r-agent"},
            headers=hdrs,
        )
        self.assertEqual(st_c, 200)

        _, _, ev1 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0", headers=hdrs)
        boot_1 = ev1["boot_id"]
        latest_1 = int(ev1["latest_id"])

        hub.restart()
        new_base = hub.base_url

        t0 = time.monotonic()
        st_rst, _, ev2 = http_json(
            "GET",
            f"{new_base}/events?since_id={latest_1}&boot_id={boot_1}&wait=10",
            headers=hdrs,
            timeout=20.0,
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(st_rst, 200)
        self.assertLess(elapsed, 2.0)
        self.assertTrue(ev2["reset"])
        boot_2 = ev2["boot_id"]
        self.assertNotEqual(boot_2, boot_1)

        st_snap, _, snap = http_json("GET", f"{new_base}/snapshot", headers=hdrs)
        self.assertEqual(st_snap, 200)
        self.assertTrue(any(a["agent_id"] == "r-agent" for a in snap["agents"]))
        self.assertTrue(any(t["title"] == "R-1" for t in snap["tasks"]))
        st_ctx, _, ctx_v1 = http_json("GET", f"{new_base}/context/cfg_v", headers=hdrs)
        self.assertEqual(st_ctx, 200)
        self.assertEqual(ctx_v1["value"], {"v": 1})

        _, _, ev_cur = http_json("GET", f"{new_base}/events?since_id=0&wait=0", headers=hdrs)
        new_latest = int(ev_cur["latest_id"])

        st_c2, _, _ = http_json(
            "PUT",
            f"{new_base}/context/cfg_v",
            body={"value": {"v": 2}, "updated_by": "r-agent"},
            headers=hdrs,
        )
        self.assertEqual(st_c2, 200)

        st_inc, _, ev3 = http_json(
            "GET",
            f"{new_base}/events?since_id={new_latest}&boot_id={boot_2}&wait=5",
            headers=hdrs,
            timeout=15.0,
        )
        self.assertEqual(st_inc, 200)
        self.assertFalse(ev3["reset"])
        self.assertTrue(any(e["event_type"] == "context_updated" for e in ev3["events"]), ev3)

    def test_p04_cli_invite_and_join_scoped_token_executes_sync_wait(self) -> None:
        """[F7 x F8 x F4] CLI invite + join yields scoped token that executes sync_wait and rejects replay."""
        hub = self.ws.start_hub(token="adm-tok")
        peer_repo = self.ws.create_git_repo("peer_join")

        res_inv = self.ws.run_cli(
            "invite",
            "--server",
            hub.base_url,
            "--token",
            "adm-tok",
            "--agent",
            "peer-1",
            "--ttl",
            "300",
            "--json",
        )
        self.assertEqual(res_inv.returncode, 0, res_inv.stderr or res_inv.stdout)
        inv_data = json.loads(res_inv.stdout)
        inv_token = inv_data["token"]

        res_join = self.ws.run_cli(
            "join",
            inv_token,
            "--server",
            hub.base_url,
            "--repo",
            str(peer_repo),
            "--json",
        )
        self.assertEqual(res_join.returncode, 0, res_join.stderr or res_join.stdout)
        join_data = json.loads(res_join.stdout)

        tok_file = peer_repo / ".agsync" / "token"
        srv_file = peer_repo / ".agsync" / "server_url"
        self.assertTrue(tok_file.is_file())
        self.assertTrue(srv_file.is_file())
        self.assertTrue((peer_repo / ".agsync-agent").is_file() or (peer_repo / ".agsync" / "agent_id").is_file())
        peer_token = str(join_data.get("token") or tok_file.read_text(encoding="utf-8").strip())

        st_ev0, _, ev0 = http_json(
            "GET",
            f"{hub.base_url}/events?since_id=0&wait=0",
            headers=auth_headers(token=peer_token, agent_id="peer-1"),
        )
        self.assertEqual(st_ev0, 200, ev0)
        baseline_id = int(ev0["latest_id"])

        holder: dict[str, Any] = {}

        def _peer_wait() -> None:
            t0 = time.monotonic()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {"since_id": baseline_id, "timeout_sec": 8, "agent_id": "peer-1"},
                token=peer_token,
                agent_id="peer-1",
                query="agent=peer-1",
                timeout=18.0,
            )
            holder["elapsed"] = time.monotonic() - t0
            holder["status"] = st
            holder["rpc"] = rpc

        th = threading.Thread(target=_peer_wait, daemon=True)
        th.start()
        time.sleep(0.25)

        st_msg, _, msg_resp = http_json(
            "POST",
            f"{hub.base_url}/messages",
            body={"sender_id": "admin", "recipient_id": "peer-1", "body": "welcome peer"},
            headers=auth_headers(token="adm-tok"),
        )
        self.assertEqual(st_msg, 201, msg_resp)

        th.join(timeout=6.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(holder["status"], 200)
        res_mcp = holder["rpc"]["result"]
        self.assertFalse(res_mcp["isError"], res_mcp)
        sc = res_mcp["structuredContent"]
        self.assertFalse(sc["timed_out"])
        self.assertTrue(any(e["event_type"] == "message_sent" for e in sc["events"]), sc)

        st_replay, _, replay_body = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv_token},
        )
        self.assertIn(st_replay, (401, 409), f"Replay unexpectedly returned {st_replay}: {replay_body}")

    def test_p05_agent_token_impersonation_403_across_rest_mcp_and_x_agent_id(self) -> None:
        """[F8 x F7 x F4] Per-agent token enforces 403 across REST headers/body/query, admin routes, and /mcp."""
        hub = self.ws.start_hub(token="adm-tok")
        tok_a = _onboard_peer(hub.base_url, "adm-tok", "agent-a")
        _onboard_peer(hub.base_url, "adm-tok", "agent-b")

        # 1. Mismatched X-Agent-Id header vs own body
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/leases/acquire",
                body={"owner_id": "agent-a", "resource": "src/a.py"},
                headers=auth_headers(token=tok_a, agent_id="agent-b"),
            )[0],
            403,
        )
        # 2. Matching X-Agent-Id header but impersonated sender_id body
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/messages",
                body={"sender_id": "agent-b", "recipient_id": "*", "body": "spoof"},
                headers=auth_headers(token=tok_a, agent_id="agent-a"),
            )[0],
            403,
        )
        # 3. Path/query impersonation
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/agents/agent-b/heartbeat",
                body={},
                headers=auth_headers(token=tok_a, agent_id="agent-a"),
            )[0],
            403,
        )
        self.assertEqual(
            http_json(
                "GET",
                f"{hub.base_url}/messages?agent_id=agent-b",
                headers=auth_headers(token=tok_a, agent_id="agent-a"),
            )[0],
            403,
        )
        # 4. Admin-only routes blocked
        hdrs_a = auth_headers(token=tok_a, agent_id="agent-a")
        self.assertEqual(http_json("GET", f"{hub.base_url}/state/export", headers=hdrs_a)[0], 403)
        self.assertEqual(
            http_json("POST", f"{hub.base_url}/invites", body={"agent_id": "evil"}, headers=hdrs_a)[0],
            403,
        )
        self.assertEqual(http_json("GET", f"{hub.base_url}/invites", headers=hdrs_a)[0], 403)

        # 5. /mcp impersonation via X-Agent-Id, body owner_id, or ?agent=
        self.assertEqual(
            mcp_call(
                hub.base_url,
                "sync_acquire_lease",
                {"resource": "src/m1.py"},
                token=tok_a,
                agent_id="agent-b",
            )[0],
            403,
        )
        self.assertEqual(
            mcp_call(
                hub.base_url,
                "sync_acquire_lease",
                {"resource": "src/m2.py", "owner_id": "agent-b"},
                token=tok_a,
                agent_id="agent-a",
            )[0],
            403,
        )
        self.assertEqual(
            mcp_call(
                hub.base_url,
                "sync_wait",
                {"since_id": 0, "timeout_sec": 0.1},
                token=tok_a,
                agent_id="agent-a",
                query="agent=agent-b",
            )[0],
            403,
        )

        # Contrast: legitimate own-identity call succeeds
        st_ok, rpc_ok = mcp_call(
            hub.base_url,
            "sync_acquire_lease",
            {"resource": "src/own.py"},
            token=tok_a,
            agent_id="agent-a",
            query="agent=agent-a",
        )
        self.assertEqual(st_ok, 200, rpc_ok)
        self.assertFalse(rpc_ok["result"]["isError"], rpc_ok)


class TestPairwiseRemoteTunnelAndNetwork(unittest.TestCase):
    """Pairwise interactions across F7, F9, F12."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_p06_base_url_allowed_host_tls_proxy_headers_and_invite_join_urls(self) -> None:
        """[F9 x F7] --base-url + --allowed-host + TLS Origin/Host 443 + invite join_url & evil Origin 403."""
        hub = self.ws.start_hub(
            token="adm-tok",
            base_url="https://team-hub.trycloudflare.com",
            allowed_hosts=["proxy.internal"],
        )

        tls_hdrs = {
            "Host": "team-hub.trycloudflare.com",
            "Origin": "https://team-hub.trycloudflare.com",
        }
        st_inv, _, inv = http_json(
            "POST",
            f"{hub.base_url}/invites",
            body={"agent_id": "tls-peer"},
            headers={**tls_hdrs, **auth_headers(token="adm-tok")},
        )
        self.assertIn(st_inv, (200, 201), inv)
        self.assertTrue(
            str(inv["join_url"]).startswith("https://team-hub.trycloudflare.com"),
            inv["join_url"],
        )

        st_j, _, joined = http_json(
            "POST",
            f"{hub.base_url}/invites/join",
            body={"invite_token": inv["token"]},
            headers=tls_hdrs,
        )
        self.assertEqual(st_j, 200, joined)
        self.assertTrue(str(joined["server_url"]).startswith("https://team-hub.trycloudflare.com"), joined)
        self.assertTrue(str(joined["mcp_url"]).startswith("https://team-hub.trycloudflare.com"), joined)

        st_int, _, h_int = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={"Host": "proxy.internal", "Origin": "http://proxy.internal"},
        )
        self.assertEqual(st_int, 200, h_int)

        st_evil, _, _ = http_json(
            "GET",
            f"{hub.base_url}/health",
            headers={"Host": "team-hub.trycloudflare.com", "Origin": "https://evil.example.com"},
        )
        self.assertEqual(st_evil, 403)

    def test_p07_proxy_bypass_local_vs_remote_and_doctor_https_port(self) -> None:
        """[F9 x F12] build_opener_for_url bypasses proxy locally while preserving remote proxy & doctor :443."""
        from agsync.adapters.network import build_opener_for_url

        hub = self.ws.start_hub()
        old_http = os.environ.get("HTTP_PROXY")
        old_https = os.environ.get("HTTPS_PROXY")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
        try:
            local_opener = build_opener_for_url(hub.base_url)
            st, _, _ = http_json("GET", f"{hub.base_url}/health", opener=local_opener)
            self.assertEqual(st, 200)

            remote_opener = build_opener_for_url("https://team-hub.trycloudflare.com")
            proxy_handlers = [
                h for h in remote_opener.handlers if isinstance(h, urllib.request.ProxyHandler)
            ]
            self.assertTrue(
                not proxy_handlers or any(h.proxies for h in proxy_handlers),
                "Remote URL opener must preserve proxy config rather than forcing ProxyHandler({})",
            )
        finally:
            if old_http is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old_http
            if old_https is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = old_https

        repo = self.ws.create_git_repo("p07_doc")
        res = self.ws.run_cli(
            "doctor",
            "--repo",
            str(repo),
            "--server",
            "https://127.0.0.1",
            "--json",
        )
        rep = json.loads(res.stdout)
        tcp_chk = next(c for c in rep["checks"] if c["name"] == "hub_tcp")
        self.assertIn(":443", tcp_chk["detail"])
        self.assertNotIn(":8765", tcp_chk["detail"])


class TestPairwiseWorktreeAndMultiIdeHooks(unittest.TestCase):
    """Pairwise interactions across F10, F11, F12, F13, F14, F16."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_p08_worktree_create_with_junctions_identity_and_claude_hook_proxy(self) -> None:
        """[F13 x F10 x F14] worktree create + junctions + .agsync-agent auto-identity + hook-proxy --host claude."""
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("wt_claude")
        (proj / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (proj / "node_modules" / "pkg" / "index.js").write_text("module.exports = 42;\n", encoding="utf-8")
        (proj / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (proj / ".venv" / "Scripts" / "marker.txt").write_text("venv-ok\n", encoding="utf-8")

        _acquire_lease(hub.base_url, "peer-other", "src/locked.py")

        res_wt = self.ws.run_cli(
            "worktree",
            "create",
            "claude-wt",
            "--repo",
            str(proj),
            "--server",
            hub.base_url,
            "--install-hooks",
        )
        self.assertEqual(res_wt.returncode, 0, res_wt.stderr or res_wt.stdout)
        wt1 = self.ws.ws_dir / "wt_claude-wt-1"
        self.assertTrue(wt1.is_dir())
        self.assertEqual((wt1 / ".agsync-agent").read_text(encoding="utf-8").strip(), "claude-wt")
        self.assertTrue((wt1 / "node_modules" / "pkg" / "index.js").is_file())
        self.assertTrue((wt1 / ".venv" / "Scripts" / "marker.txt").is_file())
        if os.name == "nt":
            self.assertTrue(os.path.isjunction(wt1 / "node_modules"))
            self.assertTrue(os.path.isjunction(wt1 / ".venv"))

        # Omit --agent so hook-proxy resolves identity from wt1/.agsync-agent
        deny_in = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(wt1 / "src" / "locked.py")},
                "cwd": str(wt1),
            }
        )
        r_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            stdin_text=deny_in,
            cwd=wt1,
        )
        self.assertEqual(r_deny.returncode, 2, f"Expected exit 2 on deny: {r_deny.stdout} {r_deny.stderr}")
        self.assertIn("peer-other", r_deny.stderr)

        allow_in = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(wt1 / "src" / "free.py")},
                "cwd": str(wt1),
            }
        )
        r_allow = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--server",
            hub.base_url,
            stdin_text=allow_in,
            cwd=wt1,
        )
        self.assertEqual(r_allow.returncode, 0, r_allow.stderr)

        _acquire_lease(hub.base_url, "claude-wt", "src/mine.py")
        own_in = json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(wt1 / "src" / "mine.py")},
                "cwd": str(wt1),
            }
        )
        r_own = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--require-lease",
            "--server",
            hub.base_url,
            stdin_text=own_in,
            cwd=wt1,
        )
        self.assertEqual(r_own.returncode, 0, r_own.stderr)

    def test_p09_multi_worktree_identity_guard_blocks_antigravity_hook_proxy_without_identity(self) -> None:
        """[F14 x F10 x F13] Multi-worktree guard blocks hostname fallback in lock CLI and Antigravity hook-proxy."""
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("wt_guard")
        self.assertEqual(
            self.ws.run_cli("worktree", "create", "ag-wt", "--repo", str(proj)).returncode,
            0,
        )
        wt1 = self.ws.ws_dir / "wt_guard-wt-1"
        _acquire_lease(hub.base_url, "peer-b", "src/locked.py")

        # With .agsync-agent present, deny on peer-b's locked file
        locked_payload = json.dumps(
            {
                "toolCall": {"name": "edit_file", "args": {"Path": str(wt1 / "src" / "locked.py")}},
                "cwd": str(wt1),
            }
        )
        r_locked = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            stdin_text=locked_payload,
            cwd=wt1,
        )
        self.assertEqual(r_locked.returncode, 0, r_locked.stderr)
        self.assertEqual(json.loads(r_locked.stdout)["decision"], "deny")

        # Remove both .agsync-agent and .agsync/agent_id from wt1 and proj
        (wt1 / ".agsync-agent").unlink(missing_ok=True)
        (wt1 / ".agsync" / "agent_id").unlink(missing_ok=True)
        (proj / ".agsync-agent").unlink(missing_ok=True)
        (proj / ".agsync" / "agent_id").unlink(missing_ok=True)

        # CLI lock without explicit identity must fail
        r_lock = self.ws.run_cli("lock", "src/free.py", "--server", hub.base_url, cwd=wt1)
        self.assertNotEqual(r_lock.returncode, 0, "Expected lock CLI to refuse hostname fallback in worktree")
        self.assertRegex((r_lock.stdout + "\n" + r_lock.stderr).lower(), r"\.agsync-agent|identity|ambiguous|agent")

        # Antigravity hook-proxy without identity must refuse silent allow
        for check_dir in (wt1, proj):
            free_payload = json.dumps(
                {
                    "toolCall": {"name": "edit_file", "args": {"Path": str(check_dir / "src" / "free.py")}},
                    "cwd": str(check_dir),
                }
            )
            r_hp = self.ws.run_cli(
                "hook-proxy",
                "--host",
                "antigravity",
                "--server",
                hub.base_url,
                stdin_text=free_payload,
                cwd=check_dir,
            )
            if r_hp.returncode == 0 and r_hp.stdout.strip().startswith("{"):
                out = json.loads(r_hp.stdout)
                self.assertIn(out.get("decision"), ("deny", "ask"), out)
                self.assertRegex(
                    (out.get("reason", "") + "\n" + r_hp.stderr).lower(),
                    r"\.agsync-agent|identity|ambiguous|agent",
                )
            else:
                self.assertNotEqual(r_hp.returncode, 0)
                self.assertRegex((r_hp.stdout + "\n" + r_hp.stderr).lower(), r"\.agsync-agent|identity|ambiguous|agent")

        # Restoring wt1/.agsync-agent restores allow
        (wt1 / ".agsync-agent").write_text("ag-wt\n", encoding="utf-8")
        free_wt1 = json.dumps(
            {
                "toolCall": {"name": "edit_file", "args": {"Path": str(wt1 / "src" / "free.py")}},
                "cwd": str(wt1),
            }
        )
        r_ok = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "antigravity",
            "--server",
            hub.base_url,
            stdin_text=free_wt1,
            cwd=wt1,
        )
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
        self.assertEqual(json.loads(r_ok.stdout)["decision"], "allow")

    def test_p10_worktree_remove_unlinks_junctions_and_restores_single_worktree_identity(self) -> None:
        """[F13 x F14] worktree remove preserves main caches and restores single-repo default identity."""
        hub = self.ws.start_hub()
        proj = self.ws.create_git_repo("wt_rm_guard")
        (proj / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (proj / "node_modules" / "pkg" / "index.js").write_text("keep-cache\n", encoding="utf-8")
        (proj / ".venv").mkdir(parents=True, exist_ok=True)
        (proj / ".venv" / "marker.txt").write_text("keep-venv\n", encoding="utf-8")
        (proj / ".agsync-agent").unlink(missing_ok=True)
        (proj / ".agsync" / "agent_id").unlink(missing_ok=True)

        self.assertEqual(
            self.ws.run_cli("worktree", "create", "wt-temp", "--repo", str(proj)).returncode,
            0,
        )
        wt1 = self.ws.ws_dir / "wt_rm_guard-wt-1"
        self.assertTrue(wt1.is_dir())

        r_blocked = self.ws.run_cli("check", "src/free.py", "--server", hub.base_url, cwd=proj)
        self.assertNotEqual(r_blocked.returncode, 0, "Multi-worktree main repo without .agsync-agent must fail check")

        r_rm = self.ws.run_cli("worktree", "remove", "wt-temp", "--repo", str(proj))
        self.assertEqual(r_rm.returncode, 0, r_rm.stderr or r_rm.stdout)
        self.assertFalse(wt1.exists())
        self.assertEqual((proj / "node_modules" / "pkg" / "index.js").read_text(encoding="utf-8"), "keep-cache\n")
        self.assertEqual((proj / ".venv" / "marker.txt").read_text(encoding="utf-8"), "keep-venv\n")

        r_restored = self.ws.run_cli("check", "src/free.py", "--server", hub.base_url, cwd=proj)
        self.assertEqual(r_restored.returncode, 0, r_restored.stderr or r_restored.stdout)

    def test_p11_cursor_claude_dedup_with_record_writes_sanitized_sample(self) -> None:
        """[F10 x F11 x F12] Cursor + .claude/ dedup prevents double chained hook run while --record redacts secrets."""
        hub = self.ws.start_hub(token="secret-tok-123")
        repo = self.ws.create_git_repo("cursor_rec_dedup")
        chain_log = repo / "chain_runs.txt"
        hook_script = repo / "chain_hook.py"
        hook_script.write_text(
            f"from pathlib import Path\nwith Path({str(chain_log)!r}).open('a', encoding='utf-8') as f:\n    f.write('ran\\n')\n",
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
                                "hooks": [{"type": "command", "command": f'"{sys.executable}" "{hook_script}"'}],
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
            "agent-a",
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
                "authorization": "Bearer secret-tok-123",
            }
        )
        cl_payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
                "cursor_version": "0.50.0",
                "authorization": "Bearer secret-tok-123",
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
            "agent-a",
            "--token",
            "secret-tok-123",
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
            "agent-a",
            "--token",
            "secret-tok-123",
            stdin_text=cl_payload,
            cwd=repo,
        )
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)

        dedup_dir = repo / ".agsync" / "hook-dedup"
        self.assertTrue(dedup_dir.is_dir() and any(dedup_dir.iterdir()), "Missing hook-dedup marker")
        if chain_log.exists():
            lines = [ln for ln in chain_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1, f"Chained hook ran {len(lines)} times instead of 1")

        samples = list((repo / ".agsync" / "hook-samples").glob("*.json"))
        self.assertGreaterEqual(len(samples), 1)
        for sample in samples:
            raw = sample.read_text(encoding="utf-8")
            self.assertNotIn("secret-tok-123", raw, f"Token leaked in {sample}")
            parsed = json.loads(raw)
            self.assertIn("decision", parsed)

    def test_p12_claude_hook_proxy_chains_user_hook_and_kills_process_tree_on_timeout(self) -> None:
        """[F10 x F11] Claude hook-proxy chains user hook, kills process tree on --chain-timeout, and preserves deny."""
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("claude_tree_kill")
        pids_file = repo / "pids.json"
        hang_script = repo / "hang_tree.py"
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
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [{"type": "command", "command": f'"{sys.executable}" "{hang_script}"'}],
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
            "agent-a",
            "--with-claude-hook",
        )
        self.assertEqual(r_inst.returncode, 0, r_inst.stderr or r_inst.stdout)

        free_in = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "free.py")},
                "cwd": str(repo),
            }
        )
        t0 = time.monotonic()
        r_free = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--chain-timeout",
            "0.5",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=free_in,
            cwd=repo,
            timeout=15.0,
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 6.0, f"Claude hook-proxy hung for {elapsed:.2f}s")
        self.assertTrue(pids_file.is_file(), "Chained hook never spawned")
        pids = json.loads(pids_file.read_text(encoding="utf-8"))
        self.assertEqual(len(pids), 2)
        time.sleep(0.3)
        for pid in pids:
            self.assertFalse(is_pid_alive(int(pid)), f"Orphan PID {pid} survived tree kill")

        _acquire_lease(hub.base_url, "peer-b", "src/locked.py")
        deny_in = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(repo / "src" / "locked.py")},
                "cwd": str(repo),
            }
        )
        r_deny = self.ws.run_cli(
            "hook-proxy",
            "--host",
            "claude",
            "--chain-timeout",
            "0.5",
            "--server",
            hub.base_url,
            "--agent",
            "agent-a",
            stdin_text=deny_in,
            cwd=repo,
            timeout=15.0,
        )
        self.assertEqual(r_deny.returncode, 2)
        self.assertIn("peer-b", r_deny.stderr)

    def test_p13_setup_mcp_all_install_all_hooks_and_doctor_repo_integration(self) -> None:
        """[F12 x F10 x F14 x F16] setup-mcp --ide all + install-hooks --with-all-hooks --with-templates + doctor."""
        hub = self.ws.start_hub()
        repo = self.ws.create_git_repo("all_ide_doc")

        ag_cfg = self.ws.home_dir / ".gemini" / "config" / "mcp_config.json"
        claude_cfg = repo / ".mcp.json"
        cursor_cfg = self.ws.home_dir / ".cursor" / "mcp.json"
        for p in (ag_cfg, claude_cfg, cursor_cfg):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"mcpServers": {"other-mcp": {"command": "echo"}}}), encoding="utf-8")

        r_mcp = self.ws.run_cli(
            "setup-mcp",
            "--ide",
            "all",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "ide-agent",
            cwd=repo,
        )
        self.assertEqual(r_mcp.returncode, 0, r_mcp.stderr or r_mcp.stdout)

        for p in (ag_cfg, claude_cfg, cursor_cfg):
            raw = p.read_text(encoding="utf-8")
            self.assertNotIn("/sse", raw)
            parsed = json.loads(raw)
            self.assertIn("other-mcp", parsed["mcpServers"])
            self.assertIn("agsync", parsed["mcpServers"])
            self.assertIn("/mcp?agent=ide-agent", json.dumps(parsed["mcpServers"]["agsync"]))
            self.assertTrue((p.parent / f"{p.name}.bak").is_file(), f"Missing .bak for {p}")

        r_hooks = self.ws.run_cli(
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--agent",
            "ide-agent",
            "--with-all-hooks",
            "--with-templates",
        )
        self.assertEqual(r_hooks.returncode, 0, r_hooks.stderr or r_hooks.stdout)

        for rel in (
            ".git/hooks/pre-commit",
            ".agents/hooks.json",
            ".claude/settings.json",
            ".cursor/hooks.json",
            "AGENTS.md",
        ):
            self.assertTrue((repo / rel).is_file(), f"Missing {rel}")

        agents_md = (repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("sync_wait", agents_md)
        self.assertIn(".agsync-agent", agents_md)

        skills = list((repo / ".agents" / "skills").glob("*/SKILL.md"))
        self.assertGreaterEqual(len(skills), 3)
        for sk in skills:
            txt = sk.read_text(encoding="utf-8")
            self.assertTrue(txt.startswith("---"), f"{sk} missing YAML frontmatter")
            self.assertIn("name:", txt)
            self.assertIn("description:", txt)

        self.assertIn(".agsync", (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8"))

        r_doc = self.ws.run_cli(
            "doctor",
            "--repo",
            str(repo),
            "--server",
            hub.base_url,
            "--json",
            cwd=repo,
        )
        self.assertEqual(r_doc.returncode, 0, r_doc.stdout or r_doc.stderr)
        rep = json.loads(r_doc.stdout)
        self.assertTrue(rep["ok"], rep)


class TestPairwiseDashboardControlPanelAndScale(unittest.TestCase):
    """Pairwise interactions across F3, F4, F5, F6, F7, F8, F9, F13, F15."""

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_p14_events_long_poll_streams_xss_safe_events_with_dashboard_contract(self) -> None:
        """[F5 x F6 x F3] /dashboard contract + XSS payloads streamed safely through GET /events."""
        hub = self.ws.start_hub()
        st_d, hdrs_d, raw_d = http_request("GET", f"{hub.base_url}/dashboard")
        self.assertEqual(st_d, 200)
        self.assertIn("text/html", hdrs_d.get("content-type", ""))
        self.assertEqual(hdrs_d.get("x-content-type-options", "").lower(), "nosniff")
        self.assertEqual(hdrs_d.get("cache-control", "").lower(), "no-store")
        html = raw_d.decode("utf-8", errors="replace")
        for needle in ("/events", "since_id", "boot_id", "esc(", "renderFeed", "Live Activity Feed"):
            self.assertIn(needle, html)
        self.assertRegex(html, r"esc\(ev\.summary|esc\(t\.title")

        _, _, ev0 = http_json("GET", f"{hub.base_url}/events?since_id=0&wait=0")
        boot_id = ev0["boot_id"]
        latest_id = int(ev0["latest_id"])

        holder: dict[str, Any] = {}

        def _watch() -> None:
            t0 = time.monotonic()
            st, _, body = http_json(
                "GET",
                f"{hub.base_url}/events?since_id={latest_id}&boot_id={boot_id}&wait=10",
                timeout=20.0,
            )
            holder["elapsed"] = time.monotonic() - t0
            holder["status"] = st
            holder["body"] = body

        th = threading.Thread(target=_watch, daemon=True)
        th.start()
        time.sleep(0.25)

        xss_msg = "<img src=x onerror=alert(1)>"
        xss_task = "<script>alert('xss')</script>"
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/messages",
                body={"sender_id": "xss-tester", "recipient_id": "*", "body": xss_msg},
            )[0],
            201,
        )
        self.assertEqual(
            http_json(
                "POST",
                f"{hub.base_url}/tasks",
                body={"title": xss_task, "created_by": "xss-tester"},
            )[0],
            201,
        )

        th.join(timeout=6.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(holder["status"], 200)
        self.assertLess(holder["elapsed"], 3.0)
        events = list(holder["body"]["events"])
        cursor = int(holder["body"]["latest_id"])
        if len(events) < 2:
            events.extend(drain_http_events(hub.base_url, since_id=cursor))
        types = {e["event_type"] for e in events}
        self.assertIn("message_sent", types)
        self.assertIn("task_created", types)
        self.assertTrue(
            any(
                xss_task in e.get("summary", "") or xss_task in json.dumps(e.get("payload", {}))
                for e in events
            ),
            events,
        )

    def test_p15_control_panel_phase2_worktree_invite_join_and_base_url_verified_on_hub(self) -> None:
        """[F15 x F13 x F7 x F9 x F2] AgsyncControlPanel drives base_url, worktree, and invite/join verified on hub & SQLite."""
        hub = self.ws.start_hub(token="adm-tok")
        proj = self.ws.create_git_repo("panel_proj")
        wt_path = self.ws.ws_dir / "panel_proj-wt-1"

        driver = self.ws.root / "p15_tk_driver.py"
        script = _tk_driver_preamble() + f"""
import json, time
from pathlib import Path
try:
    from agsync.config import load_config
    from scripts import control_panel as cp_mod
    if not _HEADLESS_TK:
        _install_modal_neutralizers(cp_mod.messagebox, cp_mod.simpledialog, cp_mod.filedialog)
    panel = cp_mod.AgsyncControlPanel(root)
    panel.host_ip_var.set("127.0.0.1")
    panel.port_var.set({str(hub.port)!r})
    panel.token_var.set("adm-tok")
    panel.repo_var.set({str(proj)!r})
    base_attr = next((a for a in ("base_url_var", "remote_url_var", "public_url_var") if hasattr(panel, a)), None)
    assert base_attr is not None, "Missing base_url StringVar on AgsyncControlPanel"
    getattr(panel, base_attr).set("https://panel-e2e.trycloudflare.com")
    panel._save_current_config()

    for attr in ("wt_agent_var", "worktree_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-peer")
    create_fn = next((getattr(panel, n) for n in ("create_worktree", "worktree_create", "on_create_worktree") if hasattr(panel, n)), None)
    assert create_fn is not None, "Missing worktree create handler"
    create_fn()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not Path({str(wt_path)!r}).is_dir():
        root.update()
        time.sleep(0.05)
    assert Path({str(wt_path)!r}).is_dir(), "GUI did not create worktree"

    panel.repo_var.set({str(wt_path)!r})
    for attr in ("invite_agent_var", "peer_agent_var"):
        if hasattr(panel, attr):
            getattr(panel, attr).set("gui-peer")
    inv_fn = next((getattr(panel, n) for n in ("create_invite", "generate_invite", "on_create_invite") if hasattr(panel, n)), None)
    join_fn = next((getattr(panel, n) for n in ("join_invite", "join_hub", "on_join_invite") if hasattr(panel, n)), None)
    assert inv_fn and join_fn, "Missing invite/join handlers"
    inv_fn()
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
    assert inv_tok, "Create invite did not populate invite token/link var"

    join_fn()
    deadline = time.monotonic() + 6.0
    wt_tok_file = Path({str(wt_path)!r}) / ".agsync" / "token"
    while time.monotonic() < deadline and not wt_tok_file.is_file():
        root.update()
        time.sleep(0.05)
    assert wt_tok_file.is_file(), "GUI join did not write .agsync/token in worktree"

    print(json.dumps({{
        "headless": _HEADLESS_TK,
        "reloaded": load_config(),
        "wt_token": wt_tok_file.read_text(encoding="utf-8").strip(),
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
            timeout=25.0,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"GUI driver failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        out = json.loads(res.stdout)
        self.assertIn("https://panel-e2e.trycloudflare.com", json.dumps(out["reloaded"]))

        wt_tok = out["wt_token"]
        _register_agent(hub.base_url, "gui-peer", token=wt_tok)

        st_inv, _, listed = http_json(
            "GET", f"{hub.base_url}/invites", headers=auth_headers(token="adm-tok")
        )
        self.assertEqual(st_inv, 200)
        items = listed.get("invites", listed) if isinstance(listed, dict) else listed
        self.assertTrue(any(i["agent_id"] == "gui-peer" and bool(i["used"]) for i in items), items)

        hub.stop()
        tok_rows = inspect_sqlite(hub.db_path, "SELECT * FROM agent_tokens WHERE agent_id = ?;", ("gui-peer",))
        inv_rows = inspect_sqlite(hub.db_path, "SELECT * FROM invites WHERE agent_id = ?;", ("gui-peer",))
        self.assertEqual(len(tok_rows), 1)
        self.assertEqual(len(inv_rows), 1)
        self.assertEqual(inv_rows[0]["used"], 1)

    def test_p16_events_ring_buffer_trim_preserves_sync_wait_and_get_events_cursor_continuity(self) -> None:
        """[F3 x F4 x F5] 1060-event burst trims to 1000 rows while preserving live cursor & sync_wait wakeup."""
        hub = self.ws.start_hub()
        statuses = send_persistent_put_batch("127.0.0.1", int(hub.port), 1060, key_prefix="p16_")
        self.assertTrue(all(s == 200 for s in statuses))

        st_old, _, old_ev = http_json("GET", f"{hub.base_url}/events?since_id=10&wait=0")
        self.assertEqual(st_old, 200)
        self.assertTrue(old_ev["reset"], "Trimmed cursor since_id=10 should trigger reset=True")
        latest_id = int(old_ev["latest_id"])
        self.assertGreaterEqual(latest_id, 1060)

        st_tail, _, tail = http_json(
            "GET",
            f"{hub.base_url}/events?since_id={latest_id - 5}&wait=0&limit=50",
        )
        self.assertEqual(st_tail, 200)
        self.assertFalse(tail["reset"])
        self.assertEqual(len(tail["events"]), 5)

        holder: dict[str, Any] = {}

        def _waiter() -> None:
            t0 = time.monotonic()
            st, rpc = mcp_call(
                hub.base_url,
                "sync_wait",
                {"since_id": latest_id, "timeout_sec": 10},
                timeout=20.0,
            )
            holder["elapsed"] = time.monotonic() - t0
            holder["status"] = st
            holder["rpc"] = rpc

        th = threading.Thread(target=_waiter, daemon=True)
        th.start()
        time.sleep(0.25)
        st_more = send_persistent_put_batch("127.0.0.1", int(hub.port), 5, key_prefix="p16_tail_")
        self.assertTrue(all(s == 200 for s in st_more))

        th.join(timeout=6.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(holder["status"], 200)
        self.assertLess(holder["elapsed"], 3.0)
        sc = holder["rpc"]["result"]["structuredContent"]
        self.assertFalse(sc["reset"])
        self.assertFalse(sc["timed_out"])

        collected = list(sc["events"])
        if len(collected) < 5:
            collected.extend(drain_http_events(hub.base_url, since_id=int(sc["latest_id"])))
        self.assertGreaterEqual(len(collected), 5)
        ids = [int(e["id"]) for e in collected[:5]]
        self.assertEqual(ids, list(range(latest_id + 1, latest_id + 6)))

        hub.stop()
        cnt = inspect_sqlite(hub.db_path, "SELECT COUNT(*) AS cnt FROM events;")
        self.assertEqual(cnt[0]["cnt"], 1000)

    def test_p17_stdio_mcp_bridge_sync_wait_long_timeout_with_scoped_agent_token(self) -> None:
        """[F4 x F8] Stdio mcp_bridge --timeout 2.0 survives sync_wait(6.0s) and surfaces scoped 403 impersonation."""
        hub = self.ws.start_hub(token="adm-tok")
        peer_tok = _onboard_peer(hub.base_url, "adm-tok", "bridge-peer")
        _, _, ev0 = http_json(
            "GET",
            f"{hub.base_url}/events?since_id=0&wait=0",
            headers=auth_headers(token=peer_tok, agent_id="bridge-peer"),
        )
        latest_id = int(ev0["latest_id"])

        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "agsync.mcp_bridge",
                "--server",
                hub.base_url,
                "--agent",
                "bridge-peer",
                "--token",
                peer_tok,
                "--timeout",
                "2.0",
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

        def _send_and_recv(req: dict[str, Any], wait_timeout: float = 15.0) -> dict[str, Any]:
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            box: dict[str, Any] = {}

            def _read() -> None:
                line = proc.stdout.readline()
                box["line"] = line

            rt = threading.Thread(target=_read, daemon=True)
            rt.start()
            rt.join(timeout=wait_timeout)
            self.assertFalse(rt.is_alive(), f"mcp_bridge response timed out for req {req}")
            raw = box.get("line", "")
            self.assertTrue(raw.strip(), "mcp_bridge closed stdout unexpectedly")
            return json.loads(raw)

        init_resp = _send_and_recv(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            }
        )
        self.assertIn("result", init_resp)

        wait_resp = _send_and_recv(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "sync_wait",
                    "arguments": {"since_id": latest_id, "timeout_sec": 6.0},
                },
            },
            wait_timeout=18.0,
        )
        wait_res = wait_resp["result"]
        self.assertFalse(wait_res["isError"], f"sync_wait failed under --timeout 2.0: {wait_res}")
        self.assertTrue(wait_res["structuredContent"]["timed_out"])

        imp_resp = _send_and_recv(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sync_send_message",
                    "arguments": {"sender_id": "other-agent", "recipient_id": "*", "body": "spoof"},
                },
            }
        )
        imp_res = imp_resp["result"]
        self.assertTrue(imp_res["isError"], imp_res)
        self.assertEqual(imp_res["structuredContent"].get("status_code"), 403, imp_res)

        proc.stdin.close()
        rc = proc.wait(timeout=8.0)
        stderr_out = proc.stderr.read() if proc.stderr is not None else ""
        proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        self.assertEqual(rc, 0, stderr_out)


if __name__ == "__main__":
    unittest.main()
