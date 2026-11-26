# Independent Reviewer Rubric (`antigravity_multi_agent_sync`)

Use this rubric to independently evaluate whether all project requirements and acceptance criteria are satisfied.

## 1. Guide & Architecture Rubric (Requirement R1)

| # | Criterion | Pass Condition | Evidence Location |
|---|---|---|---|
| G1 | **Accurate Antigravity Multi-Machine Model** | Correctly documents per-machine local isolation (`language_server.exe` on `localhost`, `~/.gemini/antigravity/`, `~/.gemini/config/projects/<uuid>.json`), notes that Antigravity has NO built-in cross-machine mesh/live-edit/distributed locks, accurately describes `remoteControlEnabled`, Remote Agents, and single-host `native_swarm`, uses Streamable HTTP (`/mcp`) rather than unsupported `/sse`, and uses `.agents/skills/*/SKILL.md` rather than deprecated `.agents/workflows/`. | `docs/ARCHITECTURE.md` §1, `templates/` |
| G2 | **Explicit Git Answer & Trade-off Analysis** | Explicitly answers *"Is Git needed for two side-by-side machines?"* and provides a clear 4-way trade-off matrix (Git+branches only vs SMB/NFS vs Syncthing/Mutagen vs Hybrid Git+LAN `agsync`), covering silent overwrites, `.git/index.lock` races, build artifact collisions, IDE watcher thrashing, and LAN-only Git without GitHub. | `docs/ARCHITECTURE.md` §2–3 |
| G3 | **End-to-End Concrete Setup Guide** | Step-by-step instructions for both Machine A and Machine B covering Python/Git prerequisites, LAN IP discovery, Windows Firewall port 8765, Git branch/bare-repo setup, starting `agsync`, configuring `~/.gemini/config/mcp_config.json`, installing `AGENTS.md` + `.agents/skills/` + hooks, daily collaborative workflow, and troubleshooting, with zero broken commands or missing config keys. | `docs/SETUP_GUIDE.md`, `docs/SETUP_GUIDE.fa.md` |

## 2. Coordination & Conflict-Prevention Tooling Rubric (Requirement R2)

| # | Criterion | Pass Condition | Evidence Location |
|---|---|---|---|
| T1 | **Zero-Dependency Runtime** | All modules in `agsync/`, `scripts/smoke_check.py`, and `tests/` run on Python 3.12 stdlib alone with zero `pip install` requirements. | `pyproject.toml`, `agsync/*.py` |
| T2 | **Live Presence & Messaging** | Supports registering two agents, TTL online/offline status, direct and `*` broadcast messages, monotonic IDs, and unread cursor/ack polling. | `agsync/store.py`, `agsync/server.py` |
| T3 | **Atomic Task Splitting** | Concurrent `claim_task` calls (tested under `threading.Barrier`) yield exactly 1 success and 1 `409 Conflict`; supports task `scope_paths` and status lifecycle. | `agsync/store.py`, `tests/test_concurrency.py` |
| T4 | **Conflict-Free Parallel Editing & Guardrails** | Exclusive TTL leases with monotonic fencing tokens, hierarchical directory/file overlap rejection, path normalization + Windows case-insensitivity, `PreToolUse` `hook_guard.py` returning `{"decision": "deny"}` on peer-locked edits, and Git `pre-commit` guard (`cli.py guard --staged`). | `agsync/protocol.py`, `agsync/store.py`, `agsync/hook_guard.py`, `agsync/cli.py` |
| T5 | **Live Shared Context / Contracts** | Shared `context_kv` with monotonic versioning and optimistic `expected_version` CAS (`409` on stale writes). | `agsync/store.py`, `agsync/server.py` |
| T6 | **Dual Antigravity MCP Support** | Supports both native Streamable HTTP (`POST /mcp`, MCP `2025-03-26`) and newline-delimited Stdio JSON-RPC 2.0 (`agsync.mcp_bridge`) exposing all 13 `sync_*` tools. | `agsync/server.py`, `agsync/mcp_bridge.py`, `tests/test_mcp_and_hooks.py` |

## 3. Verification & Smoke Check Rubric

| # | Criterion | Pass Condition | Verification Command |
|---|---|---|---|
| V1 | **Automated Unit & Concurrency Tests** | All tests in `tests/` execute cleanly and pass with exit code `0`. | `python -m unittest discover -s tests -v` |
| V2 | **Standalone Fresh-Setup Smoke Check** | `scripts/smoke_check.py --spawn-local` runs end-to-end (presence, messaging, task split, lock conflict rejection, context sharing, release, `/health`) and exits `0`. | `python scripts/smoke_check.py --spawn-local` |
| V3 | **Fresh-Repo Hook & Template Walkthrough** | Copying templates and running `install-hooks` on a fresh temp Git repo produces working `.git/hooks/pre-commit` and `.agents/hooks.json` with zero errors. | `tests/test_cli_smoke.py` |
