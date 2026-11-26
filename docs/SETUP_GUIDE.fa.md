# راهنمای گام‌به‌گام راه‌اندازی: هماهنگی چند سیستم و چند ایجنت (`agsync`)

[فارسی](SETUP_GUIDE.fa.md) | [English](SETUP_GUIDE.md)

این راهنما راه‌اندازی کامل `agsync` را از صفر برای دو یا چند همکار (**Machine A** = میزبان هاب، **Machine B / سایر سیستم‌ها** = کلاینت‌ها) و **هر ایجنت سازگار با MCP یا CLI** (شامل Antigravity، Claude Code، Cursor، Windsurf، Codex، Gemini CLI و غیره) توضیح می‌دهد.

> **نمادهای استفاده‌شده در این راهنما:**
> - `<MACHINE_A_IP>`: آدرس IPv4 شبکه محلی سیستم میزبان (مثلاً `192.168.1.10`).
> - `<AGSYNC_DIR>`: مسیر پوشه `antigravity_multi_agent_sync` روی سیستم.
> - `<PROJECT_DIR>`: مسیر کلونِ مخزن پروژه مشترک روی هر سیستم.
> - `<SHARED_TOKEN>`: توکن مشترک احراز هویت Bearer (هنگام اجرای هاب روی `0.0.0.0` الزامی است).

---

## گام ۱: بررسی پیش‌نیازها روی هر سیستم

```powershell
python --version   # باید Python 3.12 یا بالاتر باشد (بدون نیاز به هیچ کتابخانه خارجی)
git --version      # پیشنهادی: Git 2.38+ (توسط agsync doctor بررسی می‌شود)
```

---

## گام ۲: پیدا کردن IP شبکه محلی Machine A

روی **Machine A**:
- **ویندوز (PowerShell):**
  ```powershell
  Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL|Tun|TAP' -and $_.IPAddress -notlike '169.254.*' } | Select-Object InterfaceAlias, IPAddress
  ```
- **لینوکس / مک:**
  ```bash
  ip -4 addr show   # یا در مک: ipconfig getifaddr en0
  ```

---

## گام ۳: باز کردن فایروال ویندوز روی Machine A (در صورت ویندوزی بودن میزبان)

مطمئن شوید پروفایل شبکه وای‌فای/اترنت روی **Private** تنظیم شده است، سپس در **PowerShell با دسترسی Administrator** اجرا کنید (یا از دکمه **Open Firewall (Admin)** در پنل گرافیکی استفاده کنید):

```powershell
New-NetFirewallRule -DisplayName "agsync LAN Coordination Hub" -Direction Inbound -Protocol TCP -LocalPort 8765 -Profile Private -Action Allow
New-NetFirewallRule -DisplayName "agsync LAN Discovery" -Direction Inbound -Protocol UDP -LocalPort 8766 -Profile Private -Action Allow
```

---

## گام ۴: ایزوله‌سازی کد با Git (ریموت آنلاین یا ۱۰۰٪ آفلاین روی LAN)

به **هر ایجنت/سیستم کلون (یا `git worktree`) و برنچ مستقل بدهید**. هرگز پوشه `.git` یا فایل‌های کاری را از طریق SMB، OneDrive یا Syncthing بین دو سیستم به اشتراک نگذارید.

### حالت A: مخزن مشترک GitHub / GitLab / Gitea
- **سیستم A:** `cd <PROJECT_DIR> && git checkout -b agent-a/work`
- **سیستم B:** `git clone <YOUR_REPO_URL> <PROJECT_DIR> && cd <PROJECT_DIR> && git checkout -b agent-b/work`

### حالت B: Git کاملاً آفلاین روی LAN (بدون نیاز به اینترنت)
روی **Machine A**:
```powershell
git init --bare -b main C:\git-repos\my_app.git
git clone C:\git-repos\my_app.git C:\projects\my_app
cd C:\projects\my_app
git commit --allow-empty -m "chore: initial repo"
git push -u origin HEAD:main
git checkout -b agent-a/work
```
روی **Machine B** (از طریق OpenSSH روی سیستم A):
```powershell
git clone -b main ssh://<USER_A>@<MACHINE_A_IP>/C:/git-repos/my_app.git C:\projects\my_app
cd C:\projects\my_app
git checkout -b agent-b/work
```

---

## گام ۵: اجرای سرور هاب `agsync` روی Machine A

> **قانون امنیتی فاز ۱:** بایند شدن روی `0.0.0.0` بدون توکن مجاز نیست (باید `AGSYNC_TOKEN`، `--token` یا `--auto-token` بدهید؛ مگر برای تست کنترل‌شده با `--allow-insecure-lan`). مسیر پیش‌فرض دیتابیس SQLite WAL در `~/.agsync/state.db` است.

```powershell
cd <AGSYNC_DIR>
$env:AGSYNC_TOKEN = "<SHARED_TOKEN>"
python -m agsync.cli serve --host 0.0.0.0 --port 8765 --discover
```

*(برای تست فقط روی همین سیستم با `--host 127.0.0.1`، توکن اجباری نیست.)*

### تست اتصال از هر دو سیستم
- روی **Machine A:** `Invoke-RestMethod http://127.0.0.1:8765/health`
- روی **Machine B:** `Invoke-RestMethod http://<MACHINE_A_IP>:8765/health`
*(کلاینت و پنل `agsync` به‌صورت خودکار برای آدرس‌های `127.0.0.1` و شبکه خصوصی LAN پروکسی سیستم مثل v2rayN/Clash/Happ را دور می‌زنند.)*

---

## گام ۶: اتصال ایجنت‌ها از طریق MCP یا CLI

`agsync` ۱۳ ابزار `sync_*` را هم از طریق **Streamable HTTP (`POST /mcp`)** و هم از طریق **Stdio JSON-RPC (`python -m agsync.mcp_bridge`)** ارائه می‌دهد.

### حالت ۱: Antigravity (`~/.gemini/config/mcp_config.json`)
```powershell
# روی Machine A:
python -m agsync.cli setup-mcp --server http://127.0.0.1:8765 --agent machine-a --token "<SHARED_TOKEN>"

# روی Machine B:
python -m agsync.cli setup-mcp --server http://<MACHINE_A_IP>:8765 --agent machine-b --token "<SHARED_TOKEN>"
```
*(این دستور فایل را با `.bak` و ادغام اتمیک به‌روز می‌کند و سایر سرورهای MCP شما را دست‌نخورده نگه می‌دارد.)*

### حالت ۲: Claude Code / Cursor / Windsurf و سایر کلاینت‌های MCP
کانفیگ زیر را در فایل MCP کلاینت خودتان (مثلاً `.mcp.json` برای Claude Code یا `~/.cursor/mcp.json` برای Cursor) قرار دهید:

**با Streamable HTTP:**
```json
{
  "mcpServers": {
    "agsync": {
      "url": "http://<MACHINE_A_IP>:8765/mcp?agent=claude-b",
      "headers": {
        "X-Agent-Id": "claude-b",
        "Authorization": "Bearer <SHARED_TOKEN>"
      }
    }
  }
}
```

**یا با Stdio Bridge:**
```json
{
  "mcpServers": {
    "agsync": {
      "command": "python",
      "args": [
        "-m",
        "agsync.mcp_bridge",
        "--server",
        "http://<MACHINE_A_IP>:8765",
        "--agent",
        "claude-b"
      ],
      "env": {
        "PYTHONPATH": "<AGSYNC_DIR>",
        "AGSYNC_TOKEN": "<SHARED_TOKEN>"
      }
    }
  }
}
```

### حالت ۳: ایجنت‌های متنی / CLI (مثل Codex یا اسکریپت‌ها)
هر ایجنتی که دسترسی به ترمینال دارد می‌تواند مستقیماً از `python -m agsync.cli` استفاده کند (`register`، `status`، `inbox --unread --ack`، `task`، `lock`، `unlock`، `check`، `send`، `context`).

---

## گام ۷: نصب قوانین، اسکیل‌ها و هوک‌های قفل روی `<PROJECT_DIR>`

این دستور هم در کلون عادی، هم در `git worktree` (با `--git-common-dir`) و هم با `core.hooksPath` سفارشی درست کار می‌کند:

```powershell
# روی Machine A:
python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://127.0.0.1:8765" --agent machine-a --with-antigravity-hook --with-templates

# روی Machine B:
python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://<MACHINE_A_IP>:8765" --agent machine-b --with-antigravity-hook --with-templates
```

نتیجه این دستور:
- هوک `pre-commit` گیت بدون ثابت‌کردن مسیر venv موقت نصب می‌شود.
- کلید `agsync-lease-guard` به‌صورت اتمیک با `.agents/hooks.json` ادغام می‌شود (بدون پاک کردن هوک‌های قبلی شما + بکاپ `.bak`).
- فایل `AGENTS.md` و اسکیل‌های `sync-*` کپی می‌شوند و `.agsync/` و `.agsync-agent` به `<git-common-dir>/info/exclude` اضافه می‌گردند.
- `hook_guard` به‌طور خودکار جلوی ویرایش `.agsync/**`، `.agsync-agent` و `.agents/hooks.json` را توسط ایجنت می‌گیرد.

---

## گام ۸: جریان کار روزانه

1. **ثبت‌نام و وضعیت:** هر ایجنت در شروع با `sync_register`، `sync_status` و `sync_poll_messages` وضعیت تیم و پیام‌ها را می‌خواند.
2. **تقسیم تسک:** تسک‌ها با `sync_create_task` تعریف و با `sync_claim_task` به‌صورت اتمیک تصاحب می‌شوند.
3. **اشتراک قراردادها:** پورت‌ها و ساختار API با `sync_publish_context` منتشر می‌شوند.
4. **قفل قبل از ویرایش:** پیش از هر ویرایش، `sync_acquire_lease` صدا زده می‌شود تا تداخل رخ ندهد.
5. **کامیت و تحویل کار:** پس از تست و کامیت روی برنچ خود، ایجنت وضعیت تسک را `done`/`review` می‌گذارد، قفل‌ها را آزاد می‌کند و پیام `handoff` می‌فرستد.

---

## گام ۹: بررسی سلامت (`agsync doctor`) و تست‌ها

```powershell
cd <AGSYNC_DIR>
python -m agsync.cli doctor --repo "<PROJECT_DIR>"
python -m unittest discover -s tests -v
python scripts/smoke_check.py --spawn-local
```

---

## رفع خطاهای رایج

- **سرور روی `0.0.0.0` بالا نمی‌آید:** توکن (`AGSYNC_TOKEN` / `--token` / `--auto-token`) را تنظیم کنید.
- **خطای `TCP FAILED` در Machine B:** خروجی `agsync doctor` را روی Machine A ببینید؛ اگر پروفایل ویندوز روی `Public` است به `Private` تغییر دهید و فایروال پورت `8765` را چک کنید.
- **خطای `401` / `403` / `415`:** یکسان‌بودن Bearer Token، ارسال `Content-Type: application/json` در درخواست‌های تغییردهنده و معتبر بودن هدر `Host` را چک کنید.
- **خطای `400` روی `GET /messages?auto_ack=1`:** متد `GET /messages` فقط خواندنی است؛ تایید خواندن از طریق `POST /messages/ack` انجام می‌شود.
- **نقل‌قول JSON در PowerShell 5.1:** برای `agsync context set` کوتیشن‌های داخلی را Escape کنید (`'{\"port\":8765}'`) یا از `--raw` استفاده کنید.
