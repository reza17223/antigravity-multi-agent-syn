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

from agsync.adapters.network import (
    WindowsJobObject,
    build_opener_for_url,
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


class AgsyncControlPanel:
    def __init__(self, root: tk.Tk, initial_mode: str | None = None) -> None:
        self.root = root
        self.cfg = load_config()
        if initial_mode in ("host", "peer"):
            self.cfg["mode"] = initial_mode

        self.root.title("agsync Unified Control Panel")
        self.root.geometry("860x700")
        self.root.minsize(760, 600)

        self.server_proc: subprocess.Popen[bytes] | None = None
        self._server_log_fh = None
        self._tail_stop = threading.Event()
        self._job = WindowsJobObject()
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._apply_mode_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_logs()
        self._start_auto_sync_listener()

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}

        # Header + Role Switch
        header = ttk.Frame(self.root)
        header.pack(fill="x", **pad)

        ttk.Label(header, text="agsync Control Panel", font=("Segoe UI", 14, "bold")).pack(side="left")

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
        ttk.Button(row2, text="Open Web Dashboard", command=self.open_dashboard).pack(side="left", padx=4)

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

        r_sync = ttk.Frame(ag_frame)
        r_sync.pack(fill="x", padx=6, pady=4)
        ttk.Label(r_sync, text="Team Sync:").pack(side="left", padx=(0, 6))
        self.clone_btn = ttk.Button(
            r_sync, text="📥 Clone Project from Host", command=self.clone_project_ui
        )
        self.clone_btn.pack(side="left", padx=4)
        self.sync_btn = ttk.Button(
            r_sync, text="🔄 Pull / Sync Changes", command=self.sync_changes_ui
        )
        self.sync_btn.pack(side="left", padx=4)
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
            total_lines = int(self.log_box.index("end-1c").split(".")[0])
            if total_lines > MAX_LOG_LINES:
                excess = total_lines - MAX_LOG_LINES
                self.log_box.delete("1.0", f"{excess + 1}.0")
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
        url = f"http://{ip}:{port}/dashboard"
        self.log(f"Opening dashboard: {url}")
        webbrowser.open(url)

    def _browse_repo(self) -> None:
        chosen = filedialog.askdirectory(title="Select Target Git Project Repository")
        if chosen:
            self.repo_var.set(chosen)
            self._save_current_config()

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

    def _on_close(self) -> None:
        self._save_current_config()
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
            while not self._tail_stop.is_set():
                if not self.auto_sync_var.get():
                    time.sleep(2.0)
                    continue
                url = (
                    f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
                    if self.mode_var.get() == "host"
                    else self._server_url()
                )
                tok = self.token_var.get().strip() or None
                repo_val = self.repo_var.get().strip()
                my_agent = self.agent_var.get().strip() or default_agent_id()
                if not repo_val or not Path(repo_val).is_dir():
                    time.sleep(3.0)
                    continue

                try:
                    client = SyncClient(url, token=tok, timeout=20.0)
                    events = client.get_events(since_id=last_event_id, wait=10.0, limit=50)
                    for ev in events:
                        ev_id = int(ev.get("event_id", 0))
                        if ev_id > last_event_id:
                            last_event_id = ev_id
                        ev_type = ev.get("event_type")
                        owner = ev.get("agent_id")
                        payload = ev.get("payload") or {}

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


def main() -> None:
    parser = argparse.ArgumentParser(description="agsync Unified XAMPP-style Control Panel")
    parser.add_argument("--mode", choices=["host", "peer"], default=None)
    args = parser.parse_args()

    root = tk.Tk()
    AgsyncControlPanel(root, initial_mode=args.mode)
    root.mainloop()


if __name__ == "__main__":
    main()
