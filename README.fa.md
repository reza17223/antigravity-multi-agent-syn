# agsync — همکاری هم‌زمان چند ایجنت هوش مصنوعی بدون تداخل و بازنویسی کد

[فارسی](README.fa.md) | [English](README.md)

[![CI](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml/badge.svg)](https://github.com/reza17223/antigravity-multi-agent-syn/actions/workflows/ci.yml)

اگر شما و همکارتان روی یک پروژه کار می‌کنید و هر دو از ایجنت‌های کدنویسی (مثل **Antigravity**، **Claude Code**، **Cursor**، **Windsurf**، **Codex** یا **Gemini CLI**) استفاده می‌کنید، احتمالاً با این مشکل روبه‌رو شده‌اید:

ایجنت‌ها به‌طور پیش‌فرض از کار همدیگر خبر ندارند؛ ممکن است هر دو هم‌زمان روی یک فایل بنویسند، کد همدیگر را پاک کنند یا بیلد پروژه را خراب کنند.

**`agsync` این مشکل را حل می‌کند.** مثل یک **واکی‌تاکی + تابلوی تسک + قفل هوشمند فایل** بین ایجنت‌هاست. بدون نیاز به نصب هیچ کتابخانه اضافه‌ای — فقط با پایتون ۳.۱۲ استاندارد.

---

## به زبان ساده چیکار می‌کند؟

- **قبل از ویرایش فایل قفل می‌گیرد:** وقتی ایجنت A روی `auth.py` کار می‌کند، فایل قفل می‌شود. اگر ایجنت B بخواهد همان فایل یا پوشه‌اش را تغییر دهد، `agsync` خودکار جلویش را می‌گیرد.
- **کد هر کس روی برنچ خودش امن می‌ماند:** هر نفر در پوشه و برنچ خودش کار می‌کند تا `node_modules`، `.venv` و `.git` با هم تداخل نداشته باشند.
- **تسک‌ها، پیام‌ها و قراردادها زنده ردوبدل می‌شوند:** ایجنت‌ها تسک‌ها را بین خودشان تقسیم می‌کنند (دو نفر هم‌زمان یک تسک را برنمی‌دارند)، به هم پیام می‌دهند و آدرس APIها را به اشتراک می‌گذارند.
- **هم در شبکه محلی (LAN) و هم از راه دور کار می‌کند:** چه کنار هم روی یک وای‌فای باشید، چه در دو شهر مختلف با **لینک دعوت یک‌بارمصرف (`Join Link`)** و **Cloudflare Tunnel**.
- **محدود به یک ابزار نیست:** از **Antigravity، Claude Code و Cursor** به‌صورت مستقیم پشتیبانی می‌کند و برای **Windsurf، Codex، Gemini CLI** و اسکریپت‌های سفارشی هم آماده است.

---

## راه‌اندازی سریع (در ۲ دقیقه)

### روش ۱: پنل گرافیکی ویندوز (ساده‌ترین راه)

1. روی فایل **`Start_Sync_Panel.bat`** دبل‌کلیک کنید.
2. **روی سیستم میزبان (نفر اول):**
   - نقش را روی **`HOST (Hub)`** بگذارید، یک نام (مثلاً `reza`) بنویسید و **`Generate Token`** را بزنید.
   - اگر همکارتان کنار شماست، **`Open Firewall (Admin)`** و بعد **`Start Hub Server`** را بزنید.
   - با **`Browse...`** پوشه پروژه‌تان را انتخاب کنید و دکمه‌های **`Connect Antigravity MCP`** و **`Install Repo Hooks & Templates`** را بزنید. *(اگر پوشه هنوز Git نداشته باشد، پنل خودکار پیشنهاد می‌دهد برایتان بسازد.)*
3. **روی سیستم همکار (نفر دوم):**
   - **`Start_Sync_Panel.bat`** را باز کنید و نقش را روی **`PEER (Client)`** بگذارید.
   - **اگر کنار هم هستید:** آی‌پی میزبان را وارد کنید (یا **`Discover LAN Hub`** را بزنید)، توکن را Paste کنید و **`Test Connection (TCP + /health)`** را بزنید.
   - **اگر از راه دور هستید:** نفر اول دکمه **`Cloudflare Tunnel`** و بعد **`Create Join Link...`** را می‌زند و لینک را می‌فرستد؛ نفر دوم فقط دکمه **`Join Remote Hub...`** را می‌زند و لینک را Paste می‌کند.
   - پوشه پروژه را انتخاب و MCP + هوک‌ها را نصب کنید (یا دکمه **`📥 Clone Project from Host`** را بزنید تا پروژه خودکار دانلود شود).

### نحوه بروزرسانی برنامه به نسخه‌های جدید (بدون نیاز به دانلود مجدد زیپ):
1. **از داخل پنل:** روی دکمه **`🚀 Check for Updates`** در بالای پنل بزنید تا برنامه با ۱ کلیک بروزرسانی شود.
2. **فایل آماده ویندوز:** روی فایل **`Update_App.bat`** دابل‌کلیک کنید.
3. **با خط فرمان:** `python scripts/updater.py` را اجرا کنید.

### روش ۲: خط فرمان (مک / لینوکس / ویندوز)

1. **اجرای هاب** روی سیستم اول:
   ```powershell
   $env:AGSYNC_TOKEN = "my-secret-token"
   python -m agsync.cli serve --host 0.0.0.0 --port 8765
   ```
2. **اتصال به IDE / ایجنت دلخواه شما:**
   ```powershell
   # انتخاب IDE: antigravity یا claude یا cursor
   python -m agsync.cli setup-mcp --ide claude --server http://127.0.0.1:8765 --agent claude-a --token my-secret-token
   python -m agsync.cli install-hooks --repo C:\path\to\project --ide claude --with-templates
   ```
3. **اطمینان از سلامت تنظیمات:**
   ```powershell
   python -m agsync.cli doctor --repo C:\path\to\project
   ```

---

## کارهای جالب دیگری که می‌توانید انجام دهید

- **دیدن وضعیت زنده تیم:** مرورگر را باز کنید و بروید به `http://<HOST_IP>:8765/dashboard` (یا در پنل دکمه **Open Web Dashboard** را بزنید).
- **اجرای چند ایجنت هم‌زمان روی یک سیستم:** با `agsync worktree create --agent agent-2` یک پوشه کاری تمیز و جدا برای ایجنت دوم بسازید.
- **چک کردن تداخل برنچ‌ها قبل از Merge:** دستور `agsync merge-check --base main --target HEAD`.
- **اجرای تست‌های پروژه:** `python -m unittest discover -s tests -v`.

---

## راهنماهای بیشتر

- **نسخه انگلیسی README:** [README.md](README.md)
- **راهنمای گام‌به‌گام نصب:** [docs/SETUP_GUIDE.fa.md](docs/SETUP_GUIDE.fa.md) | [English](docs/SETUP_GUIDE.md)
- **معماری و پشت صحنه:** [docs/ARCHITECTURE.fa.md](docs/ARCHITECTURE.fa.md) | [English](docs/ARCHITECTURE.md)
