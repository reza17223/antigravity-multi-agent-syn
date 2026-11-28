"""Unified XAMPP-style Control Panel for agsync (Host/Peer switch, MCP & Repo setup, Diagnostics)."""

from __future__ import annotations

import argparse
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
import urllib.error
import urllib.request
import webbrowser

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agsync import __version__

from agsync.adapters.network import (
    WindowsJobObject,
    build_opener_for_url,
    inspect_pid_lockfile,
    inspect_windows_network_profiles,
    tcp_check,
    validate_port,
)
from agsync.client import SyncClient, SyncClientError
from agsync.commands.clone import clone_from_host, pull_latest_changes
from agsync.commands.doctor import run_doctor
from agsync.config import (
    configure_antigravity_mcp,
    default_agent_id,
    detect_lan_ip,
    load_config,
    save_config,
)
from agsync.db import default_data_dir
from agsync.discovery import DEFAULT_DISCOVERY_PORT, discover_hubs

MAX_LOG_LINES = 1000


def check_health(url: str, timeout: float = 2.5) -> tuple[bool, str]:
    """Check /health endpoint bypassing system proxies for local/LAN URLs."""
    target = f"{url.rstrip('/')}/health"
    try:
        opener = build_opener_for_url(target)
        req = urllib.request.Request(target, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


def setup_universal_clipboard(root: tk.Tk) -> None:
    """Enable universal Copy/Paste/Cut/SelectAll across all widgets, layouts (Persian/English), and right-click context menu."""
    for keysym in ("v", "V", "Arabic_ra"):
        try:
            root.event_add("<<Paste>>", f"<Control-Key-{keysym}>")
        except tk.TclError:
            pass
    for keysym in ("c", "C", "Arabic_zain"):
        try:
            root.event_add("<<Copy>>", f"<Control-Key-{keysym}>")
        except tk.TclError:
            pass
    for keysym in ("x", "X", "Arabic_tah"):
        try:
            root.event_add("<<Cut>>", f"<Control-Key-{keysym}>")
        except tk.TclError:
            pass
    for keysym in ("a", "A", "Arabic_sheen"):
        try:
            root.event_add("<<SelectAll>>", f"<Control-Key-{keysym}>")
        except tk.TclError:
            pass
    for keysym in ("z", "Z", "Arabic_zah"):
        try:
            root.event_add("<<Undo>>", f"<Control-Key-{keysym}>")
        except tk.TclError:
            pass

    def _do_paste(w: tk.Widget) -> None:
        try:
            text = root.clipboard_get()
        except tk.TclError:
            return
        if not text:
            return
        if isinstance(w, (tk.Entry, ttk.Entry)):
            try:
                sel_start = w.index("sel.first")
                w.delete("sel.first", "sel.last")
                w.insert(sel_start, text)
                w.icursor(sel_start + len(text))
            except tk.TclError:
                w.insert(tk.INSERT, text)
        elif isinstance(w, tk.Text):
            try:
                sel_start = w.index("sel.first")
                w.delete("sel.first", "sel.last")
                w.insert(sel_start, text)
                w.mark_set("insert", f"{sel_start}+{len(text)}c")
            except tk.TclError:
                w.insert(tk.INSERT, text)

    def _do_copy(w: tk.Widget) -> None:
        try:
            text = ""
            if isinstance(w, (tk.Entry, ttk.Entry)):
                text = w.selection_get()
            elif isinstance(w, tk.Text):
                text = w.get("sel.first", "sel.last")
            if text:
                root.clipboard_clear()
                root.clipboard_append(text)
                root.update()
        except tk.TclError:
            pass

    def _do_cut(w: tk.Widget) -> None:
        _do_copy(w)
        if isinstance(w, (tk.Entry, ttk.Entry)):
            try:
                w.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
        elif isinstance(w, tk.Text):
            try:
                w.delete("sel.first", "sel.last")
            except tk.TclError:
                pass

    def _do_select_all(w: tk.Widget) -> None:
        if isinstance(w, (tk.Entry, ttk.Entry)):
            w.selection_range(0, tk.END)
            w.icursor(tk.END)
        elif isinstance(w, tk.Text):
            w.tag_add("sel", "1.0", "end")

    def _do_clear(w: tk.Widget) -> None:
        if isinstance(w, (tk.Entry, ttk.Entry)):
            w.delete(0, tk.END)
        elif isinstance(w, tk.Text):
            w.delete("1.0", tk.END)

    def _on_ctrl_keypress(event: tk.Event) -> str | None:
        is_ctrl = bool(event.state & 4) or (sys.platform == "darwin" and bool(event.state & 8))
        if not is_ctrl:
            return None
        w = event.widget
        if not isinstance(w, (tk.Entry, ttk.Entry, tk.Text)):
            return None

        ks = (event.keysym or "").lower()
        kc = event.keycode

        if kc == 86 or ks in ("v", "arabic_ra"):
            _do_paste(w)
            return "break"
        elif kc == 67 or ks in ("c", "arabic_zain"):
            _do_copy(w)
            return "break"
        elif kc == 88 or ks in ("x", "arabic_tah"):
            _do_cut(w)
            return "break"
        elif kc == 65 or ks in ("a", "arabic_sheen"):
            _do_select_all(w)
            return "break"
        return None

    ctx_menu = tk.Menu(root, tearoff=0)

    def _show_context_menu(event: tk.Event) -> None:
        w = event.widget
        w.focus_set()
        ctx_menu.delete(0, tk.END)

        if isinstance(w, (tk.Entry, ttk.Entry)):
            if not w.selection_present():
                w.icursor(f"@{event.x}")
            has_sel = bool(w.selection_present())
            is_readonly = str(w.cget("state")) in ("readonly", "disabled")
        elif isinstance(w, tk.Text):
            if not w.tag_ranges("sel"):
                w.mark_set("insert", f"@{event.x},{event.y}")
            has_sel = bool(w.tag_ranges("sel"))
            is_readonly = str(w.cget("state")) in ("disabled",)
        else:
            has_sel = False
            is_readonly = False

        try:
            has_clip = bool(root.clipboard_get())
        except tk.TclError:
            has_clip = False

        state_sel = tk.NORMAL if (has_sel and not is_readonly) else tk.DISABLED
        state_copy = tk.NORMAL if has_sel else tk.DISABLED
        state_paste = tk.NORMAL if (has_clip and not is_readonly) else tk.DISABLED

        ctx_menu.add_command(label="Cut   (Ctrl+X)", state=state_sel, command=lambda: _do_cut(w))
        ctx_menu.add_command(label="Copy  (Ctrl+C)", state=state_copy, command=lambda: _do_copy(w))
        ctx_menu.add_command(label="Paste (Ctrl+V)", state=state_paste, command=lambda: _do_paste(w))
        ctx_menu.add_separator()
        ctx_menu.add_command(label="Select All (Ctrl+A)", command=lambda: _do_select_all(w))
        if not is_readonly:
            ctx_menu.add_command(label="Clear", command=lambda: _do_clear(w))

        try:
            ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            ctx_menu.grab_release()

    for cls in ("Entry", "TEntry", "Text"):
        root.bind_class(cls, "<Button-3>", _show_context_menu)
        root.bind_class(cls, "<KeyPress>", _on_ctrl_keypress, add="+")
        root.bind_class(cls, "<<SelectAll>>", lambda e: _do_select_all(e.widget))


class AgsyncControlPanel:
    def __init__(self, root: tk.Tk, initial_mode: str | None = None) -> None:
        self.root = root
        self.cfg = load_config()
        if initial_mode in ("host", "peer"):
            self.cfg["mode"] = initial_mode

        self.root.title(f"agsync Unified Control Panel v{__version__}")
        self.root.geometry("860x700")
        self.root.minsize(760, 600)

        self.server_proc: subprocess.Popen[bytes] | None = None
        self._server_log_fh = None
        self._tail_stop = threading.Event()
        self._job = WindowsJobObject()
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        setup_universal_clipboard(self.root)
        self._apply_mode_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_logs()
        self._start_auto_sync_listener()

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}

        # Header + Role Switch
        header = ttk.Frame(self.root)
        header.pack(fill="x", **pad)

        ttk.Label(header, text=f"agsync Control Panel v{__version__}", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Button(header, text="🚀 Check for Updates", command=self.check_updates_ui).pack(side="left", padx=16)

        self.status_lbl = tk.Label(
            header,
            text="IDLE",
            bg="#6c757d",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=4,
        )
        self.status_lbl.pack(side="right")

        role_box = ttk.Frame(header)
        role_box.pack(side="right", padx=12)
        ttk.Label(role_box, text="Role:").pack(side="left", padx=(0, 4))
        self.mode_var = tk.StringVar(value=str(self.cfg.get("mode", "host")))
        ttk.Radiobutton(
            role_box, text="HOST (Hub)", value="host", variable=self.mode_var, command=self._on_mode_switch
        ).pack(side="left", padx=4)
        ttk.Radiobutton(
            role_box, text="PEER (Client)", value="peer", variable=self.mode_var, command=self._on_mode_switch
        ).pack(side="left", padx=4)

        # 1. Identity & Network Config
        net_frame = ttk.LabelFrame(self.root, text="1. Identity & Hub Network")
        net_frame.pack(fill="x", **pad)

        ttk.Label(net_frame, text="Agent ID:").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self.agent_var = tk.StringVar(value=str(self.cfg.get("agent_id") or default_agent_id()))
        ttk.Entry(net_frame, textvariable=self.agent_var, width=18).grid(row=0, column=1, padx=4, pady=6)

        ttk.Label(net_frame, text="Host LAN IP:").grid(row=0, column=2, padx=6, pady=6, sticky="w")
        self.host_ip_var = tk.StringVar(value=str(self.cfg.get("host_ip") or detect_lan_ip()))
        ttk.Entry(net_frame, textvariable=self.host_ip_var, width=18).grid(row=0, column=3, padx=4, pady=6)

        ttk.Label(net_frame, text="Port:").grid(row=0, column=4, padx=6, pady=6, sticky="w")
        self.port_var = tk.StringVar(value=str(self.cfg.get("port", 8765)))
        ttk.Entry(net_frame, textvariable=self.port_var, width=7).grid(row=0, column=5, padx=4, pady=6)

        ttk.Label(net_frame, text="Token:").grid(row=1, column=0, padx=6, pady=6, sticky="w")
        self.token_var = tk.StringVar(value=str(self.cfg.get("token", "")))
        ttk.Entry(net_frame, textvariable=self.token_var, width=18, show="*").grid(row=1, column=1, padx=4, pady=6)

        net_btns = ttk.Frame(net_frame)
        net_btns.grid(row=1, column=2, columnspan=4, padx=4, pady=6, sticky="w")
        ttk.Button(net_btns, text="Detect My IP", command=self._refresh_ip).pack(side="left", padx=3)
        ttk.Button(net_btns, text="Discover LAN Hub", command=self.discover_lan).pack(side="left", padx=3)
        ttk.Button(net_btns, text="Generate Token", command=self._generate_token).pack(side="left", padx=3)
        ttk.Button(net_btns, text="Copy Token", command=self._copy_token).pack(side="left", padx=3)
        ttk.Button(net_btns, text="📋 Paste Token", command=self._paste_token).pack(side="left", padx=3)
        ttk.Button(net_btns, text="Save Config", command=self._save_current_config).pack(side="left", padx=3)

        # 2. Hub & Diagnostics Controls
        ctrl_frame = ttk.LabelFrame(self.root, text="2. Hub Controls, Handoff & Diagnostics")
        ctrl_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(ctrl_frame)
        row1.pack(fill="x", padx=6, pady=4)

        self.start_btn = ttk.Button(row1, text="Start Hub Server", command=self.start_server)
        self.start_btn.pack(side="left", padx=4)

        self.stop_btn = ttk.Button(row1, text="Stop Hub Server", command=self.stop_server, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        self.fw_btn = ttk.Button(row1, text="Open Firewall (Admin)", command=self.open_firewall)
        self.fw_btn.pack(side="left", padx=4)

        self.pull_btn = ttk.Button(row1, text="Pull State from Old Host...", command=self.pull_state_dialog)
        self.pull_btn.pack(side="left", padx=4)

        row2 = ttk.Frame(ctrl_frame)
        row2.pack(fill="x", padx=6, pady=4)

        ttk.Button(row2, text="Test Connection (TCP + /health)", command=self.run_diagnostics).pack(
            side="left", padx=4
        )
        ttk.Button(row2, text="Run Doctor", command=self.run_doctor_ui).pack(side="left", padx=4)
        ttk.Button(row2, text="Check Live Agents & Locks", command=self.check_status).pack(side="left", padx=4)
        ttk.Button(row2, text="📨 Inbox / پیام‌ها", command=self.check_inbox_ui).pack(side="left", padx=4)
        ttk.Button(row2, text="💬 Send Message...", command=self.send_message_dialog).pack(side="left", padx=4)
        ttk.Button(row2, text="🌐 Open Web Dashboard (داشبورد زنده)", command=self.open_dashboard).pack(side="left", padx=4)

        self.discover_chk_var = tk.BooleanVar(value=bool(self.cfg.get("discover", True)))
        ttk.Checkbutton(row2, text="Broadcast LAN Discovery", variable=self.discover_chk_var).pack(
            side="right", padx=6
        )

        # 3. Antigravity & Project Integration
        ag_frame = ttk.LabelFrame(self.root, text="3. Antigravity & Project Repo Integration (1-Click Setup)")
        ag_frame.pack(fill="x", **pad)

        r_repo = ttk.Frame(ag_frame)
        r_repo.pack(fill="x", padx=6, pady=4)
        ttk.Label(r_repo, text="Project Repo:").pack(side="left", padx=(0, 6))
        self.repo_var = tk.StringVar(value=str(self.cfg.get("repo_path", "")))
        ttk.Entry(r_repo, textvariable=self.repo_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r_repo, text="Browse...", command=self._browse_repo).pack(side="left", padx=4)
        ttk.Button(r_repo, text="📋 Paste Path", command=self._paste_repo_path).pack(side="left", padx=4)

        r_setup = ttk.Frame(ag_frame)
        r_setup.pack(fill="x", padx=6, pady=4)
        ttk.Label(r_setup, text="MCP Mode:").pack(side="left", padx=(0, 6))
        self.mcp_mode_var = tk.StringVar(value=str(self.cfg.get("mcp_mode", "http")))
        ttk.Combobox(
            r_setup, textvariable=self.mcp_mode_var, values=["http", "stdio"], width=8, state="readonly"
        ).pack(side="left", padx=4)
        ttk.Button(r_setup, text="Connect Antigravity MCP", command=self.connect_mcp).pack(side="left", padx=6)
        self.attach_btn = ttk.Button(
            r_setup, text="Install Repo Hooks & Templates", command=self.attach_repo
        )
        self.attach_btn.pack(side="left", padx=4)
        ttk.Button(
            r_setup, text="🛡️ Ensure Safe .gitignore", command=self.ensure_gitignore_ui
        ).pack(side="left", padx=4)

        r_sync = ttk.Frame(ag_frame)
        r_sync.pack(fill="x", padx=6, pady=4)
        ttk.Label(r_sync, text="Team Sync:").pack(side="left", padx=(0, 6))

        self.git_daemon_proc: subprocess.Popen | None = None
        self.git_daemon_btn = ttk.Button(
            r_sync, text="📡 Share Git on LAN", command=self.toggle_git_daemon_ui
        )
        self.git_daemon_btn.pack(side="left", padx=4)

        self.sync_btn = ttk.Button(
            r_sync, text="🔄 Sync / Pull", command=self.pull_lan_git_ui
        )
        self.sync_btn.pack(side="left", padx=4)
        self.clone_btn = self.sync_btn
        self.pull_lan_git_btn = self.sync_btn

        self.auto_sync_var = tk.BooleanVar(value=True)
        self.pending_sync = False
        ttk.Checkbutton(
            r_sync, text="Auto-Sync on Peer Done", variable=self.auto_sync_var
        ).pack(side="left", padx=8)

        r_p2 = ttk.Frame(ag_frame)
        r_p2.pack(fill="x", padx=6, pady=4)
        ttk.Label(r_p2, text="Phase 2:").pack(side="left", padx=(0, 6))
        ttk.Button(r_p2, text="Create Worktree...", command=self.create_worktree_ui).pack(side="left", padx=4)
        ttk.Button(r_p2, text="Create Join Link...", command=self.create_invite_ui).pack(side="left", padx=4)
        ttk.Button(r_p2, text="Join Remote Hub...", command=self.join_remote_ui).pack(side="left", padx=4)
        ttk.Button(r_p2, text="Cloudflare Tunnel", command=self.start_tunnel_ui).pack(side="left", padx=4)
        ttk.Button(r_p2, text="Merge Check", command=self.merge_check_ui).pack(side="left", padx=4)

        # 4. Live Logs Console
        log_frame = ttk.LabelFrame(self.root, text="Live Logs & Step-by-Step Error Diagnostics")
        log_frame.pack(fill="both", expand=True, **pad)

        log_top = ttk.Frame(log_frame)
        log_top.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Button(log_top, text="Clear Logs", command=self.clear_logs).pack(side="right")
        self.auto_scroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_top, text="Auto-Scroll", variable=self.auto_scroll_var).pack(side="right", padx=6)

        self.log_box = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
        )
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)
        self.log(f"Unified Panel ready. Data dir: {default_data_dir()}")

    def _validated_port(self) -> int | None:
        raw = self.port_var.get().strip() or "8765"
        try:
            return validate_port(raw)
        except ValueError as exc:
            self.log(f"Invalid port: {exc}")
            messagebox.showerror("Invalid Port", str(exc))
            return None

    def _server_url(self) -> str:
        ip = self.host_ip_var.get().strip() or "127.0.0.1"
        port = self.port_var.get().strip() or "8765"
        return f"http://{ip}:{port}"

    def _generate_token(self) -> None:
        tok = secrets.token_urlsafe(18)
        self.token_var.set(tok)
        self._save_current_config()
        self.log("Generated new Bearer token and saved config.")

    def _copy_token(self) -> None:
        tok = self.token_var.get().strip()
        if not tok:
            messagebox.showinfo("No Token", "Token field is empty. Click 'Generate Token' first.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(tok)
        self.root.update()
        self.log("Bearer token copied to clipboard.")

    def _paste_token(self) -> None:
        try:
            tok = self.root.clipboard_get().strip()
            if not tok:
                messagebox.showinfo("Clipboard Empty", "Clipboard has no text.")
                return
            self.token_var.set(tok)
            self._save_current_config()
            self.log("Pasted Bearer token from clipboard and saved config.")
        except Exception as exc:
            messagebox.showwarning("Clipboard Error", f"Could not read clipboard: {exc}")

    def _save_current_config(self) -> None:
        port_raw = self.port_var.get().strip() or "8765"
        try:
            port_num = validate_port(port_raw)
        except ValueError:
            port_num = 8765
        data = {
            "mode": self.mode_var.get(),
            "agent_id": self.agent_var.get().strip() or default_agent_id(),
            "host_ip": self.host_ip_var.get().strip(),
            "port": port_num,
            "token": self.token_var.get().strip(),
            "repo_path": self.repo_var.get().strip(),
            "mcp_mode": self.mcp_mode_var.get(),
            "discover": bool(self.discover_chk_var.get()),
        }
        path = save_config(data)
        self.log(f"Saved config -> {path}")

    def _on_mode_switch(self) -> None:
        mode = self.mode_var.get()
        if mode == "peer" and self.server_proc is not None and self.server_proc.poll() is None:
            if not messagebox.askyesno(
                "Switch to Peer", "Local hub server is running. Stop it and switch to PEER mode?"
            ):
                self.mode_var.set("host")
                return
            self.stop_server()
        if mode == "host":
            self.host_ip_var.set(detect_lan_ip())
        self._apply_mode_ui()
        self._save_current_config()
        self.log(f"Switched role to {mode.upper()}.")

    def _apply_mode_ui(self) -> None:
        is_host = self.mode_var.get() == "host"
        running = self.server_proc is not None and self.server_proc.poll() is None
        self.start_btn.config(state="disabled" if (not is_host or running) else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.fw_btn.config(state="normal" if is_host else "disabled")
        self.pull_btn.config(state="normal" if is_host else "disabled")
        if not running:
            self.status_lbl.config(
                text="HOST STOPPED" if is_host else "PEER READY",
                bg="#d9534f" if is_host else "#0275d8",
            )

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}\n")

    def _poll_logs(self) -> None:
        inserted = 0
        while not self.log_queue.empty() and inserted < 100:
            self.log_box.insert("end", self.log_queue.get_nowait())
            inserted += 1
        if inserted:
            y_view = self.log_box.yview()
            is_at_bottom = y_view[1] >= 0.96
            total_lines = int(self.log_box.index("end-1c").split(".")[0])
            if total_lines > MAX_LOG_LINES:
                excess = total_lines - MAX_LOG_LINES
                self.log_box.delete("1.0", f"{excess + 1}.0")
            if getattr(self, "auto_scroll_var", None) and self.auto_scroll_var.get() and is_at_bottom:
                self.log_box.see("end")
        self.root.after(150, self._poll_logs)

    def clear_logs(self) -> None:
        self.log_box.delete("1.0", "end")

    def _refresh_ip(self) -> None:
        ip = detect_lan_ip()
        self.host_ip_var.set(ip)
        self.log(f"Detected LAN IP: {ip}")

    def discover_lan(self) -> None:
        self.log(f"Listening for LAN agsync hubs on UDP {DEFAULT_DISCOVERY_PORT} (3s)...")
        threading.Thread(target=self._discover_worker, daemon=True).start()

    def _discover_worker(self) -> None:
        try:
            hubs = discover_hubs(timeout_sec=3.0)
        except Exception as exc:
            self.log(f"Discovery error: {exc}")
            return
        if not hubs:
            self.log("No LAN hub beacons received. Make sure Host started with 'Broadcast LAN Discovery' enabled and UDP 8766 is allowed.")
            return
        hub = hubs[0]
        self.log(f"Found {len(hubs)} hub(s): {hub['url']} (machine={hub.get('machine')})")
        self.root.after(0, lambda: self.host_ip_var.set(str(hub.get("host_ip", ""))))
        self.root.after(0, lambda: self.port_var.set(str(hub.get("port", 8765))))

    def start_server(self) -> None:
        if self.server_proc is not None and self.server_proc.poll() is None:
            return
        port_num = self._validated_port()
        if port_num is None:
            return

        if not self.token_var.get().strip():
            # Non-loopback binding requires a Bearer token; auto-generate and persist one.
            self.token_var.set(secrets.token_urlsafe(18))
            self.log("Auto-generated Bearer token for secure LAN binding (share with Peer).")
        self._save_current_config()

        # Clean up any orphaned or zombie server holding state.db.pid
        db_path = default_data_dir() / "state.db"
        try:
            pid_info = inspect_pid_lockfile(db_path)
            if pid_info.get("alive"):
                existing_pid = pid_info.get("data", {}).get("pid")
                if existing_pid and existing_pid != os.getpid():
                    self.log(f"Terminating orphaned hub server process (PID {existing_pid})...")
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/PID", str(existing_pid)], capture_output=True)
                    else:
                        import signal
                        os.kill(existing_pid, signal.SIGKILL)
                    time.sleep(0.6)
            elif pid_info.get("exists") and not pid_info.get("alive"):
                pid_path = Path(pid_info["path"])
                try:
                    pid_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            self.log(f"PID cleanup notice: {exc}")

        cmd = [sys.executable, "-m", "agsync.cli", "serve", "--host", "0.0.0.0", "--port", str(port_num)]
        if self.discover_chk_var.get():
            cmd.append("--discover")
        repo_val = self.repo_var.get().strip()
        if repo_val and Path(repo_val).is_dir():
            cmd.extend(["--repo", repo_val])

        env = dict(os.environ)
        tok = self.token_var.get().strip()
        if tok:
            env["AGSYNC_TOKEN"] = tok

        log_dir = default_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        hub_log_path = log_dir / "agsync-panel-hub.log"
        self._server_log_fh = open(hub_log_path, "a+", encoding="utf-8")
        self._server_log_fh.seek(0, os.SEEK_END)
        start_offset = self._server_log_fh.tell()

        self.log(f"Starting hub server on :{port_num} (log: {hub_log_path})")
        lan_ip = self.host_ip_var.get().strip() or detect_lan_ip()
        self.log(f"🌐 Live Web Dashboard: http://{lan_ip}:{port_num}/dashboard (or click 'Open Web Dashboard')")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.server_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            env=env,
            stdout=self._server_log_fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        self._job.assign_process(self.server_proc)
        self.status_lbl.config(text=f"HOST RUNNING :{port_num}", bg="#5cb85c")
        self._apply_mode_ui()
        self._tail_stop.clear()
        threading.Thread(
            target=self._tail_server_log,
            args=(hub_log_path, start_offset),
            daemon=True,
        ).start()

    def _tail_server_log(self, log_path: Path, start_offset: int) -> None:
        proc = self.server_proc
        pos = start_offset
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as reader:
                reader.seek(pos)
                while not self._tail_stop.is_set():
                    line = reader.readline()
                    if line:
                        self.log_queue.put(line)
                        continue
                    if proc is not None and proc.poll() is not None:
                        rest = reader.read()
                        if rest:
                            self.log_queue.put(rest)
                        break
                    time.sleep(0.2)
        except OSError:
            pass
        if proc is not None:
            code = proc.wait()
            self.log(f"Hub server exited with code {code}.")
        if self._server_log_fh is not None:
            try:
                self._server_log_fh.close()
            except OSError:
                pass
            self._server_log_fh = None
        self.root.after(0, self._apply_mode_ui)

    def stop_server(self) -> None:
        self._tail_stop.set()
        if self.server_proc is not None and self.server_proc.poll() is None:
            self.log("Stopping hub server...")
            self.server_proc.terminate()
            try:
                self.server_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.server_proc.kill()

    def open_firewall(self) -> None:
        port_num = self._validated_port()
        if port_num is None:
            return
        if os.name != "nt":
            self.log("Firewall helper is for Windows. Ensure TCP/UDP ports are open on your OS.")
            return

        profiles = inspect_windows_network_profiles()
        public_profiles = [
            p.get("Name") or p.get("InterfaceAlias") or "unknown"
            for p in profiles
            if str(p.get("NetworkCategory", "")).lower() == "public"
        ]
        if public_profiles:
            self.log(
                f"WARNING: Network profile(s) {public_profiles} are set to Public. "
                "Switch to Private in Windows Settings so LAN firewall rules work."
            )

        ps_cmd = (
            f"New-NetFirewallRule -DisplayName 'agsync LAN Coordination Hub' "
            f"-Direction Inbound -Protocol TCP -LocalPort {port_num} -Profile Private -Action Allow -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -DisplayName 'agsync LAN Discovery' "
            f"-Direction Inbound -Protocol UDP -LocalPort {int(DEFAULT_DISCOVERY_PORT)} -Profile Private -Action Allow -ErrorAction SilentlyContinue"
        )
        self.log(f"Requesting Administrator permission to open TCP {port_num} & UDP {DEFAULT_DISCOVERY_PORT}...")
        try:
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -Command {ps_cmd}"',
                ]
            )
        except Exception as exc:
            self.log(f"ERROR launching UAC prompt: {exc}")

    def pull_state_dialog(self) -> None:
        if self.server_proc is None or self.server_proc.poll() is not None:
            messagebox.showwarning("Start Server First", "Start your local Hub Server before pulling state from an old host.")
            return
        old_url = simpledialog.askstring(
            "Host Handoff (Pull State)",
            "Enter previous Host URL (e.g. http://192.168.1.182:8765):",
            parent=self.root,
        )
        if not old_url:
            return
        threading.Thread(target=self._pull_worker, args=(old_url.strip(),), daemon=True).start()

    def _pull_worker(self, old_url: str) -> None:
        tok = self.token_var.get().strip() or None
        local_url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
        try:
            bundle = SyncClient(old_url, token=tok, timeout=5.0).export_state()
            res = SyncClient(local_url, token=tok, timeout=5.0).import_state(bundle, replace=True)
            self.log(f"State handoff OK from {old_url}: {res.get('counts')}")
        except SyncClientError as exc:
            self.log(f"State handoff FAILED: {exc.message}")

    def run_diagnostics(self) -> None:
        port_num = self._validated_port()
        if port_num is None:
            return
        self._save_current_config()
        ip = self.host_ip_var.get().strip() or "127.0.0.1"
        threading.Thread(target=self._diag_worker, args=(ip, port_num), daemon=True).start()

    def _diag_worker(self, ip: str, port_num: int) -> None:
        is_host = self.mode_var.get() == "host"
        target_ip = "127.0.0.1" if is_host else ip
        self.log(f"--- Diagnostics ({'HOST' if is_host else 'PEER'} -> {target_ip}:{port_num}) ---")

        ok_tcp, tcp_msg = tcp_check(target_ip, port_num, timeout=2.5)
        if ok_tcp:
            self.log(f"[1/2] TCP CONNECT OK: {tcp_msg}")
        else:
            self.log(f"[1/2] TCP CONNECT FAILED ({target_ip}:{port_num}): {tcp_msg}")
            self.log("      -> Check: 1) Hub Server started? 2) Windows Firewall Private rule open? 3) VPN/Proxy LAN exclusion?")
            self.root.after(0, lambda: self.status_lbl.config(text="TCP FAILED", bg="#d9534f"))
            return

        url = f"http://{target_ip}:{port_num}"
        ok_http, msg = check_health(url)
        if ok_http:
            self.log(f"[2/2] HEALTH OK: {msg}")
            self.root.after(0, lambda: self.status_lbl.config(text="CONNECTED", bg="#5cb85c"))
        else:
            self.log(f"[2/2] HEALTH FAILED ({url}/health): {msg}")
            self.root.after(0, lambda: self.status_lbl.config(text="HTTP FAILED", bg="#f0ad4e"))

    def run_doctor_ui(self) -> None:
        self.log("Running agsync doctor...")
        repo_str = self.repo_var.get().strip() or str(ROOT_DIR)
        url = (
            f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
            if self.mode_var.get() == "host"
            else self._server_url()
        )
        threading.Thread(target=self._doctor_worker, args=(Path(repo_str), url), daemon=True).start()

    def _doctor_worker(self, repo: Path, url: str) -> None:
        try:
            report = run_doctor(repo=repo, server_url=url)
            self.log(f"=== agsync doctor ({'OK' if report['ok'] else 'ISSUES FOUND'}) ===")
            for chk in report["checks"]:
                self.log(f"[{chk['status'].upper()}] {chk['name']}: {chk['detail']}")
                if chk.get("fix"):
                    self.log(f"       fix: {chk['fix']}")
        except Exception as exc:
            self.log(f"Doctor error: {exc}")

    def check_status(self) -> None:
        ip = "127.0.0.1" if self.mode_var.get() == "host" else self.host_ip_var.get().strip()
        port = self.port_var.get().strip() or "8765"
        url = f"http://{ip}:{port}"
        threading.Thread(target=self._status_worker, args=(url,), daemon=True).start()

    def _status_worker(self, url: str) -> None:
        cmd = [sys.executable, "-m", "agsync.cli", "status", "--server", url]
        env = dict(os.environ)
        tok = self.token_var.get().strip()
        if tok:
            env["AGSYNC_TOKEN"] = tok
        res = subprocess.run(cmd, cwd=str(ROOT_DIR), env=env, capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            self.log(f"Live Snapshot:\n{res.stdout.strip()}")
        else:
            self.log(f"Status error: {res.stderr.strip() or res.stdout.strip()}")

    def open_dashboard(self) -> None:
        ip = "127.0.0.1" if self.mode_var.get() == "host" else self.host_ip_var.get().strip()
        port = self.port_var.get().strip() or "8765"
        tok = self.token_var.get().strip()
        url = f"http://{ip}:{port}/dashboard"
        if tok:
            url += f"?token={tok}#token={tok}"
        self.log(f"Opening dashboard: {url}")
        webbrowser.open(url)

    def check_inbox_ui(self) -> None:
        ip = "127.0.0.1" if self.mode_var.get() == "host" else self.host_ip_var.get().strip()
        port = self.port_var.get().strip() or "8765"
        url = f"http://{ip}:{port}"
        tok = self.token_var.get().strip()
        agent = self.agent_var.get().strip() or default_agent_id()

        def _worker() -> None:
            try:
                client = SyncClient(url, token=tok or None, timeout=5.0)
                res = client.poll_messages(agent, unread_only=False)
                msgs = res.get("messages", [])
                if not msgs:
                    self.log(f"Inbox for '{agent}': No messages found.")
                    self.root.after(0, lambda: messagebox.showinfo("Inbox", f"No messages found for '{agent}'."))
                    return
                lines = []
                for m in msgs:
                    sender = m.get("sender_id", "unknown")
                    body = m.get("body", "")
                    lines.append(f"• From: {sender}\n  {body}")
                full_text = "\n\n".join(lines)
                self.log(f"Inbox for '{agent}' ({len(msgs)} messages):\n{full_text}")
                self.root.after(0, lambda: messagebox.showinfo(f"Inbox ({len(msgs)} messages)", full_text))
            except Exception as exc:
                self.log(f"Inbox check error: {exc}")
                self.root.after(0, lambda: messagebox.showerror("Inbox Error", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def send_message_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Send Sync Message")
        dlg.geometry("440x220")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Recipient Agent ID (e.g. soheilnasiri or *):").pack(anchor="w", padx=12, pady=(12, 2))
        recip_var = tk.StringVar(value="soheilnasiri" if self.agent_var.get().strip() != "soheilnasiri" else "desktop-146aj7o")
        ttk.Entry(dlg, textvariable=recip_var, width=35).pack(anchor="w", padx=12, pady=2)

        ttk.Label(dlg, text="Message Body:").pack(anchor="w", padx=12, pady=(8, 2))
        body_var = tk.StringVar()
        body_entry = ttk.Entry(dlg, textvariable=body_var, width=45)
        body_entry.pack(fill="x", padx=12, pady=2)
        body_entry.focus_set()

        def _do_send() -> None:
            to_agent = recip_var.get().strip()
            body_txt = body_var.get().strip()
            if not to_agent or not body_txt:
                messagebox.showerror("Error", "Please provide recipient and message body.", parent=dlg)
                return
            dlg.destroy()
            ip = "127.0.0.1" if self.mode_var.get() == "host" else self.host_ip_var.get().strip()
            port = self.port_var.get().strip() or "8765"
            url = f"http://{ip}:{port}"
            tok = self.token_var.get().strip()
            from_agent = self.agent_var.get().strip() or default_agent_id()

            def _send_worker() -> None:
                try:
                    client = SyncClient(url, token=tok or None, timeout=5.0)
                    client.send_message(from_agent, to_agent, body_txt)
                    self.log(f"Message sent to '{to_agent}': {body_txt}")
                    self.root.after(0, lambda: messagebox.showinfo("Sent", f"Message sent to '{to_agent}' successfully!"))
                except Exception as exc:
                    self.log(f"Failed to send message: {exc}")
                    self.root.after(0, lambda: messagebox.showerror("Send Error", str(exc)))

            threading.Thread(target=_send_worker, daemon=True).start()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=12, pady=16)
        ttk.Button(btn_row, text="Send Message", command=_do_send).pack(side="right", padx=4)
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=4)

    def _browse_repo(self) -> None:
        chosen = filedialog.askdirectory(title="Select Target Git Project Repository")
        if chosen:
            self.repo_var.set(chosen)
            self._save_current_config()

    def _paste_repo_path(self) -> None:
        try:
            val = self.root.clipboard_get().strip().strip('"').strip("'")
            if not val:
                messagebox.showinfo("Clipboard Empty", "Clipboard has no text.")
                return
            self.repo_var.set(val)
            self._save_current_config()
            self.log(f"Pasted Project Repo path from clipboard: {val}")
        except Exception as exc:
            messagebox.showwarning("Clipboard Error", f"Could not read clipboard: {exc}")

    def connect_mcp(self) -> None:
        self._save_current_config()
        url = (
            f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
            if self.mode_var.get() == "host"
            else self._server_url()
        )
        try:
            res = configure_antigravity_mcp(
                server_url=url,
                agent_id=self.agent_var.get().strip() or default_agent_id(),
                mode=self.mcp_mode_var.get(),
                token=self.token_var.get().strip(),
            )
            self.log(f"Antigravity MCP configured ({res['mode']}) -> {res['mcp_config_path']}")
            if res.get("backup_path"):
                self.log(f"Backup created at: {res['backup_path']}")
            messagebox.showinfo(
                "MCP Connected",
                f"Updated {res['mcp_config_path']} for agent '{res['agent_id']}'.\nRestart or reload MCP servers in Antigravity if needed.",
            )
        except Exception as exc:
            self.log(f"MCP setup FAILED: {exc}")
            messagebox.showerror("MCP Setup Failed", str(exc))

    def ensure_gitignore_ui(self) -> None:
        repo_str = self.repo_var.get().strip()
        if not repo_str:
            self._browse_repo()
            repo_str = self.repo_var.get().strip()
        if not repo_str:
            return
        repo = Path(repo_str)
        if not repo.is_dir():
            messagebox.showerror("Invalid Folder", f"Directory does not exist:\n{repo}")
            return
        from agsync.commands.hooks import ensure_repo_gitignore
        changed, note = ensure_repo_gitignore(repo)
        self.log(f"Multi-Agent Safe .gitignore ({note}) -> {repo / '.gitignore'}")
        messagebox.showinfo(
            "Safe .gitignore Ready",
            f"Multi-agent safe .gitignore ({note}):\n{repo / '.gitignore'}\n\n"
            "Excluded items:\n"
            "• Local SQLite databases (*.sqlite)\n"
            "• Local machine identity (.agsync/)\n"
            "• Large archives (*.zip, *.rar)\n"
            "• Cache and log files (.deploy_cache.json, *.log)",
        )

    def attach_repo(self) -> None:
        repo_str = self.repo_var.get().strip()
        if not repo_str:
            self._browse_repo()
            repo_str = self.repo_var.get().strip()
        if not repo_str:
            return
        self._save_current_config()
        repo = Path(repo_str)
        auto_git_init = False
        if repo.is_dir() and not (repo / ".git").exists():
            if not messagebox.askyesno(
                "Initialize Git Repository?",
                f"'{repo}' is not a Git repository yet.\n\nRun 'git init -b main' automatically and install agsync hooks?",
            ):
                return
            auto_git_init = True
        url = (
            f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
            if self.mode_var.get() == "host"
            else self._server_url()
        )
        agent = self.agent_var.get().strip() or default_agent_id()
        tok = self.token_var.get().strip()
        self.attach_btn.config(state="disabled")
        self.log(f"Installing repo hooks & templates in {repo}...")
        threading.Thread(
            target=self._attach_repo_worker,
            args=(repo, url, agent, tok, auto_git_init),
            daemon=True,
        ).start()

    def _attach_repo_worker(
        self, repo: Path, url: str, agent: str, tok: str, auto_git_init: bool = False
    ) -> None:
        if auto_git_init:
            try:
                init_res = subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=str(repo),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if init_res.returncode == 0:
                    self.log(f"Initialized Git repository in {repo} (branch: main)")
                else:
                    err_init = init_res.stderr.strip() or init_res.stdout.strip()
                    self.root.after(0, lambda: self.attach_btn.config(state="normal"))
                    self.root.after(0, lambda: messagebox.showerror("Git Init Failed", err_init))
                    return
            except Exception as exc:
                self.root.after(0, lambda: self.attach_btn.config(state="normal"))
                self.root.after(0, lambda: messagebox.showerror("Git Init Failed", str(exc)))
                return
        cmd = [
            sys.executable,
            "-m",
            "agsync.cli",
            "install-hooks",
            "--repo",
            str(repo),
            "--server",
            url,
            "--agent",
            agent,
            "--with-antigravity-hook",
            "--with-templates",
            "--require-lease",
        ]
        if auto_git_init:
            cmd.append("--init-if-needed")
        env = dict(os.environ)
        if tok:
            env["AGSYNC_TOKEN"] = tok
        try:
            res = subprocess.run(
                cmd, cwd=str(ROOT_DIR), env=env, capture_output=True, text=True, timeout=15
            )
            ok = res.returncode == 0
            out = res.stdout.strip()
            err = res.stderr.strip() or out
        except Exception as exc:
            ok = False
            out = ""
            err = str(exc)

        def _finish() -> None:
            self.attach_btn.config(state="normal")
            if ok:
                self.log(f"Repo hooks & templates installed:\n{out}")
                if self.mode_var.get() == "host":
                    try:
                        SyncClient(url, token=tok or None, timeout=5.0).attach_repo(repo)
                        self.log(f"Attached repository to hub for peer cloning: {repo}")
                    except Exception as exc:
                        self.log(f"Hub repo attach notice: {exc}")
                messagebox.showinfo("Repo Attached", f"Hooks & templates installed in:\n{repo}")
            else:
                self.log(f"Repo hook install FAILED: {err}")
                messagebox.showerror("Repo Setup Failed", err)

        self.root.after(0, _finish)

    def toggle_git_daemon_ui(self) -> None:
        if self.git_daemon_proc is not None and self.git_daemon_proc.poll() is None:
            self.git_daemon_proc.terminate()
            self.git_daemon_proc = None
            self.git_daemon_btn.config(text="📡 Share Git on LAN")
            self.log("LAN Git Server stopped.")
            try:
                hub_url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}" if self.mode_var.get() == "host" else self._server_url()
                tok = self.token_var.get().strip() or None
                SyncClient(hub_url, token=tok, timeout=2.0).publish_context("lan_git:url", "")
            except Exception:
                pass
            return

        repo_val = self.repo_var.get().strip() or str(ROOT_DIR)
        repo_path = Path(repo_val).resolve()
        if not (repo_path / ".git").exists():
            messagebox.showerror("No Git Repo", f"'{repo_path}' is not a Git repository.")
            return

        try:
            subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"], capture_output=True)

            # Auto-ensure repository has an active commit and 'main' branch
            has_commit = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
            ).returncode == 0
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
            )
            has_changes = bool(status_res.stdout.strip())
            if not has_commit or has_changes:
                self.log("Auto-committing current project state before LAN share...")
                subprocess.run(["git", "add", "-A"], cwd=str(repo_path), capture_output=True)
                subprocess.run(
                    ["git", "commit", "-m", "chore: snapshot project state before LAN share"],
                    cwd=str(repo_path),
                    capture_output=True,
                )
                subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo_path), capture_output=True)
                self.log("Auto-commit complete. Branch 'main' ready for peer sync.")

            parent_dir = str(repo_path.parent)
            repo_name = repo_path.name
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.git_daemon_proc = subprocess.Popen(
                [
                    "git",
                    "daemon",
                    "--reuseaddr",
                    f"--base-path={parent_dir}",
                    "--export-all",
                    "--listen=0.0.0.0",
                    "--port=9418",
                ],
                creationflags=flags,
            )
            self._job.assign_process(self.git_daemon_proc)
            self.git_daemon_btn.config(text="⏹️ Stop LAN Git (:9418)")
            lan_ip = self.host_ip_var.get().strip() or detect_lan_ip()
            git_url = f"git://{lan_ip}:9418/{repo_name}"
            self.log(f"📡 LAN Git Server running on {git_url}")
            self.log(f"Peer can pull using: git pull {git_url} main")

            # Announce LAN Git URL to hub context so peer can 1-click pull without typing!
            try:
                hub_url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}" if self.mode_var.get() == "host" else self._server_url()
                tok = self.token_var.get().strip() or None
                SyncClient(hub_url, token=tok, timeout=2.0).publish_context("lan_git:url", git_url)
                self.log(f"Published LAN Git URL to Hub context: {git_url}")
            except Exception:
                pass

            messagebox.showinfo(
                "LAN Git Server Active",
                f"Repository is now shared on the local network!\n\nGit URL:\n{git_url}\n\nYour peer can now click 'Pull from Peer LAN Git' to clone or pull instantly without internet.",
            )
        except Exception as exc:
            self.log(f"Failed to start LAN Git server: {exc}")
            messagebox.showerror("LAN Git Error", str(exc))

    def pull_lan_git_ui(self) -> None:
        """1-Click Clone or Pull project directly from Peer's LAN Git Server (git://)."""
        hub_url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}" if self.mode_var.get() == "host" else self._server_url()
        tok = self.token_var.get().strip() or None

        # 1. Discover published LAN Git URL from hub context
        discovered_git_url = ""
        try:
            client = SyncClient(hub_url, token=tok, timeout=2.5)
            ctx = client.get_context("lan_git:url")
            if ctx.get("found"):
                discovered_git_url = str(ctx.get("value") or "").strip()
        except Exception:
            pass

        # 2. If not found in context, prompt with intelligent pre-fill
        if not discovered_git_url:
            peer_ip = self.host_ip_var.get().strip() or "192.168.1.130"
            repo_val = self.repo_var.get().strip() or str(ROOT_DIR)
            repo_name = Path(repo_val).name
            default_url = f"git://{peer_ip}:9418/{repo_name}"
            target_url = simpledialog.askstring(
                "Pull from Peer LAN Git",
                "Enter Peer's LAN Git URL:\n(e.g. git://192.168.1.x:9418/repo_name)",
                initialvalue=default_url,
                parent=self.root,
            )
            if not target_url:
                return
            discovered_git_url = target_url.strip()

        dest_val = self.repo_var.get().strip()
        if not dest_val:
            dest_val = filedialog.askdirectory(title="Select Local Folder to Clone/Pull Into", parent=self.root)
            if not dest_val:
                return
            self.repo_var.set(dest_val)
            self._save_current_config()

        dest_path = Path(dest_val)
        self.pull_lan_git_btn.config(state="disabled")
        self.log(f"Connecting to Peer LAN Git: {discovered_git_url} -> {dest_path}...")

        def _worker() -> None:
            err = None
            out_msg = ""
            try:
                subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"], capture_output=True)
                if (dest_path / ".git").exists():
                    self.log(f"Local repo exists. Pulling latest commits from {discovered_git_url}...")
                    res = subprocess.run(
                        ["git", "pull", discovered_git_url, "main"],
                        cwd=str(dest_path),
                        capture_output=True,
                        text=True,
                        timeout=30.0,
                    )
                    if res.returncode == 0:
                        out_msg = res.stdout.strip() or "Already up to date."
                    else:
                        res2 = subprocess.run(
                            ["git", "pull", discovered_git_url],
                            cwd=str(dest_path),
                            capture_output=True,
                            text=True,
                            timeout=30.0,
                        )
                        if res2.returncode == 0:
                            out_msg = res2.stdout.strip() or "Already up to date."
                        else:
                            err = res.stderr.strip() or res2.stderr.strip()
                else:
                    self.log(f"Cloning fresh repository from {discovered_git_url} into {dest_path}...")
                    dest_path.mkdir(parents=True, exist_ok=True)
                    res = subprocess.run(
                        ["git", "clone", discovered_git_url, str(dest_path)],
                        capture_output=True,
                        text=True,
                        timeout=60.0,
                    )
                    if res.returncode == 0:
                        out_msg = f"Project successfully cloned from {discovered_git_url} into {dest_path}!"
                    else:
                        err = res.stderr.strip() or res.stdout.strip()
            except Exception as exc:
                err = str(exc)

            if err and ("Connection refused" in err or "unable to connect" in err):
                self.log("LAN Git (:9418) not active on peer. Falling back to Hub Server (:8765)...")
                hub_res = pull_latest_changes(dest_path, hub_url, token=tok, log_fn=self.log)
                if hub_res.get("status") == "ok":
                    err = None
                    out_msg = f"Synced via Hub: {hub_res.get('output', 'Already up to date.')}"
                elif hub_res.get("status") == "busy":
                    err = None
                    out_msg = f"Sync postponed: {hub_res.get('message')}"
                elif hub_res.get("message"):
                    err = f"LAN Git refused and Hub sync notice: {hub_res.get('message')}"

            def _done() -> None:
                self.pull_lan_git_btn.config(state="normal")
                if err:
                    self.log(f"Peer LAN Git sync FAILED: {err}")
                    messagebox.showerror("LAN Git Sync Failed", f"Could not sync from {discovered_git_url}:\n\n{err}")
                else:
                    self.log(f"LAN Git Sync SUCCESS: {out_msg}")
                    messagebox.showinfo(
                        "LAN Git Sync Complete",
                        f"Project synchronized successfully via LAN Git!\n\nOutput:\n{out_msg}",
                    )

            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_close(self) -> None:
        self._save_current_config()
        if self.git_daemon_proc is not None and self.git_daemon_proc.poll() is None:
            self.git_daemon_proc.terminate()
        if self.server_proc is not None and self.server_proc.poll() is None:
            if messagebox.askyesno("Exit", "Hub server is still running. Stop it and exit?"):
                self.stop_server()
            else:
                return
        self._job.close()
        self.root.destroy()

    def create_worktree_ui(self) -> None:
        repo_str = self.repo_var.get().strip() or str(ROOT_DIR)
        aid = simpledialog.askstring("Create Worktree", "Agent ID for new worktree:", parent=self.root)
        if not aid:
            return
        threading.Thread(
            target=self._worktree_worker, args=(Path(repo_str), aid.strip()), daemon=True
        ).start()

    def _worktree_worker(self, repo: Path, aid: str) -> None:
        from agsync.commands.worktree import create_worktree
        try:
            res = create_worktree(repo, aid)
            self.log(f"Worktree created: {res}")
        except Exception as exc:
            self.log(f"Worktree error: {exc}")

    def create_invite_ui(self) -> None:
        aid = simpledialog.askstring("Create Join Link", "Target peer Agent ID:", parent=self.root)
        if not aid:
            return
        base_url = simpledialog.askstring(
            "Base URL", "Public/LAN Base URL:", initialvalue=self._server_url(), parent=self.root
        )
        if not base_url:
            return
        threading.Thread(
            target=self._invite_worker, args=(aid.strip(), base_url.strip()), daemon=True
        ).start()

    def _invite_worker(self, aid: str, base_url: str) -> None:
        tok = self.token_var.get().strip() or None
        local_url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
        try:
            res = SyncClient(local_url, token=tok, timeout=5.0).create_invite(aid)
            from urllib.parse import quote
            link = (
                f"{base_url.rstrip('/')}/join?nonce={quote(res['nonce'])}"
                f"&sig={quote(res['signature'])}&agent={quote(res['agent_id'])}"
            )
            self.log(f"Join Link for '{aid}': {link}")
        except Exception as exc:
            self.log(f"Create invite failed: {exc}")

    def join_remote_ui(self) -> None:
        raw = simpledialog.askstring("Join Remote Hub", "Paste Join Link URL:", parent=self.root)
        if not raw:
            return
        threading.Thread(target=self._join_worker, args=(raw.strip(),), daemon=True).start()

    def _join_worker(self, raw_url: str) -> None:
        from urllib.parse import parse_qs, urlparse
        try:
            parsed = urlparse(raw_url)
            qs = parse_qs(parsed.query)
            base = f"{parsed.scheme}://{parsed.netloc}"
            nonce = qs.get("nonce", [""])[0]
            sig = qs.get("sig", [""])[0]
            res = SyncClient(base, timeout=8.0).join_invite(nonce, sig)
            self.root.after(0, lambda: self.host_ip_var.set(parsed.hostname or ""))
            self.root.after(0, lambda: self.port_var.set(str(parsed.port or (443 if parsed.scheme == "https" else 80))))
            self.root.after(0, lambda: self.token_var.set(str(res.get("token", ""))))
            self.root.after(0, lambda: self.agent_var.set(str(res.get("agent_id", ""))))
            self.log(f"Joined remote hub as '{res.get('agent_id')}' (per-agent token saved).")
        except Exception as exc:
            self.log(f"Join remote failed: {exc}")

    def start_tunnel_ui(self) -> None:
        port = self.port_var.get().strip() or "8765"
        threading.Thread(
            target=self._tunnel_worker, args=(f"http://127.0.0.1:{port}",), daemon=True
        ).start()

    def _tunnel_worker(self, local_url: str) -> None:
        from agsync.adapters.tunnel import start_cloudflared_tunnel
        try:
            proc = start_cloudflared_tunnel(local_url, protocol="http2")
            self.log(f"Cloudflare Tunnel started (PID={proc.pid}) for {local_url}")
        except Exception as exc:
            self.log(f"Cloudflare Tunnel error: {exc}")

    def merge_check_ui(self) -> None:
        repo_str = self.repo_var.get().strip() or str(ROOT_DIR)
        threading.Thread(
            target=self._merge_check_worker, args=(Path(repo_str),), daemon=True
        ).start()

    def _merge_check_worker(self, repo: Path) -> None:
        from agsync.commands.merge_check import check_merge_tree
        try:
            res = check_merge_tree(repo, base="main", target="HEAD")
            if res["clean"]:
                self.log(f"Merge Check OK: HEAD merges cleanly into main ({res['merge_tree_oid'][:10]})")
            else:
                self.log(f"Merge Check CONFLICTS: {res['conflicts']}")
        except Exception as exc:
            self.log(f"Merge Check error: {exc}")

    def clone_project_ui(self) -> None:
        url = self._server_url()
        tok = self.token_var.get().strip() or None
        agent = self.agent_var.get().strip() or default_agent_id()
        dest_val = self.repo_var.get().strip()
        if not dest_val:
            dest_val = filedialog.askdirectory(
                title="Select Local Folder to Clone Project Into",
                parent=self.root,
            )
            if not dest_val:
                return
            self.repo_var.set(dest_val)
            self._save_current_config()

        dest_path = Path(dest_val)
        self.clone_btn.config(state="disabled")
        self.log(f"Starting 1-click clone from host ({url}) into {dest_path}...")

        def _worker() -> None:
            err = None
            try:
                clone_from_host(
                    server_url=url,
                    dest_dir=dest_path,
                    token=tok,
                    agent_id=agent,
                    log_fn=self.log,
                )
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                self.clone_btn.config(state="normal")
                if err:
                    self.log(f"Clone FAILED: {err}")
                    messagebox.showerror("Clone Failed", err)
                else:
                    self.log(f"Project successfully cloned and configured in {dest_path}!")
                    messagebox.showinfo(
                        "Clone Complete",
                        f"Project cloned from host into:\n{dest_path}\n\nAntigravity MCP and safety hooks installed!",
                    )

            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def sync_changes_ui(self) -> None:
        repo_val = self.repo_var.get().strip() or str(ROOT_DIR)
        url = (
            f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
            if self.mode_var.get() == "host"
            else self._server_url()
        )
        tok = self.token_var.get().strip() or None
        self.sync_btn.config(state="disabled")
        self.log(f"Checking for updates in {repo_val}...")

        def _worker() -> None:
            err = None
            res: dict[str, Any] = {}
            try:
                res = pull_latest_changes(repo_val, url, token=tok, log_fn=self.log)
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                self.sync_btn.config(state="normal")
                if err:
                    self.log(f"Sync FAILED: {err}")
                    messagebox.showerror("Sync Failed", err)
                elif res.get("status") == "busy":
                    messagebox.showwarning(
                        "Sync Postponed",
                        res.get("message", "Working tree has uncommitted changes."),
                    )
                elif res.get("status") == "error":
                    messagebox.showerror(
                        "Sync Failed",
                        res.get("message", "Could not pull changes."),
                    )
                else:
                    messagebox.showinfo(
                        "Sync OK",
                        f"Repository synchronized successfully:\n{res.get('output', 'Already up to date.')}",
                    )

            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_auto_sync_listener(self) -> None:
        def _loop() -> None:
            last_event_id = 0
            last_heartbeat = 0.0
            while not self._tail_stop.is_set():
                url = (
                    f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
                    if self.mode_var.get() == "host"
                    else self._server_url()
                )
                tok = self.token_var.get().strip() or None
                my_agent = self.agent_var.get().strip() or default_agent_id()

                # Automated Heartbeat every 45s: keeps agent continuously ONLINE while panel is open
                now = time.time()
                if now - last_heartbeat >= 45.0:
                    try:
                        hb_client = SyncClient(url, token=tok, timeout=4.0)
                        hb_client.register(my_agent, status="online", ttl_sec=120)
                        last_heartbeat = now
                    except Exception:
                        pass

                if not self.auto_sync_var.get():
                    time.sleep(2.0)
                    continue

                repo_val = self.repo_var.get().strip()
                if not repo_val or not Path(repo_val).is_dir():
                    time.sleep(3.0)
                    continue

                try:
                    client = SyncClient(url, token=tok, timeout=20.0)
                    events = client.get_events(since_id=last_event_id, wait=10.0, limit=50)
                    for ev in events:
                        ev_id = int(ev.get("event_id") or ev.get("id") or 0)
                        if ev_id > last_event_id:
                            last_event_id = ev_id
                        ev_type = str(ev.get("event_type") or "")
                        actor = str(ev.get("actor_id") or ev.get("agent_id") or "")
                        target_id = str(ev.get("target_id") or "")
                        summary = str(ev.get("summary") or "")
                        payload = ev.get("payload") or {}

                        # Stream real-time coordination activity to logs
                        if ev_type == "message_sent":
                            self.log(f"💬 [Hub Msg #{ev_id}] {actor} -> {target_id}: {payload.get('body') or summary}")
                        elif ev_type == "lease_acquired":
                            self.log(f"🔒 [Hub Lock #{ev_id}] {actor} locked '{target_id}'")
                        elif ev_type in ("lease_released", "lease_expired"):
                            self.log(f"🔓 [Hub Unlock #{ev_id}] Lock on '{target_id}' freed by {actor}")
                        elif ev_type == "agent_registered":
                            st = payload.get("status", "online")
                            self.log(f"👤 [Hub Agent #{ev_id}] Agent '{actor}' registered ({st})")
                        elif ev_type == "task_created":
                            self.log(f"📋 [Hub Task #{ev_id}] Task created: {payload.get('title') or target_id} (by {actor})")
                        elif ev_type == "task_updated":
                            st = payload.get("status") or ""
                            self.log(f"📋 [Hub Task #{ev_id}] Task {target_id} status: {st} (by {actor})")

                        owner = actor
                        if ev_type == "task_updated" and payload.get("status") == "done" and owner != my_agent:
                            has_lease = False
                            try:
                                snap = client.snapshot(agent_id=my_agent)
                                active_leases = snap.get("active_leases") or []
                                has_lease = any(l.get("owner_id") == my_agent for l in active_leases)
                            except Exception:
                                pass

                            if has_lease:
                                self.pending_sync = True
                                self.log(
                                    f"Peer '{owner}' completed task {payload.get('task_id')}. "
                                    "Sync queued (pending local task completion)."
                                )
                            else:
                                self.log(f"Peer '{owner}' completed task {payload.get('task_id')}. Auto-syncing changes...")
                                pull_latest_changes(repo_val, url, token=tok, log_fn=self.log)

                        if self.pending_sync:
                            has_lease = False
                            try:
                                snap = client.snapshot(agent_id=my_agent)
                                active_leases = snap.get("active_leases") or []
                                has_lease = any(l.get("owner_id") == my_agent for l in active_leases)
                            except Exception:
                                pass
                            if not has_lease:
                                self.pending_sync = False
                                self.log("Local tasks clear. Running queued auto-sync...")
                                pull_latest_changes(repo_val, url, token=tok, log_fn=self.log)

                except Exception:
                    time.sleep(3.0)

        threading.Thread(target=_loop, daemon=True).start()

    def check_updates_ui(self) -> None:
        self.log("Checking GitHub for agsync updates...")

        def _worker() -> None:
            from scripts.updater import check_github_update, perform_update

            info = check_github_update()
            if info.get("error"):
                self.log(f"Update check notice: {info['error']}")

                def _fallback_prompt() -> None:
                    if messagebox.askyesno(
                        "Update Connection Notice",
                        f"GitHub API connection timed out ({info['error']}).\n\n"
                        "Would you like to try updating directly via Git / direct download?",
                    ):
                        def _update_job() -> None:
                            ok, msg = perform_update(log_fn=self.log)
                            if ok:
                                messagebox.showinfo(
                                    "Update Complete",
                                    f"Successfully updated agsync!\n\n{msg}\n\nPlease restart the panel to use the new version.",
                                )
                            else:
                                messagebox.showerror("Update Failed", f"Update failed: {msg}")

                        threading.Thread(target=_update_job, daemon=True).start()

                self.root.after(0, _fallback_prompt)
                return
            cur = info.get("current_version")
            if not info.get("has_update"):
                self.log(f"agsync is up to date (v{cur}).")
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Up to Date", f"You are running the latest version of agsync (v{cur})."
                    ),
                )
                return

            latest = info.get("latest_version")
            self.log(f"New version available: v{latest} (current: v{cur})")

            def _prompt() -> None:
                if messagebox.askyesno(
                    "Update Available",
                    f"A new version of agsync is available!\n\nCurrent version: v{cur}\nLatest version: v{latest}\n\nWould you like to install the update now?",
                ):
                    def _update_job() -> None:
                        ok, msg = perform_update(log_fn=self.log)
                        if ok:
                            messagebox.showinfo(
                                "Update Complete",
                                f"Successfully updated to v{latest}!\n\nPlease restart the panel to use the new version.",
                            )
                        else:
                            messagebox.showerror("Update Failed", f"Update failed: {msg}")

                    threading.Thread(target=_update_job, daemon=True).start()

            self.root.after(0, _prompt)

        threading.Thread(target=_worker, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="agsync Unified XAMPP-style Control Panel")
    parser.add_argument("--mode", choices=["host", "peer"], default=None)
    args = parser.parse_args()

    root = tk.Tk()
    AgsyncControlPanel(root, initial_mode=args.mode)
    root.mainloop()


if __name__ == "__main__":
    main()
