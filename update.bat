@echo off
chcp 65001 >nul
title agsync 1-Click Fast Updater
echo ======================================================================
echo                agsync 1-Click Fast Updater (به‌روزرسانی سریع)
echo ======================================================================
echo.
echo در حال بررسی و دریافت آخرین نسخه...
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo [خطا] پایتون روی سیستم یافت نشد. لطفاً مطمئن شوید Python نصب است.
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
    echo [موفق] برنامه‌ به آخرین نسخه با موفقیت به‌روزرسانی شد!
    echo ======================================================================
) else (
    echo.
    echo [توجه] در صورت بروز خطای شبکه، لطفاً پروکسی یا فیلترشکن خود را روشن کنید.
)
echo.
pause
