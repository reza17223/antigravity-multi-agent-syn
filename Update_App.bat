@echo off
cd /d "%~dp0"
echo ========================================================
echo   agsync - Checking and Installing Updates
echo ========================================================
echo.
python scripts\updater.py
echo.
pause
