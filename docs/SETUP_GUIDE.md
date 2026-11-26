# Step-by-Step Setup Guide: Multi-Machine & Multi-Agent Coordination

[English](SETUP_GUIDE.md) | [فارسی](SETUP_GUIDE.fa.md)

This guide walks two or more colleagues (**Machine A** = LAN Hub Host, **Machine B / Peers** = Clients) through setting up `agsync` from scratch for **any MCP/CLI-compatible coding agent** (Antigravity, Claude Code, Cursor, Windsurf, Codex, Gemini CLI, etc.).

> **Placeholders used below**:
> - `<MACHINE_A_IP>`: Machine A's LAN IPv4 address (e.g., `192.168.1.10`).
> - `<AGSYNC_DIR>`: Local path to `antigravity_multi_agent_sync`.
> - `<PROJECT_DIR>`: Local path to the shared project repository clone on each machine.
> - `<SHARED_TOKEN>`: Shared Bearer secret (required when binding `0.0.0.0`).

---

## Step 1: Verify Prerequisites on Each Machine

```powershell
python --version   # Python 3.12+ (zero third-party dependencies required)
git --version      # Recommended: Git 2.38+ (checked by `agsync doctor`)
```

Clone or copy `antigravity_multi_agent_sync` onto each machine.

---

## Step 2: Find Machine A's LAN IP Address

On **Machine A**:

- **Windows (PowerShell)**:
  ```powershell
  Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL|Tun|TAP' -and $_.IPAddress -notlike '169.254.*' } | Select-Object InterfaceAlias, IPAddress
  ```
- **macOS / Linux**:
  ```bash
  ip -4 addr show   # or: ipconfig getifaddr en0 (macOS)
  ```

---

## Step 3: Open Windows Firewall on Machine A (If Host Runs Windows)

Ensure your Wi-Fi/Ethernet network is set to **Private** in Windows Settings, then run in an **Administrator PowerShell** (or click **Open Firewall (Admin)** in `Start_Sync_Panel.bat`):

```powershell
New-NetFirewallRule -DisplayName "agsync LAN Coordination Hub" -Direction Inbound -Protocol TCP -LocalPort 8765 -Profile Private -Action Allow
New-NetFirewallRule -DisplayName "agsync LAN Discovery" -Direction Inbound -Protocol UDP -LocalPort 8766 -Profile Private -Action Allow
```

---

## Step 4: Set Up Git Code Isolation (Cloud Remote OR 100% LAN-Only)

Give **each agent/machine its own local Git clone (or `git worktree`) and feature branch**. Never share a `.git` folder or working directory over SMB/OneDrive/Syncthing.

### Option A: Shared Git Remote (GitHub / GitLab / Gitea)
- **Machine A**: `cd <PROJECT_DIR> && git checkout -b agent-a/work`
- **Machine B**: `git clone <YOUR_REPO_URL> <PROJECT_DIR> && cd <PROJECT_DIR> && git checkout -b agent-b/work`

### Option B: 100% Offline LAN-Only Git (No Internet Needed)
On **Machine A**:
```powershell
git init --bare -b main C:\git-repos\my_app.git
git clone C:\git-repos\my_app.git C:\projects\my_app
cd C:\projects\my_app
git commit --allow-empty -m "chore: initial repo"
git push -u origin HEAD:main
git checkout -b agent-a/work
```
On **Machine B** (via OpenSSH on Machine A):
```powershell
git clone -b main ssh://<USER_A>@<MACHINE_A_IP>/C:/git-repos/my_app.git C:\projects\my_app
cd C:\projects\my_app
git checkout -b agent-b/work
```

---

## Step 5: Start the `agsync` Hub Server on Machine A

> **Security rule (Phase 1)**: Binding a non-loopback address (`0.0.0.0`) requires a Bearer token (`AGSYNC_TOKEN`, `--token`, or `--auto-token`, unless explicitly passing `--allow-insecure-lan`). The SQLite WAL database defaults to `~/.agsync/state.db`.

```powershell
cd <AGSYNC_DIR>
$env:AGSYNC_TOKEN = "<SHARED_TOKEN>"
python -m agsync.cli serve --host 0.0.0.0 --port 8765 --discover
```

*(For quick local-only testing on one machine, `python -m agsync.cli serve --host 127.0.0.1 --port 8765` works without a token.)*

### Verify Connectivity from Both Machines
On **Machine A**: `Invoke-RestMethod http://127.0.0.1:8765/health`  
On **Machine B**: `Invoke-RestMethod http://<MACHINE_A_IP>:8765/health`  
*(Note: `agsync` CLI/client automatically bypasses system proxies like v2rayN/Clash/Happ for loopback and private LAN IPs.)*

---

## Step 6: Connect Your AI Agent(s) via MCP (or CLI)

`agsync` exposes 13 `sync_*` tools via **Streamable HTTP (`POST /mcp`, MCP `2025-03-26`)** and **Stdio JSON-RPC (`python -m agsync.mcp_bridge`)**.

### Option 1: Antigravity (`~/.gemini/config/mcp_config.json`)
Run `setup-mcp` on each machine (automatically merges atomically and preserves other servers with a `.bak` backup):
```powershell
# Machine A
python -m agsync.cli setup-mcp --server http://127.0.0.1:8765 --agent machine-a --token "<SHARED_TOKEN>"

# Machine B
python -m agsync.cli setup-mcp --server http://<MACHINE_A_IP>:8765 --agent machine-b --token "<SHARED_TOKEN>"
```

### Option 2: Claude Code / Cursor / Windsurf / Other MCP Clients
Add either **Streamable HTTP** or **Stdio Bridge** to your client's MCP config (e.g., `.mcp.json` for Claude Code or `~/.cursor/mcp.json` for Cursor):

**Streamable HTTP**:
```json
{
  "mcpServers": {
    "agsync": {
      "url": "http://<MACHINE_A_IP>:8765/mcp?agent=claude-b",
      "headers": {
        "X-Agent-Id": "claude-b",
        "Authorization": "Bearer <SHARED_TOKEN>"
      }
    }
  }
}
```

**Stdio Bridge** (for clients preferring local command transport):
```json
{
  "mcpServers": {
    "agsync": {
      "command": "python",
      "args": [
        "-m",
        "agsync.mcp_bridge",
        "--server",
        "http://<MACHINE_A_IP>:8765",
        "--agent",
        "claude-b"
      ],
      "env": {
        "PYTHONPATH": "<AGSYNC_DIR>",
        "AGSYNC_TOKEN": "<SHARED_TOKEN>"
      }
    }
  }
}
```

### Option 3: CLI-Only Agents (Codex, Shell Scripts, etc.)
Any agent with shell access can use `python -m agsync.cli` directly:
`register`, `status`, `inbox --unread --ack`, `task create/claim/update`, `lock`, `unlock`, `check`, `send`, `context set/get`.

---

## Step 7: Install Rules, Skills, and Lease Guards in `<PROJECT_DIR>`

Run on each machine/checkout (works cleanly in regular clones, `git worktree` checkouts via `--git-common-dir`, and custom `core.hooksPath`):

```powershell
# Machine A
python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://127.0.0.1:8765" --agent machine-a --with-antigravity-hook --with-templates

# Machine B
python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://<MACHINE_A_IP>:8765" --agent machine-b --with-antigravity-hook --with-templates
```

What this sets up safely:
- Installs `.git/hooks/pre-commit` (in `<git-common-dir>/hooks` or `core.hooksPath`) without hardcoding transient `.venv` paths.
- Atomically merges `agsync-lease-guard` into `<PROJECT_DIR>/.agents/hooks.json` (creating `.bak` and preserving existing user hooks).
- Copies `AGENTS.md` and `.agents/skills/sync-*` if missing, stores machine-local config under `.agsync/`, and adds `.agsync/` and `.agsync-agent` to `<git-common-dir>/info/exclude`.
- `hook_guard` automatically protects `.agsync/**`, `.agsync-agent`, and `.agents/hooks.json` from agent edits.

---

## Step 8: Daily Collaborative Workflow

1. **Start & Register**: Each agent calls `sync_register(agent_id=..., branch=...)`, `sync_status()`, and `sync_poll_messages(unread_only=true, auto_ack=true)`.
2. **Split Tasks**: Create scoped tasks (`sync_create_task`) and atomically claim (`sync_claim_task`). Duplicate claims return `409 Conflict`.
3. **Share Contracts**: Publish ports/schemas via `sync_publish_context` and notify peers via `sync_send_message`.
4. **Lease Before Edit**: Call `sync_acquire_lease(resource=..., scope="file"|"dir"|"symbol")` before editing. Overlapping peer edits are blocked (`409` + hook guard).
5. **Commit & Handoff**: Commit on your own branch, update task status (`sync_update_task`), release leases (`sync_release_lease(release_all=true)`), and send a `handoff` message.

---

## Step 9: Run `agsync doctor` & Smoke Checks

```powershell
cd <AGSYNC_DIR>

# Check Python, Git >= 2.38, worktree common-dir, BOM/UTF-16, mcp_config url/serverUrl, proxy, firewall, and hook canary
python -m agsync.cli doctor --repo "<PROJECT_DIR>"

# Full automated test suite (38 tests)
python -m unittest discover -s tests -v

# Standalone end-to-end smoke check
python scripts/smoke_check.py --spawn-local
```

---

## Troubleshooting

- **Server refuses to bind `0.0.0.0`**: Provide `AGSYNC_TOKEN` / `--token` / `--auto-token` (or `--allow-insecure-lan` for trusted private setups).
- **Machine B cannot reach `http://<MACHINE_A_IP>:8765/health`**:
  - Run `agsync doctor` on Machine A and check if Windows Network Category is `Public` instead of `Private`.
  - Ensure Firewall rules for TCP `8765` and UDP `8766` exist.
- **`401 Unauthorized` / `403 Forbidden` / `415 Unsupported Media Type`**:
  - Verify `Authorization: Bearer <SHARED_TOKEN>` matches the hub token.
  - Ensure mutating HTTP requests send `Content-Type: application/json` and valid local/IP `Host` headers.
- **`400` on `GET /messages?auto_ack=1`**:
  - `GET /messages` is strictly read-only; acknowledge messages with `POST /messages/ack` (or `SyncClient.poll_messages(..., auto_ack=True)`).
- **PowerShell 5.1 JSON quoting in `agsync context set`**:
  - Escape inner double quotes (`'{\"port\":8765}'`) or pass `--raw`.
