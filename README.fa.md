# `agsync` — هماهنگ‌ساز جامع چند-ایجنتی و چند-IDE

[فارسی](README.fa.md) | [English](README.md)

[![CI](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml/badge.svg)](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml)

سرور هماهنگی شبکه محلی (LAN)، سرور MCP، رابط خط فرمان (CLI)، کنترل‌پنل گرافیکی و هوک‌های محافظ ویرایش/کامیت — **کاملاً بدون وابستگی خارجی (فقط کتابخانه استاندارد Python 3.12+)** — تا **هر نوع ایجنت کدنویسی هوش مصنوعی** از جمله **Antigravity، Claude Code، Cursor، Windsurf، Codex، Gemini CLI یا اسکریپت‌های CLI** روی سیستم‌های جداگانه یا فضای کاری موازی بتوانند بدون بازنویسی کد یا تداخل قفل روی یک پروژه همکاری کنند.

---

## چرا `agsync` ساخته شده؟

ابزارهای کدنویسی هوش مصنوعی به‌صورت محلی روی سیستم هر برنامه‌نویس اجرا می‌شوند و **امکان ارتباط مستقیم بین دو سیستم، قفل توزیع‌شده فایل‌ها یا تابلوی تسک مشترک ندارند**. اشتراک‌گذاری مستقیم پوشه پروژه از طریق SMB، OneDrive یا Syncthing باعث بازنویسی خاموش کد، خرابی `.git/index.lock` و تداخل `node_modules` و `.venv` می‌شود.

**معماری هیبرید (لایه کد + لایه کنترل):**
- **لایه کد (`Git`)**: هر ایجنت یا سیستم کلون (یا worktree) و برنچ مستقل خودش را دارد (`agent-a/<task>` و `agent-b/<task>`). هم با GitHub/GitLab و هم ۱۰۰٪ آفلاین روی شبکه محلی (LAN) کار می‌کند.
- **لایه کنترل (`agsync`)**: یک سرور سبک Python 3.12 (`SQLite WAL` + `PRAGMA user_version`) روی یکی از سیستم‌ها اجرا می‌شود و از طریق **Streamable HTTP (`/mcp`)**، **Stdio MCP (`agsync.mcp_bridge`)** و **REST JSON** به همه ایجنت‌ها سرویس می‌دهد و با هوک‌های **`PreToolUse`** و **`pre-commit`** جلوی ویرایش یا کامیت فایل قفل‌شده را می‌گیرد.

---

## ایجنت‌ها و IDEهای سازگار

هر ابزار یا ایجنتی که با **MCP** (HTTP یا Stdio)، **REST API** یا **CLI** کار کند مستقیماً پشتیبانی می‌شود:

| ایجنت / کلاینت | روش اتصال | وضعیت |
|---|---|---|
| **Antigravity** | Streamable HTTP (`/mcp`) یا Stdio + `.agents/hooks.json` | پشتیبانی کامل (تنظیم خودکار با `agsync setup-mcp`) |
| **Claude Code / Cursor / Windsurf / Cline** | تنظیم استاندارد MCP (`url` یا `python -m agsync.mcp_bridge`) + `AGENTS.md` | پشتیبانی کامل از طریق MCP و CLI |
| **Codex / Gemini CLI / اسکریپت‌های سفارشی** | Stdio MCP یا دستورات `python -m agsync.cli` / HTTP REST | پشتیبانی کامل |

*(در فاز ۲ پراکسی یکپارچه هوک `agsync hook-proxy --host <ide>` برای هوک‌های بومی Claude Code و Cursor و مدیریت `git worktree` نیز اضافه می‌شود.)*

---

## قابلیت‌های کلیدی (`v0.1.0` — فاز ۱)

- **قفل انحصاری (Lease) با TTL و Fencing Token**: قفل سلسله‌مراتبی (`file`، `dir`، `symbol`) با تطبیق مسیرهای بدون حساسیت به حروف بزرگ/کوچک در ویندوز و حفظ مسیر منطقی در Directory Junctionها.
- **تقسیم تسک اتمیک و قرارداد مشترک**: رد تصاحب هم‌زمان تسک با `409 Conflict`، کلید-مقدار مشترک (`context_kv`) با کنترل نسخه خوش‌بینانه (`expected_version`) و پیام‌رسانی مستقیم/گروهی با تایید خواندن صریح (`POST /messages/ack`).
- **سخت‌سازی امنیتی و هم‌زمانی**:
  - اجبار `Content-Type: application/json` (`415`) و اعتبارسنجی `Host`/`Origin` (`403`) در برابر CSRF و DNS-rebinding.
  - خواندنی‌بودن کامل `GET /messages` (تغییر نشانگر خواندن فقط از طریق `POST /messages/ack`).
  - اجبار Bearer Token (`--token`، `--auto-token` یا `--allow-insecure-lan`) هنگام بایند روی `0.0.0.0`.
  - قفل پورت `SO_EXCLUSIVEADDRUSE` در ویندوز و فایل `<db>.pid` برای جلوگیری از هاب زامبی.
  - دور زدن خودکار پروکسی سیستم (`ProxyHandler({})`) برای آدرس‌های محلی و شبکه خصوصی LAN (جلوگیری از اختلال v2rayN / Clash / Happ).
- **سازگاری با Worktree، BOM/UTF-16 و مسیرهای محافظت‌شده**:
  - استفاده از `git rev-parse --git-common-dir` و `--git-path hooks` در `install-hooks`، حذف مسیر ثابت venv از `pre-commit` و ادغام اتمیک (`.bak` + `os.replace`) برای فایل‌های JSON.
  - خواندن امن فایل‌های UTF-8 BOM / UTF-16 پاورشل و محافظت `.agsync/**`، `.agsync-agent` و `.agents/hooks.json` در برابر ویرایش ایجنت.
- **عیب‌یاب (`agsync doctor`) و داشبورد زنده**: دستور و دکمه عیب‌یابی محیط/شبکه/هوک + داشبورد وب در `GET /dashboard`.

---

## شروع سریع

### روش اول: کنترل‌پنل گرافیکی (پیشنهادی در ویندوز)
1. فایل `Start_Sync_Panel.bat` را باز کنید (یا `python scripts/control_panel.py` را اجرا کنید).
2. **سیستم میزبان (Host):** نقش را روی `HOST (Hub)` بگذارید، `Agent ID` را وارد کنید، **Generate Token** را بزنید (یا بگذارید خودکار ساخته شود)، در شبکه محلی **Open Firewall (Admin)** را بزنید و **Start Hub Server** را کلیک کنید.
3. **سیستم همکار (Peer):** نقش را روی `PEER (Client)` بگذارید، IP میزبان را بدهید (یا **Discover LAN Hub** را بزنید)، توکن را وارد کرده و **Test Connection (TCP + /health)** را بزنید.
4. مسیر پروژه را انتخاب کنید و روی هر کلاینت MCP خود (با دکمه **Connect Antigravity MCP** یا قرار دادن کانفیگ استاندارد MCP در کلاینت‌های دیگر) و **Install Repo Hooks & Templates** کلیک کنید.

### روش دوم: خط فرمان (هر سیستم‌عامل و هر ایجنتی)
1. **اجرای هاب** (مسیر پیش‌فرض دیتابیس `~/.agsync/state.db` است):
   ```powershell
   $env:AGSYNC_TOKEN = "your-shared-secret"
   python -m agsync.cli serve --host 0.0.0.0 --port 8765
   ```
   *(برای تست فقط روی همین سیستم با `127.0.0.1`، توکن اجباری نیست.)*
2. **اتصال هر ایجنت سازگار با MCP** (مطابق `templates/mcp_config.http.json` یا `templates/mcp_config.stdio.json`):
   ```json
   {
     "mcpServers": {
       "agsync": {
         "url": "http://127.0.0.1:8765/mcp?agent=agent-a",
         "headers": {
           "X-Agent-Id": "agent-a",
           "Authorization": "Bearer your-shared-secret"
         }
       }
     }
   }
   ```
3. **نصب هوک‌ها و اجرای عیب‌یاب روی مخزن پروژه:**
   ```powershell
   python -m agsync.cli install-hooks --repo /path/to/project --server http://127.0.0.1:8765 --agent agent-a --with-antigravity-hook --with-templates
   python -m agsync.cli doctor --repo /path/to/project
   ```

---

## تست و بررسی سلامت

```powershell
python -m unittest discover -s tests -v
python scripts/smoke_check.py --spawn-local
python -m agsync.cli doctor --repo .
```

---

## فهرست مستندات

| موضوع | فارسی | English |
|---|---|---|
| **معرفی و شروع سریع** | [README.fa.md](README.fa.md) / [docs/OVERVIEW.fa.md](docs/OVERVIEW.fa.md) | [README.md](README.md) |
| **راهنمای گام‌به‌گام نصب** | [docs/SETUP_GUIDE.fa.md](docs/SETUP_GUIDE.fa.md) | [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) |
| **معماری و مقایسه روش‌ها** | [docs/ARCHITECTURE.fa.md](docs/ARCHITECTURE.fa.md) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| **نقشه راه (فاز ۱ تا ۳)** | [docs/ROADMAP.fa.md](docs/ROADMAP.fa.md) | — |
| **چک‌لیست ارزیابی فنی** | — | [docs/RUBRIC.md](docs/RUBRIC.md) |
