# Antigravity Multi-Agent Sync (`agsync`)

[English](README.md) | [فارسی](README.fa.md)

Zero-dependency **Python 3.12 stdlib** LAN coordination server, MCP bridge, CLI, Windows/cross-platform Control Panel, and Git/IDE lease guards enabling two or more developers running **Antigravity** on adjacent machines to collaborate concurrently on a shared repository without silent overwrites or lock conflicts.

---

## Why This Exists

Each Antigravity installation runs locally (`language_server.exe` bound to `localhost`, machine-local `~/.gemini/antigravity/` and `~/.gemini/config/`). Antigravity has **no built-in cross-machine agent mesh, distributed file locking, or shared task board**.

**Hybrid Architecture**:
- **Code Plane (Git)**: Each machine keeps its own local Git checkout on its own feature branch (`agent-a/<task>`, `agent-b/<task>`), avoiding `.git/index.lock` races and build-artifact collisions (`node_modules`, `.venv`, `__pycache__`). Works with GitHub/GitLab or 100% offline over LAN.
- **Control Plane (`agsync`)**: A hardened Python 3.12 stdlib coordination hub (`SQLite WAL` + `PRAGMA user_version` migrations) hosted on one machine and exposed to agents via **Streamable HTTP (`/mcp`)** or **Stdio MCP bridge (`agsync.mcp_bridge`)**, enforced by **Antigravity `PreToolUse` hooks** and **Git `pre-commit` guards**.

---

## Key Capabilities (Phase 1 / `v0.1.0`)

- **Exclusive TTL Leases & Fencing Tokens**: Hierarchical file, directory, and symbol locks (`scope: file | dir | symbol`) with Windows case-insensitive normalization and Junction-safe logical path checks.
- **Real-Time Messaging & Atomic Tasks**: Direct and broadcast messaging with explicit read cursors (`POST /messages/ack`), atomic task claiming (`409 Conflict` on duplicate claim), and CAS-versioned shared context KV (`expected_version`).
- **Security & Concurrency Hardening**:
  - `Content-Type: application/json` enforcement on mutating endpoints and `Host`/`Origin` validation against CSRF and DNS-rebinding.
  - Strict read-only `GET /messages` (no state mutation on `GET`).
  - Required Bearer token (`--token`, `--auto-token`, or explicit `--allow-insecure-lan`) when binding non-loopback addresses (`0.0.0.0`).
  - `SO_EXCLUSIVEADDRUSE` on Windows + OS-locked `<db>.pid` lockfile to prevent dual zombie hubs.
  - Automatic `ProxyHandler({})` bypass for loopback and private LAN addresses (preventing v2rayN/Clash/Happ proxy hijacking).
- **Worktree & Windows BOM/UTF-16 Resilience**:
  - `install-hooks` resolves `--git-common-dir` and `--git-path hooks` (supporting `git worktree` and `core.hooksPath`), avoids hardcoding transient venv Python paths, and performs atomic non-destructive JSON merges for `.agents/hooks.json` and `mcp_config.json`.
  - BOM / PowerShell UTF-16LE safe config reading and protection of `.agsync/**`, `.agsync-agent`, and `.agents/hooks.json` from agent edits.
- **Diagnostics (`agsync doctor`) & Live Dashboard**: Built-in environment/network/hook diagnostic command + self-contained web dashboard at `GET /dashboard`.

---

## Repository Layout

```text
antigravity_multi_agent_sync/
├── README.md                       # English documentation
├── README.fa.md                    # Persian documentation (مستندات فارسی)
├── pyproject.toml
├── Start_Sync_Panel.bat            # Unified Host/Peer GUI launcher (Windows)
├── Start_Host_Panel.bat
├── Start_Peer_Panel.bat
├── .github/workflows/ci.yml        # GitHub Actions CI (Windows, Ubuntu, macOS)
├── agsync/
│   ├── __init__.py
│   ├── db.py                       # SQLite WAL connection, user_version migrations, read_tx/write_tx
│   ├── protocol.py                 # Validation, path normalization, overlap matrix, compact MCP schemas
│   ├── store.py                    # Backward-compatible SyncStore facade
│   ├── server.py                   # Hardened ThreadingHTTPServer (REST API + Streamable HTTP /mcp)
│   ├── client.py                   # Stdlib urllib client with LAN proxy bypass
│   ├── mcp_bridge.py               # JSON-RPC 2.0 Stdio MCP bridge
│   ├── hook_guard.py               # Antigravity PreToolUse lease & protected-path guard
│   ├── config.py                   # User config (~/.agsync/) & Antigravity mcp_config.json registration
│   ├── discovery.py                # Zero-dependency UDP LAN beacon & discovery
│   ├── dashboard.py                # Built-in web dashboard (/dashboard)
│   ├── cli.py                      # CLI entrypoint
│   ├── core/
│   │   ├── errors.py               # SyncError, ConflictError, NotFoundError, ValidationError
│   │   ├── locks.py                # Lease acquisition, renewal, release, path checking
│   │   ├── events.py               # Agents, messages, tasks, context KV, snapshot, export/import
│   │   └── git_ops.py              # Git common-dir, hooks path, staged files, version checks
│   ├── adapters/
│   │   ├── network.py              # Proxy bypass, PID lockfile, TCP probe, Windows Job Object
│   │   └── ide.py                  # BOM/UTF-16 decoding, atomic writes, non-destructive JSON merge
│   └── commands/
│       ├── doctor.py               # `agsync doctor` diagnostic suite
│       ├── hooks.py                # `guard` and `install-hooks` handlers
│       └── sync_cmds.py            # Standard coordination CLI subcommands
├── templates/
│   ├── AGENTS.md                   # Drop-in workspace rules enforcing lease-before-edit
│   ├── mcp_config.http.json
│   ├── mcp_config.stdio.json
│   └── .agents/
│       ├── hooks.json
│       └── skills/                 # sync-start, sync-check, sync-handoff
├── docs/
│   ├── ARCHITECTURE.md
│   ├── OVERVIEW.fa.md
│   ├── ROADMAP.fa.md
│   ├── RUBRIC.md
│   ├── SETUP_GUIDE.md
│   └── SETUP_GUIDE.fa.md
├── scripts/
│   ├── control_panel.py            # Tkinter Control Panel (Host/Peer, Doctor, Firewall, MCP & Hooks)
│   └── smoke_check.py              # End-to-end smoke check (--spawn-local or --server URL)
└── tests/                          # Unit & integration tests (38 tests)
```

---

## Quickstart

### Option A: GUI Control Panel (Recommended on Windows)
1. Double-click `Start_Sync_Panel.bat` (or run `python scripts/control_panel.py`).
2. **Host (Machine A)**: Select `HOST (Hub)`, set an `Agent ID`, click **Generate Token** (or let **Start Hub Server** auto-generate one), open Windows Firewall if on LAN (**Open Firewall (Admin)**), and click **Start Hub Server**.
3. **Peer (Machine B)**: Select `PEER (Client)`, enter the Host LAN IP (or click **Discover LAN Hub**), paste the shared Bearer token, and click **Test Connection (TCP + /health)**.
4. On both machines, select your shared project directory and click **Connect Antigravity MCP** and **Install Repo Hooks & Templates**.

### Option B: CLI
1. **Start the Hub on Machine A** (state DB defaults to `~/.agsync/state.db`):
   ```powershell
   $env:AGSYNC_TOKEN = "your-shared-secret"
   python -m agsync.cli serve --host 0.0.0.0 --port 8765
   ```
   *(For local-only testing on loopback, `--host 127.0.0.1` works without a token.)*
2. **Verify health & diagnostics**:
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8765/health
   python -m agsync.cli doctor --repo C:\path\to\your\project
   ```
3. **Register Antigravity MCP & install repo hooks**:
   ```powershell
   python -m agsync.cli setup-mcp --server http://127.0.0.1:8765 --agent machine-a --token your-shared-secret
   python -m agsync.cli install-hooks --repo C:\path\to\your\project --server http://127.0.0.1:8765 --agent machine-a --with-antigravity-hook --with-templates
   ```

> **PowerShell 5.1 tip**: When passing inline JSON to `python -m agsync.cli context set`, escape inner double quotes (`'{\"port\":8765}'`) so PowerShell preserves them.

---

## Verification & Diagnostics

```powershell
# Run full test suite (38 tests, zero third-party dependencies)
python -m unittest discover -s tests -v

# Run end-to-end smoke check with temporary local server
python scripts/smoke_check.py --spawn-local

# Run environment, Git worktree, proxy, BOM/UTF-16, and hook diagnostics
python -m agsync.cli doctor --repo .
```

---

## Documentation
- **Persian README**: [README.fa.md](README.fa.md)
- **Full Persian Overview**: [docs/OVERVIEW.fa.md](docs/OVERVIEW.fa.md)
- **Step-by-Step Setup Guide**: [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) | [راهنمای فارسی](docs/SETUP_GUIDE.fa.md)
- **Architecture & Trade-offs**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Roadmap**: [docs/ROADMAP.fa.md](docs/ROADMAP.fa.md)
