# `agsync` — Universal Multi-Agent & Multi-IDE Sync

[English](README.md) | [فارسی](README.fa.md)

[![CI](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml/badge.svg)](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml)

Zero-dependency **Python 3.12 stdlib** LAN coordination hub, MCP server, CLI, GUI Control Panel, and Git/IDE lease guards enabling **any AI coding agents** — **Antigravity, Claude Code, Cursor, Windsurf, Codex, Gemini CLI, or custom CLI scripts** — across adjacent machines or parallel workspaces to collaborate on one codebase without overwrites or lock conflicts.

---

## Why `agsync`?

Modern AI coding runtimes execute locally on each developer's machine and have **no built-in cross-machine agent mesh, distributed file locking, or shared task board**. Sharing raw working folders over SMB, OneDrive, or Syncthing causes silent write races, corrupted `.git/index.lock` files, and broken `node_modules` / `.venv` caches.

**Hybrid Architecture (Code Plane + Control Plane)**:
- **Code Plane (`Git`)**: Every agent/machine keeps its own Git clone (or local worktree) on its own feature branch (`agent-a/<task>`, `agent-b/<task>`). Works with GitHub/GitLab or 100% offline over LAN.
- **Control Plane (`agsync`)**: A lightweight Python 3.12 stdlib coordination server (`SQLite WAL` + `PRAGMA user_version`) exposing **Streamable HTTP (`/mcp`)**, **Stdio MCP (`agsync.mcp_bridge`)**, and **REST JSON**, backed by **Git `pre-commit`** and **IDE `PreToolUse` guards**.

---

## Compatible Agents & IDEs

Any client that speaks **MCP** (Streamable HTTP or Stdio), **HTTP REST**, or **CLI** works out of the box:

| Client / Agent | How It Connects | Status |
|---|---|---|
| **Antigravity** | Streamable HTTP (`/mcp`) or Stdio (`agsync.mcp_bridge`) + `.agents/hooks.json` | Supported (auto-setup via `agsync setup-mcp`) |
| **Claude Code / Cursor / Windsurf / Cline** | Standard MCP (`url` or `command`: `python -m agsync.mcp_bridge`) + `AGENTS.md` | Supported via standard MCP JSON |
| **Codex / Gemini CLI / Custom Scripts** | Stdio MCP or `python -m agsync.cli` / REST API | Supported |

*(Phase 2 adds dedicated `agsync hook-proxy --host <ide>` adapters for Claude Code & Cursor native hooks plus `git worktree` orchestration.)*

---

## Core Capabilities (`v0.1.0` — Phase 1)

- **Exclusive TTL Leases & Fencing Tokens**: Hierarchical locks (`file`, `dir`, `symbol`) with monotonic `fencing_token`, Windows case-insensitive normalization, and Junction-safe logical path checks.
- **Atomic Task Splitting & Shared Context**: `409 Conflict` on duplicate task claims, CAS-versioned shared KV (`expected_version`) for API contracts/ports, and direct/broadcast messaging with explicit read cursors (`POST /messages/ack`).
- **Security & Concurrency Hardening**:
  - `Content-Type: application/json` enforcement (`415`) and `Host`/`Origin` validation (`403`) against CSRF and DNS-rebinding.
  - Strictly read-only `GET /messages` (cursor updates require `POST /messages/ack`).
  - Required Bearer token (`--token`, `--auto-token`, or explicit `--allow-insecure-lan`) on non-loopback binds (`0.0.0.0`).
  - Windows `SO_EXCLUSIVEADDRUSE` + OS-locked `<db>.pid` lockfile preventing dual zombie hubs.
  - Automatic `ProxyHandler({})` bypass for loopback and private LAN addresses (preventing v2rayN/Clash/Happ proxy hijacking).
- **Git Worktree, BOM/UTF-16 & Protected-Path Guardrails**:
  - `install-hooks` uses `git rev-parse --git-common-dir` and `--git-path hooks` (supporting `git worktree` and `core.hooksPath`), avoids hardcoding transient venv paths in `pre-commit`, and performs atomic (`.bak` + `os.replace`) non-destructive JSON merges.
  - `hook_guard` handles UTF-8 BOM / PowerShell UTF-16LE configs and blocks agent edits to `.agsync/**`, `.agsync-agent`, and `.agents/hooks.json`.
- **Built-In Diagnostics & Live Dashboard**: `agsync doctor` (CLI + Control Panel button) and live web dashboard at `GET /dashboard`.

### Phase 2 Capabilities (`feat/phase2-multi-ide-remote`)
- **Multi-IDE Hook Proxy (`agsync hook-proxy --host antigravity|claude|cursor`)**: Translates Antigravity (`ask`/`deny`), Claude Code (`tool_name`/`tool_input`, `exit 2` + `stderr`), and Cursor (`preToolUse` deduplication), with `--record` sampling and timeout-guarded user hook chaining.
- **Opt-In `git worktree` (`agsync worktree create|list|remove`)**: Creates sibling `../<repo>-wt-<n>` worktrees with explicit `.agsync-agent` identity and admin-free Windows Directory Junctions for `node_modules`/`.venv`.
- **Remote Multi-Location Onboarding**: `agsync invite` / `agsync join` (HMAC-signed single-use nonces issuing per-`agent_id` Bearer tokens) + Cloudflare Tunnel (`--protocol http2`) + GUI controls in `scripts/control_panel.py`.
- **Real-Time Events & Live Dashboard**: SQLite `events` ring buffer (1000 records + `boot_id`), `sync_wait` MCP tool (<=15s `Condition` wait), `GET /events` long-polling, and Live Activity Feed + Browser Notifications in `/dashboard`.

### Phase 3 Capabilities (`v0.3.0`)
- **Non-Destructive Merge Pre-Check (`agsync merge-check`)**: Runs `git merge-tree --write-tree` without modifying the working tree to detect branch conflicts early.
- **External Shell Hooks**: User-defined commands in `agsync.config.json` (`hooks.on_lock`, `hooks.on_task_done`, `hooks.pre_merge`).
- **Explicit Offline Read-Only Mode**: `SyncClient.ping_or_offline()` transitions unreachable clients into `offline_read_only` to block stale lock acquisitions.
- **Packaging**: Ready for `pip install .` / `uvx` via `pyproject.toml` plus `scripts/build_exe.py` for standalone Windows `.exe` builds.

---

## Quickstart

### Option 1: GUI Control Panel (Recommended on Windows)
1. Launch `Start_Sync_Panel.bat` (or `python scripts/control_panel.py`).
2. **Host (Machine A)**: Select `HOST (Hub)`, set `Agent ID`, click **Generate Token** (or let **Start Hub Server** auto-generate one), click **Open Firewall (Admin)** if needed on LAN, and click **Start Hub Server**.
3. **Peer (Machine B)**: Select `PEER (Client)`, enter Host LAN IP (or click **Discover LAN Hub**), paste the shared token, and click **Test Connection (TCP + /health)**.
4. Connect your IDE's MCP (`Connect Antigravity MCP` for `~/.gemini/config/mcp_config.json`, or copy the MCP snippet below for Claude Code / Cursor) and click **Install Repo Hooks & Templates**.

### Option 2: CLI (Any OS / Any Agent)
1. **Start the Hub** (state DB defaults to `~/.agsync/state.db`):
   ```powershell
   $env:AGSYNC_TOKEN = "your-shared-secret"
   python -m agsync.cli serve --host 0.0.0.0 --port 8765
   ```
   *(For local-only testing on `127.0.0.1`, no token is required.)*
2. **Connect any MCP-compatible agent** (using `templates/mcp_config.http.json` or `templates/mcp_config.stdio.json`):
   ```json
   {
     "mcpServers": {
       "agsync": {
         "url": "http://127.0.0.1:8765/mcp?agent=agent-a",
         "headers": {
           "X-Agent-Id": "agent-a",
           "Authorization": "Bearer your-shared-secret"
         }
       }
     }
   }
   ```
3. **Install Git guards & run diagnostics**:
   ```powershell
   python -m agsync.cli install-hooks --repo /path/to/project --server http://127.0.0.1:8765 --agent agent-a --with-antigravity-hook --with-templates
   python -m agsync.cli doctor --repo /path/to/project
   ```

---

## Verification

```powershell
python -m unittest discover -s tests -v
python scripts/smoke_check.py --spawn-local
python -m agsync.cli doctor --repo .
```

---

## Documentation Index

| Topic | English | فارسی |
|---|---|---|
| **Project Overview** | [README.md](README.md) | [README.fa.md](README.fa.md) / [docs/OVERVIEW.fa.md](docs/OVERVIEW.fa.md) |
| **Step-by-Step Setup Guide** | [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) | [docs/SETUP_GUIDE.fa.md](docs/SETUP_GUIDE.fa.md) |
| **Architecture & Trade-Offs** | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | [docs/ARCHITECTURE.fa.md](docs/ARCHITECTURE.fa.md) |
| **Roadmap (Phases 1–3)** | — | [docs/ROADMAP.fa.md](docs/ROADMAP.fa.md) |
| **Evaluation Rubric** | [docs/RUBRIC.md](docs/RUBRIC.md) | — |
