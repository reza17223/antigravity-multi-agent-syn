# هماهنگ‌ساز چند-ایجنتی آنتی‌گرویتی (`agsync`)

[فارسی](README.fa.md) | [English](README.md)

سرور هماهنگی شبکه محلی (LAN)، پل MCP، رابط خط فرمان (CLI)، کنترل‌پنل گرافیکی و هوک‌های محافظ ویرایش/کامیت — **کاملاً بدون وابستگی خارجی (فقط کتابخانه استاندارد Python 3.12+)** — تا دو یا چند همکار با **Antigravity** روی سیستم‌های جداگانه بتوانند هم‌زمان روی یک پروژه بدون بازنویسی کد یا تداخل قفل همکاری کنند.

---

## چرا `agsync` ساخته شده؟

هر نصب Antigravity به‌صورت محلی روی یک سیستم اجرا می‌شود (`language_server.exe` روی `localhost` و تنظیمات محلی `~/.gemini/config/`). Antigravity به‌صورت پیش‌فرض **امکان ارتباط مستقیم بین دو سیستم، قفل توزیع‌شده فایل‌ها یا تابلوی تسک مشترک ندارد**.

**معماری ترکیبی (Hybrid):**
- **لایه کد (`Git`)**: هر سیستم کلون و برنچ مستقل خودش را دارد (`agent-a/<task>` و `agent-b/<task>`)؛ بنابراین تداخل `.git/index.lock`، خرابی `node_modules`/`.venv` و بازنویسی خاموش فایل‌ها پیش نمی‌آید. هم با GitHub/GitLab و هم ۱۰۰٪ آفلاین روی LAN کار می‌کند.
- **لایه کنترل (`agsync`)**: یک هاب سبک Python 3.12 با پایگاه‌داده `SQLite WAL` روی یکی از سیستم‌ها اجرا می‌شود و از طریق **Streamable HTTP (`/mcp`)** یا **Stdio MCP (`agsync.mcp_bridge`)** به ایجنت‌ها متصل شده و با **هوک `PreToolUse` آنتی‌گرویتی** و **هوک `pre-commit` گیت** از فایل‌های قفل‌شده محافظت می‌کند.

---

## قابلیت‌های کلیدی (فاز ۱ / نسخه `v0.1.0`)

- **قفل انحصاری (Lease) با TTL و Fencing Token**: پشتیبانی از قفل فایل، پوشه و نماد (`file | dir | symbol`) همراه با تطبیق مسیرهای بدون حساسیت به حروف بزرگ/کوچک در ویندوز و حفظ مسیر منطقی در Directory Junctionها.
- **پیام‌رسانی زنده، تسک اتمیک و قرارداد مشترک**: پیام مستقیم و Broadcast با نشانگر خوانده‌شدن صریح (`POST /messages/ack`)، تصاحب اتمیک تسک (`409 Conflict` در برخورد هم‌زمان) و کلید-مقدار مشترک (`context_kv`) با کنترل نسخه خوش‌بینانه (`expected_version`).
- **سخت‌سازی امنیتی و هم‌زمانی**:
  - اجبار `Content-Type: application/json` روی متدهای تغییردهنده (`POST`/`PUT`/`PATCH`) و بررسی `Host`/`Origin` برای جلوگیری از CSRF و DNS-rebinding.
  - خواندنی‌بودن کامل `GET /messages` (حذف تغییر وضعیت در `GET` و انتقال Ack به `POST /messages/ack`).
  - اجبار Bearer Token هنگام بایند روی آدرس غیر-loopback مثل `0.0.0.0` (یا `--auto-token` / `--allow-insecure-lan` صریح).
  - استفاده از `SO_EXCLUSIVEADDRUSE` در ویندوز و فایل قفل `<db>.pid` برای جلوگیری از اجرای هم‌زمان دو هاب زامبی.
  - دور زدن خودکار پروکسی سیستم (`ProxyHandler({})`) برای آدرس‌های `127.0.0.1` و شبکه خصوصی LAN تا v2rayN / Clash / Happ ترافیک محلی را منحرف نکنند.
- **سازگاری با Worktree و فایل‌های BOM/UTF-16 ویندوز**:
  - تشخیص مسیر مشترک گیت با `git rev-parse --git-common-dir` و `--git-path hooks` (پشتیبانی از `git worktree` و `core.hooksPath`)، پرهیز از ثابت‌کردن مسیر venv در `pre-commit` و ادغام اتمیک و غیرمخرب (`.bak` + `os.replace`) برای `.agents/hooks.json` و `mcp_config.json`.
  - خواندن امن فایل‌های UTF-8 BOM / UTF-16 پاورشل و مسدودسازی ویرایش `.agsync/**`، `.agsync-agent` و `.agents/hooks.json` توسط ایجنت.
- **عیب‌یاب (`agsync doctor`) و داشبورد وب**: بررسی خودکار سلامت محیط، گیت، پروکسی، فایروال و هوک‌ها + داشبورد زنده در مسیر `GET /dashboard`.

---

## ساختار مخزن

```text
antigravity_multi_agent_sync/
├── README.md                       # مستندات انگلیسی
├── README.fa.md                    # مستندات فارسی (همین فایل)
├── pyproject.toml
├── Start_Sync_Panel.bat            # اجرای گرافیکی پنل کنترل یکپارچه در ویندوز
├── Start_Host_Panel.bat
├── Start_Peer_Panel.bat
├── .github/workflows/ci.yml        # CI خودکار گیت‌هاب (Windows, Ubuntu, macOS)
├── agsync/
│   ├── __init__.py
│   ├── db.py                       # اتصال SQLite WAL، migrationها با user_version و read_tx/write_tx
│   ├── protocol.py                 # اعتبارسنجی، نرمال‌سازی مسیر و اسکیمای فشرده MCP
│   ├── store.py                    # نمای سازگار SyncStore
│   ├── server.py                   # سرور HTTP سخت‌سازی‌شده (REST API + Streamable HTTP /mcp)
│   ├── client.py                   # کلاینت urllib با دور زدن پروکسی LAN
│   ├── mcp_bridge.py               # پل Stdio MCP (JSON-RPC 2.0)
│   ├── hook_guard.py               # هوک PreToolUse برای بررسی قفل‌ها و مسیرهای محافظت‌شده
│   ├── config.py                   # مدیریت تنظیمات کاربر (~/.agsync/) و ثبت MCP آنتی‌گرویتی
│   ├── discovery.py                # کشف خودکار هاب روی LAN با UDP
│   ├── dashboard.py                # داشبورد وب زنده (/dashboard)
│   ├── cli.py                      # نقطه ورود خط فرمان
│   ├── core/
│   │   ├── errors.py               # کلاس‌های خطای استاندارد HTTP/JSON
│   │   ├── locks.py                # منطق صدور، تمدید، آزادسازی و بررسی قفل‌ها
│   │   ├── events.py               # حضور ایجنت‌ها، پیام‌ها، تسک‌ها، context و export/import
│   │   └── git_ops.py              # عملیات گیت (common-dir، hooks، فایل‌های staged، نسخه گیت)
│   ├── adapters/
│   │   ├── network.py              # دور زدن پروکسی، قفل PID، تست TCP، Windows Job Object
│   │   └── ide.py                  # خواندن BOM/UTF-16، نوشتن اتمیک و ادغام غیرمخرب JSON
│   └── commands/
│       ├── doctor.py               # دستورات تشخیص خطای `agsync doctor`
│       ├── hooks.py                # هندلرهای `guard` و `install-hooks`
│       └── sync_cmds.py            # دستورات استاندارد کلاینت در CLI
├── templates/
│   ├── AGENTS.md                   # قوانین آماده همکاری چند-ایجنتی برای پروژه
│   ├── mcp_config.http.json
│   ├── mcp_config.stdio.json
│   └── .agents/
│       ├── hooks.json
│       └── skills/                 # اسکیل‌های sync-start، sync-check و sync-handoff
├── docs/
│   ├── ARCHITECTURE.md
│   ├── OVERVIEW.fa.md
│   ├── ROADMAP.fa.md
│   ├── RUBRIC.md
│   ├── SETUP_GUIDE.md
│   └── SETUP_GUIDE.fa.md
├── scripts/
│   ├── control_panel.py            # پنل گرافیکی Tkinter (Host/Peer، Doctor، فایروال، MCP و Hooks)
│   └── smoke_check.py              # تست سرتاسری (--spawn-local یا --server URL)
└── tests/                          # مجموعه تست‌های واحد و یکپارچه (۳۸ تست)
```

---

## شروع سریع

### روش اول: پنل گرافیکی کنترل (پیشنهادی در ویندوز)
1. روی `Start_Sync_Panel.bat` دبل‌کلیک کنید (یا `python scripts/control_panel.py` را اجرا کنید).
2. **روی سیستم میزبان (Host):** نقش را روی `HOST (Hub)` بگذارید، `Agent ID` را مشخص کنید، با **Generate Token** توکن بسازید (یا بگذارید هنگام شروع خودکار ساخته شود)، در صورت نیاز **Open Firewall (Admin)** را بزنید و روی **Start Hub Server** کلیک کنید.
3. **روی سیستم همکار (Peer):** نقش را روی `PEER (Client)` بگذارید، آی‌پی LAN میزبان را وارد کنید (یا **Discover LAN Hub** را بزنید)، همان توکن را وارد کرده و **Test Connection (TCP + /health)** را بزنید.
4. روی هر دو سیستم، پوشه پروژه مشترک را انتخاب کنید و دکمه‌های **Connect Antigravity MCP** و **Install Repo Hooks & Templates** را بزنید.

### روش دوم: خط فرمان (CLI)
1. **اجرای هاب روی سیستم میزبان** (مسیر پیش‌فرض دیتابیس `~/.agsync/state.db` است):
   ```powershell
   $env:AGSYNC_TOKEN = "your-shared-secret"
   python -m agsync.cli serve --host 0.0.0.0 --port 8765
   ```
   *(برای تست محلی فقط روی همین سیستم، `--host 127.0.0.1` بدون توکن هم کار می‌کند.)*
2. **بررسی سلامت و عیب‌یابی:**
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8765/health
   python -m agsync.cli doctor --repo C:\path\to\your\project
   ```
3. **اتصال MCP آنتی‌گرویتی و نصب هوک‌ها روی پروژه:**
   ```powershell
   python -m agsync.cli setup-mcp --server http://127.0.0.1:8765 --agent machine-a --token your-shared-secret
   python -m agsync.cli install-hooks --repo C:\path\to\your\project --server http://127.0.0.1:8765 --agent machine-a --with-antigravity-hook --with-templates
   ```

> **نکته PowerShell 5.1:** هنگام ارسال JSON داخل دستور `python -m agsync.cli context set`، دابل‌کوتیشن‌های داخلی را Escape کنید (`'{\"port\":8765}'`) تا پاورشل آن‌ها را حذف نکند.

---

## تست و عیب‌یابی

```powershell
# اجرای کامل مجموعه تست‌ها (۳۸ تست، بدون نیاز به کتابخانه خارجی)
python -m unittest discover -s tests -v

# اجرای تست سرتاسری همراه با سرور موقت محلی
python scripts/smoke_check.py --spawn-local

# اجرای عیب‌یاب محیط، گیت worktree، پروکسی، BOM/UTF-16 و هوک
python -m agsync.cli doctor --repo .
```

---

## مستندات تکمیلی
- **نسخه انگلیسی README:** [README.md](README.md)
- **معرفی ساده و کامل (فارسی):** [docs/OVERVIEW.fa.md](docs/OVERVIEW.fa.md)
- **راهنمای گام‌به‌گام نصب:** [docs/SETUP_GUIDE.fa.md](docs/SETUP_GUIDE.fa.md) | [English](docs/SETUP_GUIDE.md)
- **تحلیل معماری:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **نقشه راه پروژه:** [docs/ROADMAP.fa.md](docs/ROADMAP.fa.md)
