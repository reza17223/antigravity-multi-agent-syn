@echo off
chcp 65001 >nul
cd /d "%~dp0"
title agsync - Fast 1-Click Updater
echo ======================================================================
echo             agsync Fast 1-Click Updater (به‌روزرسانی سریع)
echo ======================================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo [خطا] پایتون یافت نشد. لطفاً مطمئن شوید Python نصب است.
        pause
        exit /b 1
    ) else (
        set PY_CMD=py
    )
) else (
    set PY_CMD=python
)

%PY_CMD% scripts\updater.py
if %errorlevel% equ 0 (
    echo.
    echo ======================================================================
    echo [موفق] agsync به آخرین نسخه با موفقیت به‌روزرسانی شد!
    echo ======================================================================
) else (
    echo.
    echo [توجه] در صورت بروز خطای دانلود، لطفاً پروکسی یا فیلترشکن خود را فعال کنید.
)
echo.
pause
