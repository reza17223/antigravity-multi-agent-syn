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
      color-scheme: light dark;
      --bg: #f6f8fa;
      --surface: #ffffff;
      --surface-alt: #f0f3f6;
      --border: #d0d7de;
      --control-border: #76808a;
      --text: #1f2328;
      --muted: #59636e;
      --accent: #0969da;
      --accent-fg: #ffffff;
      --ok: #116329;
      --ok-bg: #dafbe1;
      --warn: #9a6700;
      --warn-bg: #fff8c5;
      --danger: #cf222e;
      --danger-bg: #ffebe9;
      --info: #0550ae;
      --info-bg: #ddf4ff;
      --focus: #0969da;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0d1117;
        --surface: #161b22;
        --surface-alt: #21262d;
        --border: #30363d;
        --control-border: #6e7681;
        --text: #e6edf3;
        --muted: #8b949e;
        --accent: #1f6feb;
        --accent-fg: #ffffff;
        --ok: #3fb950;
        --ok-bg: rgba(46, 160, 67, 0.15);
        --warn: #d29922;
        --warn-bg: rgba(187, 128, 9, 0.15);
        --danger: #ff7b72;
        --danger-bg: rgba(248, 81, 73, 0.15);
        --info: #79c0ff;
        --info-bg: rgba(56, 139, 253, 0.15);
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
    .wrap { max-width: 1240px; margin: 0 auto; padding: 24px 16px; }
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
    .subtitle { color: var(--muted); font-size: 14px; margin: 4px 0 0 0; }
    .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    button, input, select {
      font: inherit;
      font-size: 14px;
      border-radius: 6px;
      border: 1px solid var(--control-border);
      padding: 8px 12px;
      background: var(--surface);
      color: var(--text);
      min-height: 44px;
      transition: background-color 150ms ease, border-color 150ms ease, opacity 150ms ease;
    }
    button {
      cursor: pointer;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    button.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
    button.active { background: var(--info-bg); color: var(--info); border-color: var(--info); }
    button:focus-visible, input:focus-visible, select:focus-visible {
      outline: 3px solid var(--focus);
      outline-offset: 2px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
      gap: 16px;
      margin-bottom: 16px;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    .card-header {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 12px;
    }
    .card h2 {
      font-size: 16px;
      margin: 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .card-tools {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .card-tools label {
      font-size: 13px;
      color: var(--muted);
      font-weight: 600;
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
      white-space: nowrap;
    }
    .badge.ok { background: var(--ok-bg); color: var(--ok); }
    .badge.warn { background: var(--warn-bg); color: var(--warn); }
    .badge.danger { background: var(--danger-bg); color: var(--danger); }
    .badge.info { background: var(--info-bg); color: var(--info); }
    .table-wrap {
      overflow-x: auto;
      max-width: 100%;
    }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td {
      text-align: left;
      padding: 8px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; white-space: nowrap; }
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
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="6" cy="6" r="3"></circle>
          <circle cx="18" cy="6" r="3"></circle>
          <circle cx="12" cy="18" r="3"></circle>
          <path d="M8.5 7.5 11 15M15.5 7.5 13 15M9 6h6"></path>
        </svg>
        <div>
          <h1>agsync Coordination Hub</h1>
          <p class="subtitle" id="hub-meta" role="status" aria-atomic="true">Connecting...</p>
        </div>
      </div>
      <div class="controls">
        <label for="token-input" class="sr-only">Bearer token (optional)</label>
        <input id="token-input" type="password" placeholder="Bearer token (if enabled)" aria-label="Bearer token">
        <button id="refresh-btn" class="primary" type="button">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 1 1-2.64-6.36"></path>
            <polyline points="21 3 21 9 15 9"></polyline>
          </svg>
          <span>Refresh Now</span>
        </button>
        <button id="auto-btn" class="active" type="button" aria-pressed="true">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="9"></circle>
            <polyline points="12 7 12 12 15 14"></polyline>
          </svg>
          <span id="auto-label">Live: ON</span>
        </button>
        <button id="notify-btn" type="button" aria-pressed="false">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"></path>
            <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
          </svg>
          <span id="notify-label">Alerts: OFF</span>
        </button>
      </div>
    </header>

    <div id="error-banner" class="banner" role="alert"></div>

    <main>
      <div class="grid">
        <section class="card" aria-labelledby="agents-heading">
          <div class="card-header">
            <h2 id="agents-heading">Connected Agents <span class="badge ok" id="agents-count">0</span></h2>
          </div>
          <div id="agents-body"></div>
        </section>

        <section class="card" aria-labelledby="leases-heading">
          <div class="card-header" style="display:flex; justify-content:space-between; align-items:center;">
            <h2 id="leases-heading">Active File &amp; Scope Leases <span class="badge warn" id="leases-count">0</span></h2>
            <button class="btn btn-sm btn-subtle" onclick="releaseAllLeases()" title="Release all file locks">🔓 Unlock All</button>
          </div>
          <div id="leases-body"></div>
        </section>
      </div>

      <div class="grid">
        <section class="card" aria-labelledby="tasks-heading">
          <div class="card-header">
            <h2 id="tasks-heading">Shared Task Board <span class="badge ok" id="tasks-count">0</span></h2>
          </div>
          <div id="tasks-body"></div>
        </section>

        <section class="card" aria-labelledby="context-heading">
          <div class="card-header">
            <h2 id="context-heading">Shared Context Keys <span class="badge info" id="context-count">0</span></h2>
          </div>
          <div id="context-body"></div>
        </section>
      </div>

      <section class="card" aria-labelledby="feed-heading">
        <div class="card-header">
          <h2 id="feed-heading">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
            </svg>
            <span>Live Activity Feed</span>
            <span class="badge info" id="feed-count">0</span>
          </h2>
          <div class="card-tools">
            <label for="feed-filter">فیلتر</label>
            <select id="feed-filter" style="min-height:36px;font-size:13px;">
              <option value="all">رویدادهای تیمی (Team Activities)</option>
              <option value="message">[Messages] پیام‌ها</option>
              <option value="lease">[Leases] قفل و آزادسازی فایل‌ها</option>
              <option value="task">[Tasks] تسک‌ها</option>
              <option value="agent">[Agents] حضور ایجنت‌ها</option>
              <option value="raw">[All Raw] همه رویدادها شامل سیستمی</option>
            </select>
          </div>
        </div>
        <div id="feed-body"></div>
      </section>
    </main>
  </div>

  <script>
    const tokenInput = document.getElementById("token-input");
    const refreshBtn = document.getElementById("refresh-btn");
    const autoBtn = document.getElementById("auto-btn");
    const autoLabel = document.getElementById("auto-label");
    const notifyBtn = document.getElementById("notify-btn");
    const notifyLabel = document.getElementById("notify-label");
    const feedFilter = document.getElementById("feed-filter");
    const errBanner = document.getElementById("error-banner");

    const MAX_FEED_ITEMS = 100;
    const MAX_SEEN_EVENT_IDS = 500;
    let liveEnabled = true;
    let notifyEnabled = false;
    let currentBootId = "";
    let sinceId = 0;
    let pollAbortCtrl = null;
    let pollLoopSeq = 0;
    let feedItems = [];
    const seenEventIds = new Set();

    const savedTok = (typeof sessionStorage !== "undefined" && sessionStorage.getItem("agsync_token")) || "";
    if (savedTok) tokenInput.value = savedTok;
    try {
      if (typeof window !== "undefined" && window.location) {
        const urlParams = new URLSearchParams(window.location.search || "");
        const hashParams = new URLSearchParams((window.location.hash || "").replace(/^#/, ""));
        const paramTok = urlParams.get("token") || hashParams.get("token");
        if (paramTok) {
          tokenInput.value = paramTok;
          if (typeof sessionStorage !== "undefined") sessionStorage.setItem("agsync_token", paramTok);
        }
      }
    } catch (_e) {}

    function getAuthHeaders() {
      const headers = {};
      const tok = tokenInput.value.trim();
      if (tok) headers["Authorization"] = "Bearer " + tok;
      return headers;
    }

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

    function formatTime(ts) {
      if (!ts) return "-";
      const d = new Date(Number(ts) * 1000);
      if (Number.isNaN(d.getTime())) return "-";
      return d.toLocaleTimeString([], { hour12: false });
    }

    function formatEventBadge(evType) {
      const t = String(evType || "");
      if (t === "message_sent") return '<span class="badge info">پیام جدید</span>';
      if (t === "lease_acquired") return '<span class="badge warn">قفل فایل</span>';
      if (t === "lease_released") return '<span class="badge ok">آزادسازی قفل</span>';
      if (t === "lease_expired") return '<span class="badge danger">انقضای قفل</span>';
      if (t === "agent_registered") return '<span class="badge ok">حضور ایجنت</span>';
      if (t === "task_created") return '<span class="badge ok">تسک جدید</span>';
      if (t === "task_updated") return '<span class="badge info">آپدیت تسک</span>';
      if (t === "messages_acked") return '<span class="badge" style="opacity:0.6;">تایید دریافت</span>';
      return `<span class="badge info">${esc(t)}</span>`;
    }

    function formatSummary(ev) {
      const evType = String(ev.event_type || "");
      const payload = ev.payload || {};
      const actor = esc(ev.actor_id || "-");
      const target = esc(ev.target_id || "-");

      if (evType === "message_sent") {
        const bodyTxt = esc(payload.body || ev.summary || "");
        return `<span style="font-weight:600;">${actor}</span> -&gt; <span style="font-weight:600;">${target}</span>: <span style="color:var(--accent);font-style:italic;">"${bodyTxt}"</span>`;
      }
      if (evType === "lease_acquired") {
        return `<span style="font-weight:600;">${actor}</span> فایل <code>${target}</code> را موقتاً قفل کرد تا تداخلی رخ ندهد.`;
      }
      if (evType === "lease_released") {
        return `<span style="font-weight:600;">${actor}</span> ویرایش <code>${target}</code> را تمام و قفل را آزاد کرد.`;
      }
      if (evType === "lease_expired") {
        return `قفل فایل <code>${target}</code> به دلیل بی‌کاری منقضی و برای دیگران آزاد شد.`;
      }
      if (evType === "agent_registered") {
        const st = esc(payload.status || "online");
        return `ایجنت <span style="font-weight:600;">${actor}</span> فعال شد (وضعیت: <b>${st}</b>).`;
      }
      if (evType === "task_created") {
        const title = esc(payload.title || target);
        return `<span style="font-weight:600;">${actor}</span> تسک جدید ایجاد کرد: <b>${title}</b>`;
      }
      if (evType === "task_updated") {
        const st = esc(payload.status || "");
        return `وضعیت تسک <code>${target}</code> به <b>${st}</b> تغییر یافت.`;
      }
      return esc(ev.summary || (actor + " -> " + target));
    }

    function matchesFeedFilter(evType, filterVal) {
      const t = String(evType || "");
      if (filterVal === "all") {
        // Hide noisy internal ACK bookkeeping in the main feed
        return t !== "messages_acked";
      }
      if (filterVal === "raw") return true;
      if (filterVal === "lease") return t.startsWith("lease_");
      if (filterVal === "task") return t.startsWith("task_");
      if (filterVal === "message") return t.startsWith("message");
      if (filterVal === "agent") return t.startsWith("agent_");
      return true;
    }

    function trackSeenEventId(id) {
      if (seenEventIds.has(id)) return false;
      seenEventIds.add(id);
      while (seenEventIds.size > MAX_SEEN_EVENT_IDS) {
        const oldest = seenEventIds.values().next().value;
        seenEventIds.delete(oldest);
      }
      return true;
    }

    function seedFeed(events) {
      feedItems = [];
      seenEventIds.clear();
      const list = Array.isArray(events) ? events : [];
      for (const ev of list) {
        if (!ev || typeof ev.id !== "number") continue;
        if (trackSeenEventId(ev.id)) {
          feedItems.push(ev);
        }
      }
      if (feedItems.length > MAX_FEED_ITEMS) {
        feedItems = feedItems.slice(feedItems.length - MAX_FEED_ITEMS);
      }
      renderFeed();
    }

    function appendFeedEvents(events) {
      const added = [];
      const list = Array.isArray(events) ? events : [];
      for (const ev of list) {
        if (!ev || typeof ev.id !== "number") continue;
        if (trackSeenEventId(ev.id)) {
          feedItems.push(ev);
          added.push(ev);
        }
      }
      if (feedItems.length > MAX_FEED_ITEMS) {
        feedItems = feedItems.slice(feedItems.length - MAX_FEED_ITEMS);
      }
      if (added.length > 0) {
        renderFeed();
      }
      return added;
    }

    function renderFeed() {
      const filterVal = feedFilter.value || "all";
      const visible = feedItems
        .filter(ev => matchesFeedFilter(ev.event_type, filterVal))
        .slice()
        .reverse();
      document.getElementById("feed-count").textContent = String(visible.length);
      const el = document.getElementById("feed-body");
      if (!visible.length) {
        el.innerHTML = '<p class="empty">رویدادی برای نمایش با این فیلتر وجود ندارد.</p>';
        return;
      }
      el.innerHTML = '<div class="table-wrap"><table><thead><tr><th style="width:110px;">زمان</th><th style="width:140px;">نوع رویداد</th><th>شرح رویداد (Summary)</th></tr></thead><tbody>' +
        visible.map(ev => `<tr>
          <td style="white-space:nowrap;"><code>${esc(formatTime(ev.created_at))}</code></td>
          <td style="white-space:nowrap;">${formatEventBadge(ev.event_type)}</td>
          <td>${formatSummary(ev)}</td>
        </tr>`).join("") + "</tbody></table></div>";
    }

    function syncNotifyButton() {
      if (typeof Notification === "undefined") {
        notifyEnabled = false;
        notifyBtn.disabled = true;
        notifyBtn.classList.remove("active");
        notifyBtn.setAttribute("aria-pressed", "false");
        notifyLabel.textContent = "Alerts: Unsupported";
        return;
      }
      if (Notification.permission === "denied") {
        notifyEnabled = false;
        notifyBtn.disabled = false;
        notifyBtn.classList.remove("active");
        notifyBtn.setAttribute("aria-pressed", "false");
        notifyLabel.textContent = "Alerts: Blocked";
        return;
      }
      if (notifyEnabled && Notification.permission === "granted") {
        notifyBtn.classList.add("active");
        notifyBtn.setAttribute("aria-pressed", "true");
        notifyLabel.textContent = "Alerts: ON";
      } else {
        notifyEnabled = false;
        notifyBtn.classList.remove("active");
        notifyBtn.setAttribute("aria-pressed", "false");
        notifyLabel.textContent = "Alerts: OFF";
      }
    }

    function dispatchBrowserNotifications(newEvents) {
      if (!notifyEnabled || typeof Notification === "undefined") return;
      if (Notification.permission !== "granted") return;
      for (const ev of newEvents.slice(-3)) {
        const title = "agsync: " + String(ev.event_type || "event");
        const body = String(ev.summary || (ev.actor_id + " -> " + ev.target_id));
        try {
          new Notification(title, { body, tag: "agsync-ev-" + String(ev.id) });
        } catch (_err) {
          // Ignore browser Notification runtime restrictions
        }
      }
    }

    async function loadSnapshot({ seedEvents = false } = {}) {
      const headers = getAuthHeaders();
      const tok = tokenInput.value.trim();
      const tokQuery = tok ? ("?token=" + encodeURIComponent(tok)) : "";
      try {
        const [hRes, sRes] = await Promise.all([
          fetch("/health"),
          fetch("/snapshot" + tokQuery, { headers })
        ]);
        const health = await hRes.json();
        const bootShort = health.boot_id ? (" | Boot: " + health.boot_id.slice(0, 8)) : "";
        document.getElementById("hub-meta").textContent =
          `v${health.version}${bootShort} | Online: ${health.online_agents} | Leases: ${health.active_leases} | Tasks: ${health.tasks_count}`;

        if (sRes.status === 401) {
          showErr("احراز هویت لازم است: لطفاً توکن (Bearer Token) را در کادر بالای صفحه وارد کنید. / Authentication required: enter token above.");
          return false;
        }
        if (!sRes.ok) {
          showErr("Failed to load snapshot (" + sRes.status + ").");
          return false;
        }
        showErr("");
        const snap = await sRes.json();
        renderAgents(snap.agents || []);
        renderLeases(snap.active_leases || []);
        renderTasks(snap.tasks || []);
        renderContext(snap.context_keys || []);
        if (seedEvents) {
          if (snap.boot_id) currentBootId = String(snap.boot_id);
          if (typeof snap.latest_event_id === "number") {
            sinceId = Math.max(sinceId, snap.latest_event_id);
          }
          if (Array.isArray(snap.latest_events)) {
            seedFeed(snap.latest_events);
          } else {
            const evRes = await fetch("/events?since_id=0&limit=30&wait=0", { headers });
            if (evRes.ok) {
              const evData = await evRes.json();
              if (evData.boot_id) currentBootId = String(evData.boot_id);
              if (typeof evData.latest_id === "number") sinceId = evData.latest_id;
              seedFeed(evData.events || []);
            }
          }
        } else if (!currentBootId && snap.boot_id) {
          currentBootId = String(snap.boot_id);
        }
        return true;
      } catch (err) {
        showErr("Hub unreachable: " + err.message);
        return false;
      }
    }

    function abortLongPoll() {
      if (pollAbortCtrl) {
        pollAbortCtrl.abort();
        pollAbortCtrl = null;
      }
    }

    function sleepAbortable(ms, signal) {
      return new Promise(resolve => {
        if (signal && signal.aborted) { resolve(); return; }
        const timer = setTimeout(resolve, ms);
        if (signal) {
          signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
        }
      });
    }

    async function startLongPollLoop() {
      abortLongPoll();
      if (!liveEnabled) return;
      const mySeq = ++pollLoopSeq;

      while (liveEnabled && mySeq === pollLoopSeq) {
        const ctrl = new AbortController();
        pollAbortCtrl = ctrl;
        try {
          const qs = new URLSearchParams({
            since_id: String(sinceId),
            wait: "15",
            limit: "100"
          });
          if (currentBootId) qs.set("boot_id", currentBootId);
          const res = await fetch("/events?" + qs.toString(), {
            headers: getAuthHeaders(),
            signal: ctrl.signal
          });
          if (mySeq !== pollLoopSeq || !liveEnabled) break;
          if (res.status === 401) {
            showErr("Authentication required: enter the hub Bearer token above.");
            break;
          }
          if (!res.ok) {
            showErr("Event stream error (" + res.status + "). Retrying...");
            await sleepAbortable(2000, ctrl.signal);
            continue;
          }
          showErr("");
          const data = await res.json();
          if (data.reset) {
            if (data.boot_id) currentBootId = String(data.boot_id);
            sinceId = typeof data.latest_id === "number" ? data.latest_id : 0;
            seedFeed(data.events || []);
            await loadSnapshot({ seedEvents: false });
          } else {
            if (data.boot_id) currentBootId = String(data.boot_id);
            if (typeof data.latest_id === "number") {
              sinceId = Math.max(sinceId, data.latest_id);
            }
            const added = appendFeedEvents(data.events || []);
            if (added.length > 0) {
              dispatchBrowserNotifications(added);
              await loadSnapshot({ seedEvents: false });
            }
          }
        } catch (err) {
          if (err && err.name === "AbortError") break;
          if (mySeq !== pollLoopSeq || !liveEnabled) break;
          showErr("Event stream disconnected: " + (err && err.message ? err.message : "network error"));
          await sleepAbortable(2000, ctrl.signal);
        } finally {
          if (pollAbortCtrl === ctrl) pollAbortCtrl = null;
        }
      }
    }

    async function bootstrapDashboard() {
      abortLongPoll();
      sinceId = 0;
      const ok = await loadSnapshot({ seedEvents: true });
      if (ok && liveEnabled) {
        startLongPollLoop();
      }
    }

    function agentStateBadge(a) {
      if (!a.is_online) {
        return '<span class="badge danger" style="padding:4px 10px;font-size:12px;">OFFLINE (قطع)</span>';
      }
      const st = String(a.status || a.reported_status || "").toLowerCase();
      const now = Date.now() / 1000;
      const lastHb = Number(a.last_heartbeat || 0);
      const isStale = (now - lastHb) > 70;
      if (st === "idle" || isStale) {
        return '<span class="badge warn" style="padding:4px 10px;font-size:12px;">IDLE (آماده‌باش)</span>';
      }
      if (st === "busy") {
        return '<span class="badge info" style="padding:4px 10px;font-size:12px;">BUSY (مشغول)</span>';
      }
      return '<span class="badge ok" style="padding:4px 10px;font-size:12px;">ONLINE (فعال)</span>';
    }

    function renderAgents(items) {
      document.getElementById("agents-count").textContent = String(items.length);
      const el = document.getElementById("agents-body");
      if (!items.length) { el.innerHTML = '<p class="empty">هیچ ایجنتی هنوز ثبت نشده است.</p>'; return; }
      el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>ایجنت (Agent)</th><th>وضعیت (Status)</th><th>سیستم (Machine)</th><th>شاخه (Branch)</th></tr></thead><tbody>' +
        items.map(a => `<tr>
          <td><code>${esc(a.agent_id)}</code></td>
          <td>${agentStateBadge(a)}</td>
          <td>${esc(a.machine_name || "-")}</td>
          <td><code>${esc(a.branch || "-")}</code></td>
        </tr>`).join("") + "</tbody></table></div>";
    }

    function renderLeases(items) {
      document.getElementById("leases-count").textContent = String(items.length);
      const el = document.getElementById("leases-body");
      if (!items.length) { el.innerHTML = '<p class="empty">No active leases. All files are unlocked.</p>'; return; }
      el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>Resource</th><th>Scope</th><th>Owner</th><th>Reason</th><th style="width:100px;">Action</th></tr></thead><tbody>' +
        items.map(l => `<tr>
          <td><code>${esc(l.resource_norm)}</code></td>
          <td>${esc(l.scope)}</td>
          <td><code>${esc(l.owner_id)}</code></td>
          <td>${esc(l.reason || "-")}</td>
          <td><button class="btn btn-sm btn-warn" onclick="releaseLease('${esc(l.lease_id)}', '${esc(l.owner_id)}')">🔓 Unlock</button></td>
        </tr>`).join("") + "</tbody></table></div>";
    }

    async function releaseLease(leaseId, ownerId) {
      try {
        const res = await fetch("/leases/release", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lease_id: leaseId, owner_id: ownerId })
        });
        if (res.ok) {
          fetchSnapshot();
        } else {
          const err = await res.json().catch(() => ({}));
          alert("Could not release lease: " + (err.error || err.message || res.statusText));
        }
      } catch (e) {
        alert("Unlock error: " + e);
      }
    }

    async function releaseAllLeases() {
      if (!confirm("Release all active leases across all agents?")) return;
      try {
        const snap = await (await fetch("/snapshot")).json();
        const leases = snap.active_leases || [];
        for (const l of leases) {
          await fetch("/leases/release", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ lease_id: l.lease_id, owner_id: l.owner_id })
          });
        }
        fetchSnapshot();
      } catch (e) {
        alert("Error unlocking all: " + e);
      }
    }

    function renderTasks(items) {
      document.getElementById("tasks-count").textContent = String(items.length);
      const el = document.getElementById("tasks-body");
      if (!items.length) { el.innerHTML = '<p class="empty">No tasks created yet.</p>'; return; }
      el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Created By (سازنده)</th><th>Assignee (انجام‌دهنده)</th></tr></thead><tbody>' +
        items.map(t => {
          let stBadge = "info";
          let stLabel = esc(t.status);
          if (t.status === "done") { stBadge = "ok"; stLabel = "done (تکمیل)"; }
          else if (t.status === "in_progress") { stBadge = "warn"; stLabel = "in_progress (در حال انجام)"; }
          else if (t.status === "open") { stBadge = "info"; stLabel = "open (آزاد)"; }
          const assignee = t.owner_id ? `<code>${esc(t.owner_id)}</code>` : '<span class="badge info">Unassigned (تعیین‌نشده)</span>';
          return `<tr>
            <td><code>${esc(t.task_id)}</code></td>
            <td>${esc(t.title)}</td>
            <td><span class="badge ${stBadge}">${stLabel}</span></td>
            <td><code>${esc(t.created_by || "-")}</code></td>
            <td>${assignee}</td>
          </tr>`;
        }).join("") + "</tbody></table></div>";
    }

    function renderContext(items) {
      document.getElementById("context-count").textContent = String(items.length);
      const el = document.getElementById("context-body");
      if (!items.length) { el.innerHTML = '<p class="empty">No shared context keys set.</p>'; return; }
      el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>Key</th><th>Version</th><th>Updated By</th></tr></thead><tbody>' +
        items.map(c => `<tr>
          <td><code>${esc(c.key)}</code></td>
          <td>v${esc(c.version)}</td>
          <td><code>${esc(c.updated_by)}</code></td>
        </tr>`).join("") + "</tbody></table></div>";
    }

    tokenInput.addEventListener("change", () => {
      sessionStorage.setItem("agsync_token", tokenInput.value.trim());
      bootstrapDashboard();
    });

    refreshBtn.addEventListener("click", () => {
      bootstrapDashboard();
    });

    autoBtn.addEventListener("click", () => {
      liveEnabled = !liveEnabled;
      autoLabel.textContent = liveEnabled ? "Live: ON" : "Live: OFF";
      autoBtn.setAttribute("aria-pressed", String(liveEnabled));
      autoBtn.classList.toggle("active", liveEnabled);
      if (liveEnabled) {
        startLongPollLoop();
      } else {
        abortLongPoll();
      }
    });

    notifyBtn.addEventListener("click", async () => {
      if (typeof Notification === "undefined") {
        syncNotifyButton();
        return;
      }
      if (notifyEnabled) {
        notifyEnabled = false;
        syncNotifyButton();
        return;
      }
      if (Notification.permission === "default") {
        try {
          await Notification.requestPermission();
        } catch (_err) {
          // Ignore denied/unsupported errors
        }
      }
      notifyEnabled = Notification.permission === "granted";
      syncNotifyButton();
    });

    feedFilter.addEventListener("change", renderFeed);
    window.addEventListener("beforeunload", abortLongPoll);

    syncNotifyButton();
    bootstrapDashboard();
  </script>
</body>
</html>
"""
