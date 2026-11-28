# agsync — Let Multiple AI Coding Agents Work Together Without Stepping on Each Other

[English](README.md) | [فارسی](README.fa.md)

[![CI](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml/badge.svg)](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml)

Working on a project with a teammate where both of you use AI coding agents (like **Antigravity**, **Claude Code**, **Cursor**, **Windsurf**, **Codex**, or **Gemini CLI**)?

Without coordination, agents on different machines don't know what the other is doing — they edit the same files at the same time, overwrite each other's changes, and break builds.

**`agsync` fixes this.** Think of it as a lightweight **walkie-talkie + task board + smart file lock** for AI agents. No external Python packages needed — just standard Python 3.12+.

---

## What It Does (In Plain English)

- **Locks files before editing**: When Agent A starts editing `auth.py`, `agsync` locks it. If Agent B tries to touch `auth.py` (or its folder), `agsync` stops it automatically.
- **Keeps each developer on their own Git branch**: Everyone works in their own folder/branch so `node_modules`, `.venv`, and `.git` never collide.
- **Shares tasks, messages & API contracts live**: Agents can claim tasks (no two agents accidentally doing the same job), send messages, and share ports or API schemas in real time.
- **Works locally OR remotely**: Works side-by-side over Wi-Fi/LAN, or across the internet using a single-use **Join Link** and **Cloudflare Tunnel**.
- **Works with any agent**: Native MCP + hook support for Antigravity, Claude Code, and Cursor, plus ready-to-use configs for Windsurf, Codex, Gemini CLI, and custom REST/CLI scripts.

---

## Quick Start (2 Minutes)

### Option 1: Using the Windows Control Panel (Easiest)

1. Double-click **`Start_Sync_Panel.bat`**.
2. **On the Host machine (Person A)**:
   - Choose **`HOST (Hub)`**, enter a name (like `alice`), and click **`Generate Token`**.
   - If you are on a local network, click **`Open Firewall (Admin)`**, then click **`Start Hub Server`**.
   - Select your project folder with **`Browse...`**, then click **`Connect Antigravity MCP`** and **`Install Repo Hooks & Templates`**. *(If your folder isn't a Git repo yet, the panel will offer to initialize Git for you automatically.)*
3. **On the Peer machine (Person B)**:
   - Open **`Start_Sync_Panel.bat`** and choose **`PEER (Client)`**.
   - **Same Wi-Fi/LAN**: Enter the Host's IP (or click **`Discover LAN Hub`**), paste the token, and click **`Test Connection (TCP + /health)`**.
   - **Remote (different location)**: On the Host click **`Cloudflare Tunnel`** + **`Create Join Link...`**, send the link to Person B, and on Person B click **`Join Remote Hub...`**.
   - Select your local project folder and click **`Connect Antigravity MCP`** and **`Install Repo Hooks & Templates`**.

### Option 2: Using the Terminal (macOS / Linux / Windows)

1. **Start the Hub** on Machine A:
   ```bash
   export AGSYNC_TOKEN="my-secret-token"
   python -m agsync.cli serve --host 0.0.0.0 --port 8765
   ```
2. **Connect your IDE / Agent**:
   ```bash
   # Choose your IDE: antigravity, claude, or cursor
   python -m agsync.cli setup-mcp --ide claude --server http://127.0.0.1:8765 --agent claude-a --token my-secret-token
   python -m agsync.cli install-hooks --repo /path/to/your/project --ide claude --with-templates
   ```
3. **Check that everything is healthy**:
   ```bash
   python -m agsync.cli doctor --repo /path/to/your/project
   ```

---

## Useful Commands You Should Know

- **See what everyone is doing**: Open `http://<HOST_IP>:8765/dashboard` in your browser (or click **Open Web Dashboard** in the panel).
- **Run multiple agents on one PC**: `agsync worktree create --agent agent-2` creates a clean sibling folder with its own identity.
- **Check for merge conflicts before merging**: `agsync merge-check --base main --target HEAD`.
- **Run the test suite**: `python -m unittest discover -s tests -v`.

---

## Learn More

- **Persian README**: [README.fa.md](README.fa.md)
- **Step-by-Step Setup Guide**: [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) | [راهنمای فارسی](docs/SETUP_GUIDE.fa.md)
- **How It Works Under the Hood**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | [معماری فارسی](docs/ARCHITECTURE.fa.md)
