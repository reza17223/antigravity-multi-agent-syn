# Architecture Recommendation: Coordinating Two Antigravity Agents Across Side-by-Side Machines

## 1. How Antigravity Actually Works on Separate Machines

Before choosing a coordination design, it is critical to ground every decision in Antigravity's verified runtime architecture (verified on Antigravity `44.9.1` / Language Server `2.34.0`):

1. **Per-Machine Isolated Runtime**:
   - Each machine runs its own Electron IDE/CLI client and local `language_server.exe` daemon bound to `localhost` (`~/.gemini/antigravity/daemon/hub_<port>.json`).
   - Conversation history, `brain/` state, trajectory summaries (`conversation_summaries.db`), and local conversation locks (`presence/*.lock`) live strictly on that machine under `~/.gemini/antigravity/`.
   - User settings, MCP server registrations (`~/.gemini/config/mcp_config.json`), and per-workspace tool permission grants (`~/.gemini/config/projects/<uuid>.json`) are also strictly machine-local.
2. **What Antigravity Does NOT Have Built-In**:
   - **No cross-machine agent-to-agent mesh**: Two Antigravity installations on adjacent PCs cannot discover each other or pass messages natively.
   - **No multi-user cloud live-editing / CRDT**: Antigravity has no shared live-buffer sync across two independent workstations.
   - **No distributed file locking**: Each agent issues local `read_file`, `edit_file`, `write_file`, and `run_command` calls against its local filesystem.
   - **`remoteControlEnabled` is NOT multi-user collaboration**: In `~/.gemini/config/config.json`, Remote Control connects a single user's browser or mobile device remotely to their *own* local desktop session.
   - **Remote Agents are NOT a shared LAN workspace**: Remote Agents run isolated single-user tasks in Google-managed cloud environments.
   - **Native `swarm` is single-host only**: Built-in `native_swarm` coordinates multiple conversations on *one* machine via local `board.jsonl` / `members.json` and local `agentapi` calls; `swarm_board`'s optional `remote_board.json` viewer hook is read-only (`"Nothing here writes a file or messages an agent"`) and is not a cross-machine backend.
3. **Antigravity's Supported Extension & Guard Surfaces**:
   - **MCP (`~/.gemini/config/mcp_config.json` or `.agents/plugins/<name>/mcp_config.json`)**:
     - Supports **Stdio** (`command`, `args`, `env`, `cwd`) and **Streamable HTTP** (MCP `2025-03-26`, `"url": "http://<host>:<port>/mcp"` with optional `"headers"`).
     - **Legacy HTTP+SSE (`/sse`) is NOT supported natively** by Antigravity.
     - Note: Bare `<repo>/.agents/mcp_config.json` is not a documented discovery location; use `~/.gemini/config/mcp_config.json` or a workspace plugin directory.
   - **Workspace Rules (`AGENTS.md`, `GEMINI.md`, `.agents/rules/*.md`)**: Discovered hierarchically up to the `.git` root (24 KB per-file cap, 20,000-token budget). `AGENTS.md` / `GEMINI.md` require no frontmatter and are always active.
   - **Workspace Skills (`.agents/skills/<name>/SKILL.md`)**: Modern runbook and `/slash` command format with YAML `name` and `description`. Note: legacy `.agents/workflows/*.md` is **deprecated**.
   - **Lifecycle Hooks (`.agents/hooks.json`)**: Synchronous `type: "command"` hooks using camelCase JSON on stdin/stdout (`cmd /c` on Windows, `sh -c` on Unix, with CWD set to the folder containing `hooks.json`). `PreToolUse` can inspect `toolCall.name`, `toolCall.args`, and `cwd`, and return `{"decision": "allow"}` or `{"decision": "deny", "reason": "..."}` before any file write executes.

---

## 2. Explicit Answer: Is Git Needed for Two Side-by-Side Machines?

**Yes — Git is strongly needed for safe parallel AI coding even on two side-by-side machines, although Git alone is not enough for real-time coordination.**

### Why Git Is Needed (Even Side-by-Side)
1. **Working Tree & Build Artifact Isolation**: Autonomous agents frequently run tests, install packages (`node_modules/`, `.venv/`), or generate build caches (`__pycache__/`, `dist/`, `target/`). Separate local Git checkouts ensure Machine B's compiler or test runner never locks or mutates files under Machine A's tests.
2. **Atomic Checkpoints & Instant Rollback**: If an agent performs a broken 6-file refactor, `git diff` and `git reset`/`git restore` recover cleanly without entangling the colleague's uncommitted edits.
3. **Deterministic 3-Way Merge**: When both agents legitimately update adjacent parts of the same codebase (e.g., adding routes to a shared router or cases to a test suite), Git merges non-overlapping hunks deterministically and flags exact line conflicts for review instead of silently losing one agent's write.

### Why Git Alone Is NOT Enough
Git only synchronizes state **after** work is committed and pushed. While Agent A and Agent B edit uncommitted files for 10–20 minutes, Git provides zero visibility into:
- Which subtask the peer has already claimed,
- Which files or directories the peer is actively modifying right now,
- What API contract, schema, or port configuration the peer just changed.

### Does Git Require GitHub / Internet?
**No.** Two side-by-side machines on the same LAN can use a **bare Git repository hosted on Machine A** (over OpenSSH, a local network share, or `git daemon`) with sub-second LAN `git push` / `git pull` and zero external cloud dependency.

---

## 3. Four-Way Architecture Trade-Off Matrix

| Dimension | (a) Git + Feature Branches Only | (b) Shared Network Folder (SMB / NFS) | (c) Real-Time File Sync (Syncthing / Mutagen / rsync) | (d) **Recommended Hybrid: Separate Git Clones + LAN `agsync` Hub** |
|---|---|---|---|---|
| **Working tree isolation** | High (local disks) | **None** (single shared dir) | Low (local disk continuously mutated by peer) | **High** (independent local clones + separate branches) |
| **Silent overwrite risk** | Low at merge, **high duplicate-work risk** | **Critical** (`read` -> `write` race drops A's write) | **High** (`.sync-conflict-*` copies break builds/imports) | **Eliminated** (pre-edit exclusive leases + branch isolation) |
| **`.git` corruption risk** | None | **High** (`.git/index.lock` & Windows SMB oplock races) | **Critical** (syncing `.git/` corrupts refs/HEAD/index) | **None** (`.git/` remains strictly local on each machine) |
| **Build artifact collisions** (`node_modules`, `.venv`, `__pycache__`) | None | **Critical** (Windows `EBUSY` locks, cross-OS binaries) | **High** unless ignore rules cover every stack | **None** (each machine builds/tests locally) |
| **IDE / Language Server thrashing** | None | **Severe** (SMB latency + continuous re-indexing) | **Moderate–High** (partial multi-file writes break LSP mid-edit) | **None** (each IDE watches its own coherent branch) |
| **Real-time agent-to-agent sync** | **None** (blind until push) | None (ad-hoc notes) | None (no messaging/task/lock protocol) | **Full** (live presence, messaging, task board, leases, shared context via MCP) |
| **Setup complexity** | Low | Low–Medium | Medium | **Low** (1 Python stdlib server + 1 MCP config entry per machine) |

> **Important `git worktree` nuance**: `git worktree` shares one `.git` repository across multiple directories on a **single** machine. Do **not** share a `git worktree` across two machines over SMB; give Machine A and Machine B separate `git clone` directories on separate feature branches (`agent-a/<topic>`, `agent-b/<topic>`).

---

## 4. `agsync` Control Plane Architecture

```text
+--------------------------------------+          LAN (HTTP :8765)          +--------------------------------------+
|              Machine A               |  <------------------------------>  |              Machine B               |
|                                      |                                    |                                      |
|  Antigravity IDE / CLI               |                                    |  Antigravity IDE / CLI               |
|   |-- AGENTS.md + .agents/skills/    |                                    |   |-- AGENTS.md + .agents/skills/    |
|   |-- PreToolUse hook_guard.py       |                                    |   |-- PreToolUse hook_guard.py       |
|   '-- MCP (/mcp or mcp_bridge.py) ---+--+                              +--+--- MCP (/mcp or mcp_bridge.py)     |
|                                      |  |                              |  |                                      |
|  agsync.server (0.0.0.0:8765) <------+--+------------------------------+  |  Local Git Clone (branch: agent-b/*) |
|   '-- SQLite WAL + BEGIN IMMEDIATE   |                                    |   '-- .git/hooks/pre-commit (guard)  |
|  Local Git Clone (branch: agent-a/*) |                                    +--------------------------------------+
|   '-- .git/hooks/pre-commit (guard)  |
+--------------------------------------+
```

### Core Guarantees
1. **Atomic Serialization (`SQLite WAL` + `BEGIN IMMEDIATE` + `threading.RLock`)**:
   - Simultaneous `POST /tasks/{id}/claim` or `POST /leases/acquire` requests from Machine A and Machine B are serialized by SQLite's write lock; exactly one succeeds (`200 OK`) and the other receives structured `409 Conflict` JSON.
2. **Hierarchical Path Normalization & Overlap Rejection**:
   - Paths normalize `\` -> `/`, strip `./` and `/`, reject `..` traversal and absolute drive letters, and default to case-insensitive matching on Windows (`--case-insensitive`).
   - Leasing directory `src/api` (`scope="dir"`) blocks a peer from leasing `src\api\auth.py` (`scope="file"`), and vice versa, while permitting disjoint sibling paths (`src/api_v2/auth.py`).
   - Symbol locks (`src/api/auth.py::login`, `scope="symbol"`) allow two agents to lock distinct symbols in the same file when needed while still conflicting with whole-file or parent-directory leases.
3. **TTL Expiry & Fencing Tokens**:
   - Every lease has a TTL (default 600s) and monotonic `fencing_token`. If a machine crashes or forgets to unlock, leases expire cleanly without manual DB cleanup. Same-owner re-acquisition is idempotent and extends the TTL.
4. **Defense-in-Depth Conflict Prevention**:
   - **Layer 1 (Behavioral)**: `AGENTS.md` + `.agents/skills/` instruct both agents to claim tasks and call `sync_acquire_lease` before editing.
   - **Layer 2 (Runtime Tool Guard)**: `.agents/hooks.json` (`PreToolUse` -> `agsync.hook_guard`) intercepts `edit_file` / `write_file` / `replace_file_content` and returns `{"decision": "deny"}` if the target file is actively leased by the peer.
   - **Layer 3 (Git Commit Guard)**: `.git/hooks/pre-commit` (`agsync.cli guard --staged`) prevents committing staged files that conflict with active peer leases.
   - **Layer 4 (Git Branch Isolation)**: Separate local clones and branches ensure even an offline or forced edit cannot corrupt the peer's working tree.
