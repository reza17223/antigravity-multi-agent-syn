"""Self-contained live web dashboard HTML served at GET /dashboard."""

from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>agsync Live Coordination Dashboard</title>
  <style>
    :root {
      --bg: #f6f8fa;
      --surface: #ffffff;
      --surface-alt: #f0f3f6;
      --border: #d0d7de;
      --text: #1f2328;
      --muted: #59636e;
      --accent: #0969da;
      --accent-fg: #ffffff;
      --ok: #1a7f37;
      --ok-bg: #dafbe1;
      --warn: #9a6700;
      --warn-bg: #fff8c5;
      --danger: #cf222e;
      --danger-bg: #ffebe9;
      --focus: #0969da;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0d1117;
        --surface: #161b22;
        --surface-alt: #21262d;
        --border: #30363d;
        --text: #e6edf3;
        --muted: #8b949e;
        --accent: #2f81f7;
        --accent-fg: #ffffff;
        --ok: #3fb950;
        --ok-bg: rgba(46, 160, 67, 0.15);
        --warn: #d29922;
        --warn-bg: rgba(187, 128, 9, 0.15);
        --danger: #f85149;
        --danger-bg: rgba(248, 81, 73, 0.15);
        --focus: #58a6ff;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 16px;
      line-height: 1.5;
    }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 24px 16px; }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand h1 { font-size: 20px; margin: 0; line-height: 1.2; }
    .subtitle { color: var(--muted); font-size: 14px; margin: 0; }
    .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    button, input {
      font: inherit;
      font-size: 14px;
      border-radius: 6px;
      border: 1px solid var(--border);
      padding: 8px 12px;
      background: var(--surface);
      color: var(--text);
      min-height: 36px;
    }
    button { cursor: pointer; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }
    button.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
    button:focus-visible, input:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
      margin-bottom: 16px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }
    .card h2 {
      font-size: 16px;
      margin: 0 0 12px 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
    }
    .badge.ok { background: var(--ok-bg); color: var(--ok); }
    .badge.warn { background: var(--warn-bg); color: var(--warn); }
    .badge.danger { background: var(--danger-bg); color: var(--danger); }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td {
      text-align: left;
      padding: 8px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; }
    code {
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 13px;
      background: var(--surface-alt);
      padding: 2px 4px;
      border-radius: 4px;
    }
    .empty { color: var(--muted); font-size: 14px; margin: 8px 0; }
    .banner {
      padding: 12px 16px;
      border-radius: 8px;
      margin-bottom: 16px;
      border: 1px solid var(--border);
      background: var(--warn-bg);
      color: var(--warn);
      display: none;
    }
    .sr-only {
      position: absolute; width: 1px; height: 1px; overflow: hidden;
      clip: rect(0, 0, 0, 0); white-space: nowrap;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <circle cx="6" cy="6" r="3"></circle>
          <circle cx="18" cy="6" r="3"></circle>
          <circle cx="12" cy="18" r="3"></circle>
          <path d="M8.5 7.5 11 15M15.5 7.5 13 15M9 6h6"></path>
        </svg>
        <div>
          <h1>agsync Coordination Hub</h1>
          <p class="subtitle" id="hub-meta">Connecting...</p>
        </div>
      </div>
      <div class="controls">
        <label for="token-input" class="sr-only">Bearer token (optional)</label>
        <input id="token-input" type="password" placeholder="Bearer token (if enabled)" aria-label="Bearer token">
        <button id="refresh-btn" class="primary" type="button">Refresh Now</button>
        <button id="auto-btn" type="button" aria-pressed="true">Auto: ON (3s)</button>
      </div>
    </header>

    <div id="error-banner" class="banner" role="alert"></div>

    <main>
      <div class="grid">
        <section class="card" aria-labelledby="agents-heading">
          <h2 id="agents-heading">Connected Agents <span class="badge ok" id="agents-count">0</span></h2>
          <div id="agents-body"></div>
        </section>

        <section class="card" aria-labelledby="leases-heading">
          <h2 id="leases-heading">Active File &amp; Scope Leases <span class="badge warn" id="leases-count">0</span></h2>
          <div id="leases-body"></div>
        </section>
      </div>

      <div class="grid">
        <section class="card" aria-labelledby="tasks-heading">
          <h2 id="tasks-heading">Shared Task Board <span class="badge ok" id="tasks-count">0</span></h2>
          <div id="tasks-body"></div>
        </section>

        <section class="card" aria-labelledby="context-heading">
          <h2 id="context-heading">Shared Context Keys <span class="badge ok" id="context-count">0</span></h2>
          <div id="context-body"></div>
        </section>
      </div>
    </main>
  </div>

  <script>
    const tokenInput = document.getElementById("token-input");
    const refreshBtn = document.getElementById("refresh-btn");
    const autoBtn = document.getElementById("auto-btn");
    const errBanner = document.getElementById("error-banner");
    let autoRefresh = true;

    const savedTok = sessionStorage.getItem("agsync_token") || "";
    if (savedTok) tokenInput.value = savedTok;
    tokenInput.addEventListener("change", () => {
      sessionStorage.setItem("agsync_token", tokenInput.value.trim());
      loadState();
    });

    function esc(v) {
      return String(v ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[c]));
    }

    function showErr(msg) {
      if (!msg) { errBanner.style.display = "none"; errBanner.textContent = ""; return; }
      errBanner.style.display = "block";
      errBanner.textContent = msg;
    }

    async function loadState() {
      const headers = {};
      const tok = tokenInput.value.trim();
      if (tok) headers["Authorization"] = "Bearer " + tok;
      try {
        const [hRes, sRes] = await Promise.all([
          fetch("/health"),
          fetch("/snapshot", { headers })
        ]);
        const health = await hRes.json();
        document.getElementById("hub-meta").textContent =
          `v${health.version} • Online: ${health.online_agents} • Leases: ${health.active_leases} • Tasks: ${health.tasks_count}`;

        if (sRes.status === 401) {
          showErr("Authentication required: enter the hub Bearer token above.");
          return;
        }
        if (!sRes.ok) {
          showErr("Failed to load snapshot (" + sRes.status + ").");
          return;
        }
        showErr("");
        const snap = await sRes.json();
        renderAgents(snap.agents || []);
        renderLeases(snap.active_leases || []);
        renderTasks(snap.tasks || []);
        renderContext(snap.context_keys || []);
      } catch (err) {
        showErr("Hub unreachable: " + err.message);
      }
    }

    function renderAgents(items) {
      document.getElementById("agents-count").textContent = String(items.length);
      const el = document.getElementById("agents-body");
      if (!items.length) { el.innerHTML = '<p class="empty">No agents registered yet.</p>'; return; }
      el.innerHTML = "<table><thead><tr><th>Agent</th><th>State</th><th>Machine</th><th>Branch</th></tr></thead><tbody>" +
        items.map(a => `<tr>
          <td><code>${esc(a.agent_id)}</code></td>
          <td><span class="badge ${a.is_online ? "ok" : "danger"}">${a.is_online ? "ONLINE" : "OFFLINE"}</span></td>
          <td>${esc(a.machine_name || "-")}</td>
          <td><code>${esc(a.branch || "-")}</code></td>
        </tr>`).join("") + "</tbody></table>";
    }

    function renderLeases(items) {
      document.getElementById("leases-count").textContent = String(items.length);
      const el = document.getElementById("leases-body");
      if (!items.length) { el.innerHTML = '<p class="empty">No active leases. All files are unlocked.</p>'; return; }
      el.innerHTML = "<table><thead><tr><th>Resource</th><th>Scope</th><th>Owner</th><th>Reason</th></tr></thead><tbody>" +
        items.map(l => `<tr>
          <td><code>${esc(l.resource_norm)}</code></td>
          <td>${esc(l.scope)}</td>
          <td><code>${esc(l.owner_id)}</code></td>
          <td>${esc(l.reason || "-")}</td>
        </tr>`).join("") + "</tbody></table>";
    }

    function renderTasks(items) {
      document.getElementById("tasks-count").textContent = String(items.length);
      const el = document.getElementById("tasks-body");
      if (!items.length) { el.innerHTML = '<p class="empty">No tasks created yet.</p>'; return; }
      el.innerHTML = "<table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Owner</th></tr></thead><tbody>" +
        items.map(t => `<tr>
          <td><code>${esc(t.task_id)}</code></td>
          <td>${esc(t.title)}</td>
          <td><span class="badge ${t.status === "done" ? "ok" : "warn"}">${esc(t.status)}</span></td>
          <td><code>${esc(t.owner_id || "-")}</code></td>
        </tr>`).join("") + "</tbody></table>";
    }

    function renderContext(items) {
      document.getElementById("context-count").textContent = String(items.length);
      const el = document.getElementById("context-body");
      if (!items.length) { el.innerHTML = '<p class="empty">No shared context keys set.</p>'; return; }
      el.innerHTML = "<table><thead><tr><th>Key</th><th>Version</th><th>Updated By</th></tr></thead><tbody>" +
        items.map(c => `<tr>
          <td><code>${esc(c.key)}</code></td>
          <td>v${esc(c.version)}</td>
          <td><code>${esc(c.updated_by)}</code></td>
        </tr>`).join("") + "</tbody></table>";
    }

    refreshBtn.addEventListener("click", loadState);
    autoBtn.addEventListener("click", () => {
      autoRefresh = !autoRefresh;
      autoBtn.textContent = autoRefresh ? "Auto: ON (3s)" : "Auto: OFF";
      autoBtn.setAttribute("aria-pressed", String(autoRefresh));
    });

    loadState();
    setInterval(() => { if (autoRefresh) loadState(); }, 3000);
  </script>
</body>
</html>
"""
