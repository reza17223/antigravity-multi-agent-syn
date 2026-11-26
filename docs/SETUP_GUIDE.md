# Step-by-Step Setup Guide: Two-Machine Antigravity Coordination

This guide walks two colleagues on adjacent machines (**Machine A** = LAN Hub Host, **Machine B** = Peer) through setting up real-time Antigravity coordination and conflict-free parallel editing from scratch.

> **Placeholders used below**:
> - `<MACHINE_A_IP>`: Machine A's LAN IPv4 address (for example `192.168.1.10`, discovered in Step 2).
> - `<AGSYNC_DIR>`: Local path where you cloned/placed `antigravity_multi_agent_sync` (for example `C:\Users\Alice\teamwork_projects\antigravity_multi_agent_sync`).
> - `<PROJECT_DIR>`: Local path of the project repository both colleagues are building together (for example `C:\Users\Alice\projects\my_app`).

---

## Step 1: Verify Prerequisites on Both Machines

Run on **Machine A** and **Machine B** (PowerShell on Windows, Terminal on macOS/Linux):

```powershell
python --version   # Must be Python 3.12+ (zero pip installs required)
git --version      # Recommended: Git 2.30+
```

Clone or copy `antigravity_multi_agent_sync` onto **both** machines (or at least Machine A if Machine B connects purely over Streamable HTTP `/mcp` — though having `agsync` on both machines enables local Git `pre-commit` and `PreToolUse` hooks too).

---

## Step 2: Find Machine A's LAN IP Address

On **Machine A**:

- **Windows (PowerShell)**:
  ```powershell
  Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notlike '169.254.*' } | Select-Object InterfaceAlias, IPAddress
  ```
- **macOS / Linux**:
  ```bash
  ip -4 addr show   # or: ipconfig getifaddr en0 (macOS)
  ```

Record the LAN IPv4 address (e.g., `192.168.1.10`) as `<MACHINE_A_IP>`.

---

## Step 3: Open Windows Firewall on Machine A (Port 8765)

If **Machine A** runs Windows, allow incoming TCP traffic on port `8765` on your Private/LAN network profile (run in an **Administrator PowerShell**):

```powershell
New-NetFirewallRule -DisplayName "agsync LAN Coordination Hub" -Direction Inbound -Protocol TCP -LocalPort 8765 -Profile Private -Action Allow
```

*(Equivalent `netsh` command if preferred)*:
```powershell
netsh advfirewall firewall add rule name="agsync LAN Coordination Hub" dir=in action=allow protocol=TCP localport=8765 profile=private
```

> **Note**: If your Wi-Fi/Ethernet network is marked "Public" in Windows Settings, either switch it to **Private** (recommended for trusted office/home LANs) or omit `-Profile Private` while enabling `--token` authentication in Step 5.

---

## Step 4: Set Up Git Code Isolation (Cloud Remote OR 100% LAN-Only)

Give **each machine its own local Git clone and its own feature branch**. Never share a `.git` folder or working directory over SMB/OneDrive/Syncthing.

### Option A: Shared Git Remote (GitHub / GitLab / Gitea)
On **Machine A**:
```powershell
cd C:\path\to\my_app
git checkout -b agent-a/work
```
On **Machine B**:
```powershell
git clone <YOUR_REPO_URL> C:\path\to\my_app
cd C:\path\to\my_app
git checkout -b agent-b/work
```

### Option B: 100% Offline LAN-Only Git (No GitHub Needed)
If you want zero internet dependency, host a bare Git repo on **Machine A**:
```powershell
# On Machine A (once):
git init --bare -b main C:\git-repos\my_app.git
git clone C:\git-repos\my_app.git C:\projects\my_app
cd C:\projects\my_app
git commit --allow-empty -m "chore: initial repo"
git push -u origin HEAD:main
git checkout -b agent-a/work
```
On **Machine B** (assuming OpenSSH Server is enabled on Machine A, or shared read/write bare repo path):
```powershell
git clone -b main ssh://<USER_A>@<MACHINE_A_IP>/C:/git-repos/my_app.git C:\projects\my_app
cd C:\projects\my_app
git checkout -b agent-b/work
```

---

## Step 5: Start the `agsync` LAN Coordination Server on Machine A

On **Machine A**, inside `<AGSYNC_DIR>`:

```powershell
cd <AGSYNC_DIR>
python -m agsync.cli serve --host 0.0.0.0 --port 8765 --db .agsync/state.db
```

*(Optional shared-secret authentication — recommended on shared office networks)*:
```powershell
python -m agsync.cli serve --host 0.0.0.0 --port 8765 --db .agsync/state.db --token YOUR_SHARED_SECRET
```

### Verify Connectivity from Both Machines
On **Machine A**:
```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```
On **Machine B** (replace `<MACHINE_A_IP>`):
```powershell
Invoke-RestMethod http://<MACHINE_A_IP>:8765/health
```
Both should return `status: ok`, `online_agents: 0`, `active_leases: 0`.

---

## Step 6: Configure Antigravity MCP on Both Machines

Antigravity reads global MCP servers from `~/.gemini/config/mcp_config.json` (`C:\Users\<USER>\.gemini\config\mcp_config.json` on Windows).

Pick **either** Streamable HTTP (simplest, no local bridge process needed on Machine B) **or** Stdio Bridge (automatically binds `--agent` and env vars cleanly).

### Option 1: Streamable HTTP (`/mcp`, Antigravity Native)
> **Important**: Antigravity supports Streamable HTTP (`/mcp`). Do **not** use legacy `/sse` URLs.

Merge into `~/.gemini/config/mcp_config.json` on **Machine A** (use `127.0.0.1` and `machine-a`) and **Machine B** (use `<MACHINE_A_IP>` and `machine-b`):

```json
{
  "mcpServers": {
    "agsync": {
      "url": "http://<MACHINE_A_IP>:8765/mcp?agent=machine-b",
      "headers": {
        "X-Agent-Id": "machine-b"
      }
    }
  }
}
```
*(If you enabled `--token YOUR_SHARED_SECRET` on the server, add `"Authorization": "Bearer YOUR_SHARED_SECRET"` inside `"headers"`.)*

### Option 2: Stdio MCP Bridge (`agsync.mcp_bridge`)
Copy `templates/mcp_config.stdio.json` into `~/.gemini/config/mcp_config.json` on each machine, updating `PYTHONPATH`, `--server`, and `--agent` (`machine-a` on Machine A with `http://127.0.0.1:8765`, `machine-b` on Machine B with `http://<MACHINE_A_IP>:8765`):

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
        "machine-b"
      ],
      "env": {
        "PYTHONPATH": "<AGSYNC_DIR>",
        "AGSYNC_TOKEN": ""
      }
    }
  }
}
```

### Activate & Verify MCP in Antigravity
1. In Antigravity on each machine, open **Additional Options (`...`) > MCP Servers** (or restart Antigravity) and confirm **`agsync`** shows 13 tools (`sync_status`, `sync_register`, `sync_acquire_lease`, etc.).
2. Note that Antigravity permission grants (`~/.gemini/config/projects/<uuid>.json`) are machine-local: approve `agsync` tool calls once on Machine A and once on Machine B (or allow them in project settings).

---

## Step 7: Install Rules, Skills, and Optional Enforcement Hooks in Your Project Repo

1. **Copy `AGENTS.md` and Skills into `<PROJECT_DIR>`** (and commit them to your shared repo so both machines get them):
   ```powershell
   Copy-Item "<AGSYNC_DIR>\templates\AGENTS.md" "<PROJECT_DIR>\AGENTS.md"
   New-Item -ItemType Directory -Force "<PROJECT_DIR>\.agents\skills" | Out-Null
   Copy-Item -Recurse -Force "<AGSYNC_DIR>\templates\.agents\skills\*" "<PROJECT_DIR>\.agents\skills\"
   ```
   This enables Antigravity's modern skills (`/sync-start`, `/sync-check`, `/sync-handoff`) and always-on `AGENTS.md` rules.

2. **Install Hard Lease Guards (Git `pre-commit` + Antigravity `PreToolUse` hook)** locally on each machine:
   - On **Machine A**:
     ```powershell
     cd <AGSYNC_DIR>
     python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://127.0.0.1:8765" --agent machine-a --with-antigravity-hook
     ```
   - On **Machine B**:
     ```powershell
     cd <AGSYNC_DIR>
     python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://<MACHINE_A_IP>:8765" --agent machine-b --with-antigravity-hook
     ```
   What this does cleanly:
   - Writes `.git/hooks/pre-commit` calling `agsync.cli guard --staged` with exact absolute Python and script paths (so it works regardless of CWD or `PYTHONPATH`).
   - Saves local identity in `<PROJECT_DIR>/.agsync/` and adds `.agsync/` (and `.agents/hooks.json` if not tracked in Git) to `.git/info/exclude` (never polluted in `git status` and machine-local hook paths aren't committed).
   - Writes `<PROJECT_DIR>/.agents/hooks.json` so Antigravity's `PreToolUse` hook blocks `edit_file` / `write_file` / `replace_file_content` immediately if the peer holds an active lease on that file.

---

## Step 8: Daily Collaborative Workflow

1. **Both colleagues start their Antigravity sessions**:
   - Prompt Agent A: *"Run `/sync-start` as `machine-a` on branch `agent-a/work` and split tasks with `machine-b`."*
   - Prompt Agent B: *"Run `/sync-start` as `machine-b` on branch `agent-b/work` and check for tasks."*
2. **Task splitting**:
   - Agent A calls `sync_create_task(title="Implement /auth endpoint", scope_paths=["src/api/auth.py"])` and `sync_create_task(title="Build login UI", scope_paths=["src/ui/login.tsx"])`.
   - Agent A claims task 1 (`sync_claim_task`), Agent B claims task 2. If both race for the same task, `agsync` guarantees only one wins and the other gets a clean conflict error.
3. **Contract sharing**:
   - Agent A publishes the auth payload schema via `sync_publish_context(key="contract:auth_api", value={"endpoint": "/api/login", "fields": ["email", "password"]})` and pings Agent B with `sync_send_message`.
4. **Conflict-free parallel editing**:
   - Before editing `src/api/auth.py`, Agent A calls `sync_acquire_lease(resource="src/api/auth.py")`.
   - If Agent B accidentally tries to lease or edit `src/api/auth.py` (or parent dir `src/api`), `sync_acquire_lease` and the `PreToolUse` hook reject it with the exact holder, reason, and expiry.
5. **Commit & Handoff**:
   - Each agent tests and commits to its own branch, runs `/sync-handoff` (`sync_update_task`, `sync_release_lease(release_all=true)`, `sync_send_message(msg_type="handoff")`), and the two colleagues push/merge their branches cleanly in Git.

---

## Troubleshooting

- **Machine B cannot reach `http://<MACHINE_A_IP>:8765/health`**:
  - Confirm `agsync` is bound to `--host 0.0.0.0` (not `127.0.0.1`).
  - Confirm Windows Firewall rule for TCP `8765` exists and your LAN profile is `Private`.
- **MCP tools not visible in Antigravity**:
  - Verify `~/.gemini/config/mcp_config.json` is valid JSON (`python -m json.tool ~/.gemini/config/mcp_config.json`).
  - Ensure you used `"url": "http://.../mcp"` (Streamable HTTP), **not** `/sse`.
  - If using `mcp_config.stdio.json`, verify `PYTHONPATH` points to `<AGSYNC_DIR>`.
- **Hook fails on Windows with `ModuleNotFoundError: No module named 'agsync'`**:
  - Antigravity executes `hooks.json` with CWD = `.agents/`. Re-run `python -m agsync.cli install-hooks ... --with-antigravity-hook` so `.agents/hooks.json` embeds the full paths to `python.exe` and `hook_guard.py`.
- **Peer machine crashed while holding a lock**:
  - Leases auto-expire after their TTL (default 600s / 10 min). Wait for expiry or restart with a clean task split.
- **401 Unauthorized**:
  - Match `--token` on the server with `Authorization: Bearer <token>` (HTTP MCP), `AGSYNC_TOKEN` (Stdio/CLI), or `<PROJECT_DIR>/.agsync/token`.
- **Windows PowerShell 5.1 JSON quoting in `agsync context set`**:
  - Windows PowerShell 5.1 strips unescaped double quotes inside single-quoted arguments passed to native executables (`python.exe`). When publishing JSON via CLI, escape inner quotes (e.g., `python -m agsync.cli context set contract:v1 '{\"port\":8765}'`) or pass `--raw` for plain strings.

---

## Fresh Setup & Smoke Check

Verify everything works cleanly out of the box with zero third-party packages:

1. **Run the full automated `unittest` suite**:
   ```powershell
   cd <AGSYNC_DIR>
   python -m unittest discover -s tests -v
   ```
2. **Run the standalone smoke check (local isolated server)**:
   ```powershell
   python scripts/smoke_check.py --spawn-local
   ```
3. **Run the smoke check against your live Machine A LAN server** (from Machine B!):
   ```powershell
   python scripts/smoke_check.py --server http://<MACHINE_A_IP>:8765
   ```
