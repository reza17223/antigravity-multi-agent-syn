# راهنمای سریع راه‌اندازی و معماری هماهنگی دو سیستم Antigravity (`agsync`)

این راهنما خلاصه عملی فارسی برای همگام‌سازی لحظه‌ای دو همکار است که به‌صورت هم‌زمان روی یک پروژه با **Antigravity** روی دو سیستم کنار هم (**Machine A** میزبان شبکه محلی و **Machine B** همکار) کار می‌کنند.

---

## ۱. آیا بین دو سیستم کنار هم به Git نیاز داریم؟

**بله — حتی در دو سیستم کنار هم نیز Git شدیداً لازم است، اما Git به‌تنهایی برای هماهنگی لحظه‌ای کافی نیست.**

- **چرا پوشه مشترک شبکه (SMB) یا همگام‌سازی خودکار فایل (Syncthing) خطرناک است؟**
  - Antigravity در هر سیستم به‌صورت محلی اجرا می‌شود (`language_server.exe` روی `localhost`) و هیچ قفل توزیع‌شده یا ویرایش هم‌زمان داخلی بین دو سیستم ندارد.
  - اگر دو Agent هم‌زمان روی یک پوشه مشترک بنویسند، تغییرات همدیگر را بی‌صدا بازنویسی می‌کنند، فایل‌های `.git/index.lock` خراب می‌شوند، و کش‌های ساخت (`node_modules`، `.venv`، `__pycache__`) با هم تداخل پیدا می‌کنند.
- **معماری پیشنهادی (هیبرید)**:
  1. **لایه کد (Git)**: هر سیستم کلون و شاخه مجزای خودش را دارد (`agent-a/work` و `agent-b/work`). حتی بدون اینترنت و GitHub هم می‌توانید یک مخزن bare روی Machine A راه‌اندازی کنید (حتماً با شاخه پیش‌فرض `main` بسازید تا کلون اولیه Machine B بدون خطا انجام شود: `git init --bare -b main` و در کلون دوم `git clone -b main ...`).
  2. **لایه کنترل لحظه‌ای (`agsync`)**: یک سرور سبک Python 3.12 (بدون نیاز به هیچ کتابخانه خارجی) روی Machine A اجرا می‌شود و ارتباط زنده، تقسیم تسک اتمیک، اشتراک قراردادهای API و قفل انحصاری فایل/پوشه (Lease با TTL) را از طریق MCP و Hook به هر دو Antigravity می‌دهد.

---

## ۲. مراحل راه‌اندازی گام‌به‌گام

### گام ۱: بررسی پیش‌نیازها روی هر دو سیستم
```powershell
python --version   # باید Python 3.12 یا بالاتر باشد
git --version
```

### گام ۲: پیدا کردن IP شبکه محلی Machine A و باز کردن فایروال
روی **Machine A**:
```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notlike '169.254.*' } | Select-Object InterfaceAlias, IPAddress
```
سپس در PowerShell با دسترسی Administrator پورت `8765` را باز کنید:
```powershell
New-NetFirewallRule -DisplayName "agsync LAN Coordination Hub" -Direction Inbound -Protocol TCP -LocalPort 8765 -Profile Private -Action Allow
```

### گام ۳: اجرای سرور `agsync` روی Machine A
```powershell
cd <AGSYNC_DIR>
python -m agsync.cli serve --host 0.0.0.0 --port 8765 --db .agsync/state.db
```
تست سلامت از Machine B (به‌جای `<MACHINE_A_IP>` آدرس IP سیستم A را بگذارید):
```powershell
Invoke-RestMethod http://<MACHINE_A_IP>:8765/health
```

### گام ۴: اتصال Antigravity از طریق MCP
فایل `~/.gemini/config/mcp_config.json` را در هر دو سیستم تنظیم کنید (در Machine A از `127.0.0.1` و `machine-a` و در Machine B از `<MACHINE_A_IP>` و `machine-b` استفاده کنید). Antigravity از **Streamable HTTP (`/mcp`)** پشتیبانی می‌کند (نه `/sse` قدیمی):

```json
{
  "mcpServers": {
    "agsync": {
      "url": "http://<MACHINE_A_IP>:8765/mcp?agent=machine-b",
      "headers": {
        "X-Agent-Id": "machine-b"
      }
    }
  }
}
```
*(همچنین می‌توانید از `templates/mcp_config.stdio.json` برای اتصال Stdio استفاده کنید.)*

### گام ۵: نصب قوانین، مهارت‌ها و قفل خودکار روی مخزن پروژه
1. فایل `templates/AGENTS.md` و پوشه `templates/.agents/skills/` را در ریشه مخزن پروژه کپی کنید تا دستورات `/sync-start`، `/sync-check` و `/sync-handoff` در Antigravity فعال شوند.
2. قفل پیش از کامیت Git و Hook محافظ `PreToolUse` را روی هر سیستم نصب کنید:
   ```powershell
   # روی Machine A:
   python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://127.0.0.1:8765" --agent machine-a --with-antigravity-hook

   # روی Machine B:
   python -m agsync.cli install-hooks --repo "<PROJECT_DIR>" --server "http://<MACHINE_A_IP>:8765" --agent machine-b --with-antigravity-hook
   ```

---

## ۳. تست و بررسی سلامت نصب (Smoke Check)

```powershell
cd <AGSYNC_DIR>
python -m unittest discover -s tests -v
python scripts/smoke_check.py --spawn-local
```
برای راهنمای کامل، سناریوی کاری روزانه، راه‌اندازی Git بدون اینترنت و رفع خطاها به `docs/SETUP_GUIDE.md` و `docs/ARCHITECTURE.md` مراجعه کنید.
