# Architecture: Multi-Agent & Multi-Machine Coordination (`agsync`)

[English](ARCHITECTURE.md) | [فارسی](ARCHITECTURE.fa.md)

## 1. Why Local AI Coding Runtimes Need External Coordination

AI coding agents and IDEs (Antigravity, Claude Code, Cursor, Windsurf, Codex, Gemini CLI) execute locally on each workstation:
1. **Per-Machine Isolated State**: Conversation history, local caches, MCP configurations, and workspace permissions live locally on each developer's machine (for example, Antigravity stores runtime state under `~/.gemini/antigravity/` and `~/.gemini/config/mcp_config.json`, with its local `language_server` bound to `localhost`).
2. **No Cross-Machine Mesh or Distributed Locking**: Standalone agent runtimes cannot natively discover peer agents on another machine, claim shared tasks atomically, or acquire cross-machine file/directory leases before editing.
3. **Why Native Single-Host Features Don't Replace `agsync`**: Single-machine orchestration tools (such as local subagent boards) coordinate conversations on **one** host only and do not provide distributed locking across separate workstations or heterogeneous IDEs/agents.

---

## 2. Explicit Answer: Is Git Needed for Side-by-Side Collaboration?

**Yes — Git is essential for safe parallel AI coding even on side-by-side machines, though Git alone is not enough for real-time coordination.**

- **Why Git Is Essential**:
  1. **Working Tree & Build Artifact Isolation**: Agents install dependencies (`node_modules/`, `.venv/`), generate caches (`__pycache__/`, `dist/`, `target/`), and run test suites. Independent local checkouts (or local `git worktree`s) prevent compiler/test-runner lock collisions.
  2. **Atomic Rollback**: `git diff` / `git restore` recover cleanly from broken multi-file edits without entangling a peer's uncommitted work.
  3. **Deterministic 3-Way Merge**: Non-overlapping edits merge cleanly, and true line conflicts are flagged explicitly.
- **Why Git Alone Is Not Enough**: Git only syncs state **after** a commit and push. While two or more agents edit uncommitted files, Git gives zero visibility into active task ownership, current file leases, or in-flight API contracts.
- **No Internet Required**: Side-by-side machines can use a local bare Git repository over LAN SSH/file share with zero cloud dependency.

---

## 3. Four-Way Trade-Off Matrix

| Dimension | (a) Git + Feature Branches Only | (b) Shared Folder (SMB / NFS) | (c) Real-Time File Sync (Syncthing / Mutagen) | (d) **Hybrid: Isolated Git + `agsync` Hub** |
|---|---|---|---|---|
| **Working tree isolation** | High (local disks) | **None** (shared folder) | Low (continuous remote mutation) | **High** (separate clones/worktrees & branches) |
| **Silent overwrite risk** | High duplicate-work risk | **Critical** (`read->write` races) | **High** (`.sync-conflict-*` copies) | **Eliminated** (pre-edit exclusive leases) |
| **`.git` corruption risk** | None | **High** (`.git/index.lock` races) | **Critical** (corrupts refs/HEAD) | **None** (`.git/` stays strictly local) |
| **Build artifact collisions** | None | **Critical** (`EBUSY` / OS mismatches) | **High** without exhaustive ignores | **None** |
| **Real-time agent sync** | **None** (blind until push) | None | None | **Full** (MCP + HTTP + CLI + Hooks) |
| **Multi-IDE / Heterogeneous Agents** | Manual | Broken | Broken | **Full** (any MCP/HTTP/CLI agent) |

---

## 4. `agsync` Control Plane & Modular Design (Phase 1)

```text
+----------------------------------------+        LAN / Local HTTP        +----------------------------------------+
|          Machine A / Agent A           |  <-------------------------->  |          Machine B / Agent B           |
|  (Antigravity / Claude / Cursor / CLI) |                                |  (Antigravity / Claude / Cursor / CLI) |
|   |-- AGENTS.md + .agents/skills/      |                                |   |-- AGENTS.md + .agents/skills/      |
|   |-- PreToolUse hook_guard.py         |                                |   |-- PreToolUse hook_guard.py         |
|   '-- MCP (/mcp or mcp_bridge.py) -----+--+                          +--+--- MCP (/mcp or mcp_bridge.py)       |
|                                        |  |                          |  |                                        |
|  agsync.server (0.0.0.0:8765 + Token)<-+--+--------------------------+  |  Local Git Clone (branch: agent-b/*)   |
|   |-- db.py (SQLite WAL + user_version)|                                |   '-- git pre-commit guard (--staged)  |
|   |-- core/{locks,events,git_ops}.py   |                                +----------------------------------------+
|   |-- adapters/{network,ide}.py        |
|   '-- ~/.agsync/state.db (+ .pid lock) |
+----------------------------------------+
```

### Core Guarantees & Hardening
1. **Atomic Serialization & Read/Write Separation (`db.py`, `core/locks.py`, `core/events.py`)**:
   - Write operations serialize via `BEGIN IMMEDIATE` + `threading.RLock()` with safe `COMMIT` rollback handling and `PRAGMA user_version` schema migrations.
   - Read routes (`/health`, `/leases`, `/leases/check`, `/snapshot`, `/state/export`) run inside deferred read transactions (`read_tx()`) filtering `expires_at > now` in SQL without blocking on write locks.
   - `import_state` takes a consistent SQLite `.bak` before `replace=True` and preserves `MAX(existing, imported)` on `fencing_seq` and `task_seq`.
2. **Hierarchical Normalization & Junction Safety (`protocol.py`, `hook_guard.py`)**:
   - Paths normalize `\` -> `/`, reject `..` traversal and drive/UNC prefixes, and use logical `abspath`/`normpath` checks in `hook_guard.py` so Windows Directory Junctions cannot escape repo root.
   - Protected paths (`.agsync/**`, `.agsync-agent`, `.agents/hooks.json`) are denied before server calls.
3. **HTTP, CSRF, Proxy & Windows Hardening (`server.py`, `adapters/network.py`, `adapters/ide.py`)**:
   - `Content-Type: application/json` (`415`), `Host`/`Origin` CSRF/DNS-rebinding guards (`403`), strictly read-only `GET /messages`, UTF-8 `hmac.compare_digest`, 30s handler timeout, and catch-all `500` JSON responses.
   - `SO_EXCLUSIVEADDRUSE` + OS-locked `<db>.pid`, `ProxyHandler({})` LAN bypass, BOM/UTF-16 config decoding, `git rev-parse --git-common-dir` / `--git-path hooks` support for worktrees, and atomic `.bak` JSON merging.
4. **Defense-in-Depth**:
   - **Layer 1 (Behavioral)**: `AGENTS.md` + `skills/`
   - **Layer 2 (Runtime Tool Guard)**: `PreToolUse` (`hook_guard.py`)
   - **Layer 3 (Git Commit Guard)**: `pre-commit` (`agsync guard --staged`)
   - **Layer 4 (Git Branch Isolation)**: Independent checkouts and feature branches.
