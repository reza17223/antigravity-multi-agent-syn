# Multi-Agent & Multi-IDE Coordination Rules (`agsync`)

> [!CRITICAL]
> **STRICT HARD-ENFORCEMENT ACTIVE (`--require-lease`):**
> 1. Any attempt to edit or modify files without first holding an active lease is BLOCKED at the IDE hook level and will immediately fail.
> 2. You MUST call `sync_acquire_lease` BEFORE calling `edit_file`, `write_file`, or saving code.
> 3. **MANDATORY LEASE RELEASE:** Immediately after finishing your file edits or before sending your response to the user, you MUST call `sync_release_lease(owner_id=..., release_all=true)`! NEVER leave files locked when finishing your turn.

You are working on a shared repository alongside one or more peer AI agents (Antigravity, Claude Code, Cursor, Windsurf, Codex, Gemini CLI, or custom agents) on separate checkouts or machines.
Each agent works in its own local Git checkout and branch — there is no built-in cross-machine live edit or lock sync. Follow this protocol on every task.

## Mandatory Coordination Loop

1. **Session Start (Register & Check Live State)**
   - Call `sync_register` with your `agent_id` (e.g., `machine-a`, `claude-b`, `cursor-1`), `role`, and current Git `branch`.
   - Call `sync_status` and `sync_poll_messages` (`unread_only: true`, `auto_ack: true`) to see what peers are doing and what files are currently locked.
   - Read relevant shared contracts with `sync_get_context`.

2. **Task Splitting (Before Starting Work)**
   - Check existing tasks in `sync_status`, or create discrete tasks with `sync_create_task` specifying `scope_paths`.
   - Atomically claim your task via `sync_claim_task` and move it to `in_progress` via `sync_update_task`.
   - If `sync_claim_task` returns a conflict error, **STOP** — do not duplicate the peer's task. Pick another open task or message your peer via `sync_send_message`.

3. **Acquire Lease BEFORE Edit & Release IMMEDIATELY After**
   - Before modifying any file, call `sync_acquire_lease` on the repository-relative path (or directory with `scope: "dir"`).
   - If `sync_acquire_lease` returns `lease_conflict`:
     - **DO NOT edit or overwrite the file.**
     - Work on a non-overlapping file, or notify your peer via `sync_send_message` and wait for release.
   - **Do NOT bypass file-edit guards via shell redirections or inline scripts** (`Set-Content`, `>`, `sed -i`, `python -c`) to edit repository files without holding a lease.
   - **Do NOT modify protected agsync files** (`.agsync/**`, `.agsync-agent`, `.agents/hooks.json`).
   - **RELEASE ON FINISH:** As soon as you finish editing the file(s), call `sync_release_lease(release_all=true)` so your peer agent is not blocked.

4. **Publish Shared Contracts & Decisions**
   - Whenever you define or change an API route, payload schema, port, or shared interface, publish it immediately via `sync_publish_context` (e.g. key `contract:api_v1`) and notify peers with `sync_send_message(msg_type="contract")`.

5. **Branch Isolation, Commit & Clean Handoff**
   - Commit only on your agent's feature branch (e.g. `agent-a/<topic>` or `agent-b/<topic>`), never directly onto a peer's active branch.
   - After committing and verifying tests, call:
     - `sync_update_task` (`status: "review"` or `"done"`, with `result_summary`)
     - `sync_release_lease` (`release_all: true` or specific resource)
     - `sync_send_message` (`msg_type: "handoff"`, summarizing branch, commit, and next steps)

## CLI Fallback (If MCP Tools Are Unavailable)
If `sync_*` MCP tools are not loaded in the current agent/runtime, run the equivalent `python -m agsync.cli` commands in the terminal (`status`, `register`, `inbox --unread --ack`, `task claim`, `lock`, `unlock`, `send`, `context set`, `doctor`).
