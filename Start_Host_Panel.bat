@echo off
cd /d "%~dp0"
python scripts\control_panel.py --mode host
if errorlevel 1 pause
