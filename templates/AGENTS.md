# Multi-Agent & Multi-IDE Coordination Rules (`agsync`)

> [!CRITICAL]
> **IRONCLAD ENFORCEMENT PROTOCOL — ZERO TOLERANCE:**
> 1. **STRICT PRE-EDIT LOCK:** You CANNOT call `edit_file`, `write_file`, or modify any repository file without FIRST holding an active lease. The IDE hook (`hook_guard`) hard-blocks unauthorized edits at the engine level.
> 2. **NO SHELL BYPASSING:** Do NOT attempt to circumvent IDE guards using shell redirections (`>`, `>>`, `Set-Content`, `Out-File`, `sed`, `awk`, `file_put_contents`, or scripts). Any file modification outside of `sync_acquire_lease` is considered rogue behavior.
> 3. **MANDATORY INSTANT UNLOCK:** Leases are for atomic edits only. The moment you finish modifying a file, or before sending your final answer to the user, you MUST call `sync_release_lease(resource="<path>")` or `sync_release_lease(owner_id="<agent>", release_all=true)`. NEVER leave files locked across turns.
> 4. **STRICT REPO BOUNDARIES:** Never touch protected coordination files (`.agsync/**`, `.agsync-agent`, `.agents/**`).
> 5. **SHARED BOARD SUPREMACY:** You are strictly forbidden from working on tasks claimed by peer agents. Always check `sync_status` and claim your own discrete task via `sync_claim_task` before writing code.

You are collaborating on a shared repository across different machines and IDEs (Antigravity, Claude Code, Cursor, Windsurf, Codex, Gemini CLI). Follow this 5-stage protocol on EVERY request.

---

## 1. Discovery & Synchronization (Every Session Start)
1. Register your active presence via `sync_register(agent_id="<id>", role="<role>", branch="<branch>")`.
2. Inspect live peer activities and active locks using `sync_status()`.
3. Check unread incoming messages using `sync_poll_messages(unread_only=true, auto_ack=true)`.
4. Read shared architecture/API decisions using `sync_get_context()`.

## 2. Task Allocation & Claiming
1. Check existing tasks on the board.
2. If working on a new feature or fix, create a task with explicit file boundaries using `sync_create_task(title="...", scope_paths=["dir/file.php"])`.
3. Atomically claim it using `sync_claim_task(task_id="...")`.
4. If `sync_claim_task` returns `task_already_claimed`, **STOP IMMEDIATELY**. Do not touch those files. Coordinate with the assignee via `sync_send_message` or select another task.
5. Update status to `in_progress` via `sync_update_task(task_id="...", status="in_progress")`.

## 3. Atomic File Locking & Editing
1. **Acquire Lease:** Call `sync_acquire_lease(resource="path/to/file.php", scope="file")`.
2. If conflict returned (`lease_conflict`), do NOT edit. Notify the owner via `sync_send_message(recipient="<owner>", msg_type="wait", body="Waiting for file release...")` and work on another file.
3. Perform your edits using `edit_file` or `write_file`.
4. **Release Lease Immediately:** Call `sync_release_lease(resource="path/to/file.php")` as soon as the file is saved and valid.

## 4. Contract Publication & Decision Broadcasting
* If you change or define an API endpoint, database schema, port, or interface contract:
  - Immediately publish it: `sync_publish_context(key="contract:<name>", value={...})`.
  - Alert all peers: `sync_send_message(recipient="all", msg_type="contract", body="Updated schema in contract:<name>")`.

## 5. Verification, Task Completion & Clean Handoff
1. Verify syntax and tests.
2. Commit your work on your local feature branch.
3. Update task status: `sync_update_task(task_id="...", status="done", result_summary="...")`.
4. Ensure all leases are released: `sync_release_lease(release_all=true)`.
5. Send handoff message: `sync_send_message(recipient="all", msg_type="handoff", body="Branch ready for sync/merge.")`.

## Fallback CLI Commands
If MCP tools are ever disabled, execute the exact equivalent CLI commands in terminal:
`python -m agsync.cli [status | register | lock | unlock | task | send | context | doctor]`

## CLI Fallback (If MCP Tools Are Unavailable)
If `sync_*` MCP tools are not loaded in the current agent/runtime, run the equivalent `python -m agsync.cli` commands in the terminal (`status`, `register`, `inbox --unread --ack`, `task claim`, `lock`, `unlock`, `send`, `context set`, `doctor`).
