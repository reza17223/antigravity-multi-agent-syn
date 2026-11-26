---
name: sync-handoff
description: Commit local branch changes, publish updated contracts, release agsync leases, mark tasks complete, and notify the peer agent.
---

# Sync Handoff Runbook

Run this sequence when finishing a task or handing work off to your colleague's Antigravity agent:

1. Verify tests pass on your local branch and commit your changes locally (`git status`, `git add`, `git commit`).
2. If you added or modified any API, schema, or configuration contract, publish it with `sync_publish_context`.
3. Update your task status via `sync_update_task` (`status: "review"` or `"done"`, with a concise `result_summary`).
4. Release all file/directory leases held by this agent via `sync_release_lease` (`release_all: true`).
5. Send a handoff message to the peer via `sync_send_message` (`msg_type: "handoff"`, `recipient_id: "*"` or peer ID) including your branch name, commit hash, changed files, and any next action required.
