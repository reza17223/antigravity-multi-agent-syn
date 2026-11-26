"""Unified XAMPP-style Control Panel for agsync (Host/Peer switch, MCP & Repo setup, Diagnostics)."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agsync.client import SyncClient, SyncClientError
from agsync.config import (
    configure_antigravity_mcp,
    copy_project_templates,
    default_agent_id,
    detect_lan_ip,
    load_config,
    save_config,
)
from agsync.discovery import DEFAULT_DISCOVERY_PORT, discover_hubs


def check_health(url: str, timeout: float = 2.5) -> tuple[bool, str]:
    """Check /health endpoint and return (ok, response_or_error)."""
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


def ping_host(ip: str) -> tuple[bool, str]:
    """Run a single ICMP ping to check LAN reachability."""
    count_flag = "-n" if os.name == "nt" else "-c"
    wait_flag = "-w" if os.name == "nt" else "-W"
    wait_val = "1500" if os.name == "nt" else "2"
    try:
        res = subprocess.run(
            ["ping", count_flag, "1", wait_flag, wait_val, ip],
            capture_output=True,
            text=True,
            timeout=4,
        )
        return res.returncode == 0, res.stdout.strip()
    except Exception as exc:
        return False, str(exc)


class AgsyncControlPanel:
    def __init__(self, root: tk.Tk, initial_mode: str | None = None) -> None:
        self.root = root
        self.cfg = load_config()
        if initial_mode in ("host", "peer"):
            self.cfg["mode"] = initial_mode

        self.root.title("agsync Unified Control Panel")
        self.root.geometry("840x680")
        self.root.minsize(740, 580)

        self.server_proc: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._apply_mode_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_logs()

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

        ttk.Label(net_frame, text="Token (opt):").grid(row=1, column=0, padx=6, pady=6, sticky="w")
        self.token_var = tk.StringVar(value=str(self.cfg.get("token", "")))
        ttk.Entry(net_frame, textvariable=self.token_var, width=18, show="*").grid(row=1, column=1, padx=4, pady=6)

        net_btns = ttk.Frame(net_frame)
        net_btns.grid(row=1, column=2, columnspan=4, padx=4, pady=6, sticky="w")
        ttk.Button(net_btns, text="Detect My IP", command=self._refresh_ip).pack(side="left", padx=3)
        ttk.Button(net_btns, text="Discover LAN Hub", command=self.discover_lan).pack(side="left", padx=3)
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

        ttk.Button(row2, text="Test Connection (Ping + /health)", command=self.run_diagnostics).pack(
            side="left", padx=4
        )
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
        ttk.Button(r_setup, text="Install Repo Hooks & Templates", command=self.attach_repo).pack(
            side="left", padx=4
        )

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
        self.log(f"Unified Panel ready. Root: {ROOT_DIR}")

    def _server_url(self) -> str:
        ip = self.host_ip_var.get().strip() or "127.0.0.1"
        port = self.port_var.get().strip() or "8765"
        return f"http://{ip}:{port}"

    def _save_current_config(self) -> None:
        port_raw = self.port_var.get().strip() or "8765"
        try:
            port_num = int(port_raw)
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
        while not self.log_queue.empty():
            self.log_box.insert("end", self.log_queue.get_nowait())
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
        self._save_current_config()
        port = self.port_var.get().strip() or "8765"
        cmd = [sys.executable, "-m", "agsync.cli", "serve", "--host", "0.0.0.0", "--port", port]
        if self.token_var.get().strip():
            cmd.extend(["--token", self.token_var.get().strip()])
        if self.discover_chk_var.get():
            cmd.append("--discover")

        self.log(f"Starting hub server: {' '.join(cmd)}")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.server_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=flags,
        )
        self.status_lbl.config(text=f"HOST RUNNING :{port}", bg="#5cb85c")
        self._apply_mode_ui()
        threading.Thread(target=self._read_server_output, daemon=True).start()

    def _read_server_output(self) -> None:
        proc = self.server_proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self.log_queue.put(line)
        code = proc.wait()
        self.log(f"Hub server exited with code {code}.")
        self.root.after(0, self._apply_mode_ui)

    def stop_server(self) -> None:
        if self.server_proc is not None and self.server_proc.poll() is None:
            self.log("Stopping hub server...")
            self.server_proc.terminate()

    def open_firewall(self) -> None:
        port = self.port_var.get().strip() or "8765"
        if os.name != "nt":
            self.log("Firewall helper is for Windows. Ensure TCP/UDP ports are open on your OS.")
            return
        ps_cmd = (
            f"New-NetFirewallRule -DisplayName 'agsync LAN Coordination Hub' "
            f"-Direction Inbound -Protocol TCP -LocalPort {port} -Profile Private -Action Allow -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -DisplayName 'agsync LAN Discovery' "
            f"-Direction Inbound -Protocol UDP -LocalPort {DEFAULT_DISCOVERY_PORT} -Profile Private -Action Allow -ErrorAction SilentlyContinue"
        )
        self.log(f"Requesting Administrator permission to open TCP {port} & UDP {DEFAULT_DISCOVERY_PORT}...")
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
        self._save_current_config()
        ip = self.host_ip_var.get().strip()
        port = self.port_var.get().strip() or "8765"
        threading.Thread(target=self._diag_worker, args=(ip, port), daemon=True).start()

    def _diag_worker(self, ip: str, port: str) -> None:
        is_host = self.mode_var.get() == "host"
        self.log(f"--- Diagnostics ({'HOST' if is_host else 'PEER'} -> {ip}:{port}) ---")
        if not is_host:
            ok_ping, _ = ping_host(ip)
            if ok_ping:
                self.log("[1/2] PING OK: Host IP is reachable on LAN.")
            else:
                self.log("[1/2] PING FAILED: Cannot reach Host IP.")
                self.log("      -> Check: Same Wi-Fi/LAN? VPN 'Allow LAN' / 'Excluded routes' (192.168.1.0/24)?")
                self.root.after(0, lambda: self.status_lbl.config(text="PING FAILED", bg="#d9534f"))
                return

        url = f"http://127.0.0.1:{port}" if is_host else f"http://{ip}:{port}"
        ok_http, msg = check_health(url)
        if ok_http:
            self.log(f"[2/2] HEALTH OK: {msg}")
            self.root.after(0, lambda: self.status_lbl.config(text="CONNECTED", bg="#5cb85c"))
        else:
            self.log(f"[2/2] HEALTH FAILED ({url}/health): {msg}")
            self.log("      -> Check: 1) Is Hub Server started on Host? 2) Is Windows Firewall TCP 8765 open?")
            self.root.after(0, lambda: self.status_lbl.config(text="HTTP FAILED", bg="#f0ad4e"))

    def check_status(self) -> None:
        ip = "127.0.0.1" if self.mode_var.get() == "host" else self.host_ip_var.get().strip()
        port = self.port_var.get().strip() or "8765"
        url = f"http://{ip}:{port}"
        threading.Thread(target=self._status_worker, args=(url,), daemon=True).start()

    def _status_worker(self, url: str) -> None:
        cmd = [sys.executable, "-m", "agsync.cli", "status", "--server", url]
        tok = self.token_var.get().strip()
        if tok:
            cmd.extend(["--token", tok])
        res = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=5)
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
        url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}" if self.mode_var.get() == "host" else self._server_url()
        if self.mode_var.get() == "host":
            # For host, using LAN IP or 127.0.0.1 works locally; prefer 127.0.0.1 so IP changes don't break local Antigravity
            url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}"
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
        url = f"http://127.0.0.1:{self.port_var.get().strip() or '8765'}" if self.mode_var.get() == "host" else self._server_url()
        agent = self.agent_var.get().strip() or default_agent_id()
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
        tok = self.token_var.get().strip()
        if tok:
            cmd.extend(["--token", tok])
        res = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            self.log(f"Repo hooks & templates installed:\n{res.stdout.strip()}")
            messagebox.showinfo("Repo Attached", f"Hooks & templates installed in:\n{repo}")
        else:
            err = res.stderr.strip() or res.stdout.strip()
            self.log(f"Repo hook install FAILED: {err}")
            messagebox.showerror("Repo Setup Failed", err)

    def _on_close(self) -> None:
        self._save_current_config()
        if self.server_proc is not None and self.server_proc.poll() is None:
            if messagebox.askyesno("Exit", "Hub server is still running. Stop it and exit?"):
                self.stop_server()
            else:
                return
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="agsync Unified XAMPP-style Control Panel")
    parser.add_argument("--mode", choices=["host", "peer"], default=None)
    args = parser.parse_args()

    root = tk.Tk()
    AgsyncControlPanel(root, initial_mode=args.mode)
    root.mainloop()


if __name__ == "__main__":
    main()
