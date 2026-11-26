# نقشه راه نهایی و بازبینی‌شده `agsync` (جمع‌بندی ممیزی ChatGPT + Red-Team + Claude)

این نقشه راه حاصل ترکیب معماری اصلی، ساده‌سازی واقع‌بینانه (Red-Team) و ممیزی خط‌به‌خط سورس‌کد توسط Claude است تا خروجی نهایی **ساده برای کاربر عادی (ویندوزی)**، **ماژولار و آزاد برای کاربر حرفه‌ای (لینوکسی)**، **کم‌توکن در MCP** و **ضدباگ در ویندوز و چند IDE** باشد.

---

## اصول قطعی و اصلاح‌شده

1. **ساختار تخت و تمیز (بدون مهندسی اضافه):**
   - `agsync/db.py` (SQLite WAL + `PRAGMA user_version` migrations + `busy_timeout=10000` بدون `_write_tx` روی مسیرهای خواندنی)
   - `agsync/core/` (`locks.py`, `events.py`, `git_ops.py`)
   - `agsync/commands/` (دستورات کوچک و مجزا برای CLI)
   - `agsync/adapters/` (`ide/` و `network/`)
   - استفاده از **External Shell Hooks** در فایل کانفیگ برای کاربر حرفه‌ای به جای `plugins.py` پیچیده.
2. **واقع‌بینی در قفل‌گذاری و ایزوله‌سازی:**
   - **قفل فایل/پوشه + Glob Lock + هش محتوا (`base_content_hash`)** (حذف `Line-Range` و `AST` شکننده).
   - پشتیبانی **Opt-in (اختیاری)** از `git worktree` با مسیر کوتاه (`../repo-wt-1`)، هویت صریح `.agsync-agent` و استفاده از **Directory Junction (`mklink /J`)** در ویندوز (بدون نیاز به دسترسی Admin).
3. **MCP فوق‌فشرده (حداکثر ۳–۴ ابزار):**
   - جلوگیری از تورم توکن: ابزارهای مدیریتی (`doctor`, `worktree`, `invite/join`, `merge-check`) فقط در CLI و GUI بمانند.
   - پاسخ‌های کوتاه در MCP و استفاده از `sync_wait` با تایماوت کوتاه (۱۵ ثانیه) به جای Polling.

---

## فاز ۱: سخت‌سازی امنیتی، رفع باگ‌های واقعی و پایه پایدار (اولویت فوری)

### ۱.۱. بسته سخت‌سازی سرور، دیتابیس و ویندوز (یافته‌های قطعی ممیزی کد)
- **امنیت HTTP و CSRF (`server.py`):**
  - اجبار `Content-Type: application/json` روی درخواست‌های تغییردهنده و چک کردن `Origin`/`Host`.
  - حذف تغییر وضعیت در `GET` (تبدیل `auto_ack=1` در `GET /messages` به `POST` یا عدم تغییر وضعیت در `GET`).
  - جلوگیری از Bind شدن غیر-loopback (`0.0.0.0`) بدون توکن (یا تولید توکن خودکار/صریح در پنل).
  - تبدیل توکن‌ها به `bytes` پیش از `hmac.compare_digest` (رفع `TypeError` غیر-ASCII)، بستن یا Drain کردن بدنه خوانده‌نشده در خطاهای ۴۰۰/۴۰۱، و تنظیم `timeout=30s` روی Handler.
  - گذاشتن `except Exception` سراسری در Handlerها تا استثناها به جای قطع اتصال و **Fail-Open شدن `hook_guard`** پاسخ JSON با کد `500` برگردانند.
- **اصلاح تراکنش و هم‌زمانی SQLite (`store.py` / `db.py`):**
  - انتقال مسیرهای خواندنی (`list_leases`, `check_paths`, `snapshot`, `export_state`, `/health`) به تراکنش خواندنی با فیلتر `expires_at > now` در SQL (و Purge دوره‌ای).
  - مدیریت امن خطای `COMMIT` داخل `_write_tx` تا تراکنش باز نماند.
  - اصلاح `import_state`: استفاده از `MAX` برای `counters` (`fencing_seq` و `task_seq`) تا توکن Fencing به عقب برنگردد، بررسی برخورد شناسه در `create_task`، و محدود کردن `/state/*` به توکن ادمین + بکاپ خودکار پیش از `replace=True`.
  - اضافه کردن `PRAGMA user_version` برای Migrationها و انتقال مسیر پیش‌فرض دیتابیس به پوشه داده کاربر (`~/.agsync/`).
- **پایداری ویندوز و جلوگیری از زامبی (`server.py`, `control_panel.py`, `client.py`):**
  - استفاده از `SO_EXCLUSIVEADDRUSE` + PID/lock-file کنار DB برای جلوگیری از اجرای دو سرور هم‌زمان روی یک پورت در ویندوز.
  - محافظت `log_message` با `try/except OSError` و نوشتن لاگ در فایل، استفاده از Windows Job Object برای بستن تمیز سرور، و سقف بافر لاگ در Tkinter + رفع مسدود شدن UI Thread در `attach_repo`.
  - دور زدن سیستم پروکسی ویندوز (`urllib.request.ProxyHandler({})`) در `client.py` و `control_panel.py` برای آدرس‌های `127.0.0.1` و شبکه خصوصی تا v2rayN/Clash/Happ ترافیک LAN را ندزدند.
  - اعتبارسنجی عددی `1..65535` روی پورت پیش از ارسال به PowerShell Admin در پنل و پشتیبانی از پروفایل فایروال مناسب.
  - حذف توکن از آرگومان‌های خط فرمان (`argv`) و خواندن از Env یا فایل با دسترسی محدود.
- **رفع باگ‌های `hook_guard.py` و `cli.py` (Common-Dir & Atomic Write):**
  - خواندن تنظیمات با `utf-8-sig` و `try/except` برای مقابله با BOM و UTF-16 PowerShell.
  - در `hook_guard.py`، استفاده از `os.path.abspath`/`normpath` به جای `.resolve()` تا مسیرها از Junction بیرون نپرند؛ محافظت از `.agsync/**`، `.agsync-agent` و `.agents/hooks.json` در برابر ویرایش توسط ایجنت؛ و پوشاندن کل `main()` با `try/except` برای خروجی JSON/Exit-Code معتبر در همه شرایط.
  - در `install-hooks`: استفاده از `git rev-parse --git-common-dir` و `--git-path hooks` تا در `git worktree` و `core.hooksPath` درست کار کند؛ عدم ثابت کردن مسیر venv در `pre-commit`؛ و **ادغام اتمیک (`os.replace` + `.bak` + retry)** در `setup-mcp` و `install-hooks` به جای بازنویسی کورکورانه.
  - بررسی و همگام‌سازی کلید `serverUrl` در مقابل `url` برای تنظیمات MCP در Antigravity.

### ۱.۲. ماژولار کردن فاز ۱ + `agsync doctor` + فشرده‌سازی MCP + تست ویندوز
- شکستن فایل‌های بزرگ به ساختار تخت (`db.py`, `core/`, `commands/`, `adapters/`) با حفظ کامل سازگاری تست‌ها.
- ساخت **`agsync doctor`** شامل بررسی‌های: پورت زامبی، BOM/UTF-16، پروفایل شبکه ویندوز، پروکسی سیستم، `serverUrl` vs `url`، مسیر common-dir در worktree، و canary زنده بودن هوک.
- فشرده‌سازی و تثبیت ۳–۴ ابزار اصلی MCP (`sync_status`, `sync_claim`, `sync_message`, `sync_context` / آماده‌سازی برای `sync_wait`).
- راه‌اندازی **CI حداقلی ویندوز (+ لینوکس/مک)** در `.github/workflows/ci.yml` برای کشف سریع باگ‌های BOM، قفل و مسیر.

---

## فاز ۲: چند-ایجنت واقعی، اتصال ریموت امن و مانیتورینگ زنده

- **پراکسی یکپارچه هوک (`agsync hook-proxy --host <ide>`) و آداپتورهای IDE:**
  - پشتیبانی واقعی از تفاوت‌های ورودی/خروجی و خطایابی بین **Antigravity** (JSON stdout، `ask`/`deny`)، **Claude Code** (snake_case `tool_name`/`tool_input`، `exit 2` + `stderr`)، و **Cursor** (`preToolUse`، پیشگیری از اجرای تکراری وقتی Cursor هوک `.claude/` را می‌خواند).
  - حالت `--record` برای ذخیره پیلودهای واقعی هر IDE در `.agsync/hook-samples/`.
  - ذخیره هوک قبلی کاربر و اجرای زنجیره‌ای ایمن با timeout و kill درخت پروسه روی ویندوز.
- **پشتیبانی Opt-in از `git worktree`:**
  - `agsync worktree create/list/remove` با خواندن `git worktree list --porcelain`، مسیر کوتاه `../repo-wt-1`، اتصال `node_modules`/`.venv` با Junction (`mklink /J`)، هویت اجباری از `.agsync-agent` (بدون فالبک خطرناک `gethostname()` روی چند ایجنت هم‌زمان).
- **اتصال ریموت و Onboarding یک‌کلیکه:**
  - آداپتور **Cloudflare Tunnel (`--protocol http2`)** + امکان تنظیم `base_url` دلخواه برای کاربر حرفه‌ای.
  - **`Join Link` امن (`agsync invite` / `agsync join`):** جدول `invites` در SQLite، مصرف اتمیک `nonce` امضاشده با HMAC و TTL کوتاه (`UPDATE ... WHERE used=0`)، و صدور **Bearer Token مستقل و گره‌خورده به `agent_id` هر دستگاه**.
- **رویدادهای لحظه‌ای و داشبورد پیشرفته:**
  - جدول `events` با `boot_id`، Ring Buffer (سقف ۱۰۰۰ رکورد) و پاک‌سازی دوره‌ای.
  - ابزار `sync_wait` در MCP با تایماوت کوتاه (حداکثر ۱۵ ثانیه) و `Condition` جدا از قفل SQLite.
  - داشبورد `/dashboard` با **Long-Polling (`since_id`)** + Live Activity Feed + نوتیفیکیشن مرورگر (Browser Notification API).

---

## فاز ۳: دستیار سبک Git، آزادی کاربر حرفه‌ای و بسته‌بندی

- **دستیار سبک Git (`agsync merge-check`):**
  - بررسی پیش‌دستانه تعارض با `git merge-tree --write-tree` (با بررسی نسخه Git >= 2.38 در `doctor`) بدون تغییر Working Tree و بدون پیچیدگی `integration/` worktree اجباری.
- **آزادی عمل لینوکسی (`External Shell Hooks`):**
  - اجرای دستورات و اسکریپت‌های دلخواه کاربر در `agsync.config.json` روی رویدادهایی مثل `on_lock`، `on_task_done` و `pre_merge` (برای اجرای تست، لینتر یا ارسال پیام به سیستم‌های خارجی).
- **مدیریت صریح حالت Offline:**
  - Lease/TTL کوتاه با Heartbeat؛ هنگام قطع ارتباط با هاب، کلاینت وارد حالت Read-only / عدم صدور قفل جدید می‌شود.
- **بسته‌بندی نهایی GitHub:**
  - تکمیل `pyproject.toml` برای `pip install` / `uvx` + اسکریپت ساخت `.exe` ویندوز + مستندات دوزبانه کامل (`README.md`, `OVERVIEW.fa.md`, `SETUP_GUIDE.md`).
