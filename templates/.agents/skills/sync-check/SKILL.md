---
name: sync-check
description: Poll peer messages, verify path leases before editing, and read updated shared contracts mid-task.
---

# Sync Check Runbook

Run this check before starting a new subtask or editing shared/adjacent files:

1. Poll unread peer messages:
   - Call `sync_poll_messages` (`unread_only: true`, `auto_ack: true`).
2. Pre-flight check target files:
   - Call `sync_check_paths` with the list of repository-relative paths you plan to edit next.
   - If any path is in `blocked`, message the holding peer via `sync_send_message` instead of editing.
3. Acquire leases for clear paths:
   - Call `sync_acquire_lease` on each target file or directory (`scope: "dir"` for new subdirectories).
4. Refresh shared context:
   - Call `sync_get_context` if the peer announced a schema, port, or interface update.
