---
name: sync-start
description: Register this Antigravity agent with the agsync LAN hub, inspect unread peer messages, read shared contracts, and claim an available task.
---

# Sync Start Runbook

Run this sequence at the start of a collaborative session across Machine A and Machine B:

1. Determine your current Git branch (`git branch --show-current`) and register presence:
   - Call `sync_register` with `branch` set to your current branch and `status: "online"`.
2. Check live team state:
   - Call `sync_status` to inspect online peers, active file/directory leases, and open/claimed tasks.
   - Call `sync_poll_messages` with `unread_only: true` and `auto_ack: true` to read any peer messages or handoffs.
   - Call `sync_get_context` (with no `key`) to review all published API contracts and shared decisions.
3. Claim or propose work:
   - If an open task matches your assignment, call `sync_claim_task` and then `sync_update_task` with `status: "in_progress"`.
   - Otherwise create a scoped task with `sync_create_task` (listing `scope_paths`) and claim it.
4. Before touching any file, acquire a lease via `sync_acquire_lease`.
