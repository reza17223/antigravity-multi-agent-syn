# Antigravity Multi-Agent Sync (`agsync`)

Practical architecture, step-by-step setup guide, and **zero-dependency Python 3.12 stdlib** coordination tooling enabling two colleagues working on adjacent machines running **Antigravity** to collaborate simultaneously on a single project with real-time agent-to-agent messaging, task splitting, shared live contracts, and conflict-free parallel file editing.

## Why This Exists

Each Antigravity installation runs locally (`language_server.exe` bound to `localhost`, machine-local `~/.gemini/antigravity/` and `~/.gemini/config/`). Antigravity has **no built-in cross-machine agent mesh, cloud multi-user live editing, or distributed file locking**.

**Recommended Hybrid Architecture**:
- **Code Plane (Git)**: Each machine keeps its own local Git clone on its own feature branch (`agent-a/<task>`, `agent-b/<task>`), eliminating `.git/index.lock` races, build artifact collisions (`node_modules`, `.venv`, `__pycache__`), and silent file overwrites. Works with GitHub/GitLab OR 100% offline over LAN.
- **Control Plane (`agsync`)**: A lightweight Python 3.12 stdlib LAN server (`SQLite WAL` + `BEGIN IMMEDIATE`) hosted on Machine A, exposed to both Antigravity agents via **Streamable HTTP (`/mcp`)** or **Stdio MCP bridge (`agsync.mcp_bridge`)**, plus **CLI**, **Antigravity `PreToolUse` hook**, and **Git `pre-commit` guard**.

## Repository Layout

```text
antigravity_multi_agent_sync/
├── README.md
├── pyproject.toml
├── agsync/
│   ├── __init__.py
│   ├── protocol.py         # Path normalization, hierarchical overlap detection, MCP schemas
│   ├── store.py            # SQLite WAL store (agents, messages/cursors, tasks, leases, context_kv)
│   ├── server.py           # ThreadingHTTPServer LAN hub (REST JSON API + Streamable HTTP /mcp)
│   ├── client.py           # Stdlib urllib HTTP client
│   ├── mcp_bridge.py       # Newline-delimited JSON-RPC 2.0 Stdio MCP bridge
│   ├── hook_guard.py       # Antigravity PreToolUse stdin/stdout JSON lease guard
│   └── cli.py              # Full CLI + Git pre-commit guard + hook installer
├── templates/
│   ├── AGENTS.md           # Drop-in Antigravity workspace rules enforcing lease-before-edit
│   ├── mcp_config.stdio.json
│   ├── mcp_config.http.json
│   └── .agents/
│       ├── hooks.json
│       └── skills/
│           ├── sync-start/SKILL.md
│           ├── sync-check/SKILL.md
│           └── sync-handoff/SKILL.md
├── docs/
│   ├── ARCHITECTURE.md     # Antigravity multi-machine model & Git vs SMB/Syncthing/Hybrid matrix
│   ├── SETUP_GUIDE.md      # Complete Machine A & Machine B step-by-step setup guide
│   ├── SETUP_GUIDE.fa.md   # Persian (فارسی) quick-start & summary guide
│   └── RUBRIC.md           # Independent reviewer rubric & acceptance checklist
├── scripts/
│   └── smoke_check.py      # Fresh-setup verification script (--spawn-local or --server URL)
└── tests/                  # Python unittest suite (store, concurrency, MCP/hooks, CLI/smoke)
```

## Quickstart (3 Minutes)

1. **Start the LAN Coordination Hub on Machine A**:
   ```powershell
   python -m agsync.cli serve --host 0.0.0.0 --port 8765 --db .agsync/state.db
   ```
2. **Verify health**:
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8765/health
   ```
3. **Connect Antigravity on Machine A & Machine B** using `templates/mcp_config.http.json` (Streamable HTTP `/mcp`) or `templates/mcp_config.stdio.json` (Stdio bridge) in `~/.gemini/config/mcp_config.json`.
4. **Copy workspace rules & skills** from `templates/AGENTS.md` and `templates/.agents/skills/` into your shared project repository, and optionally run:
   ```powershell
   python -m agsync.cli install-hooks --repo C:\path\to\your\project --server http://192.168.1.10:8765 --agent machine-a --with-antigravity-hook
   ```

> **Windows PowerShell 5.1 tip**: When passing JSON to `python -m agsync.cli context set` in PowerShell 5.1, escape inner double quotes (`'{\"port\":8765}'`) so PowerShell does not strip them before Python receives `sys.argv`.

See [docs/OVERVIEW.fa.md](docs/OVERVIEW.fa.md) (معرفی کامل فارسی) and [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) ([فارسی](docs/SETUP_GUIDE.fa.md)) for full Machine A & B instructions, Windows Firewall rules, LAN-only Git setup, and troubleshooting, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the Git vs LAN/direct-sync trade-off analysis.

## Verification & Tests

Run the full automated test suite (zero third-party packages required):
```powershell
python -m unittest discover -s tests -v
```

Run the standalone fresh-setup smoke check (spawns a temporary local server and simulates Machine A & Machine B):
```powershell
python scripts/smoke_check.py --spawn-local
```
