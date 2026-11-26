@echo off
cd /d "%~dp0"
python scripts\control_panel.py --mode peer
if errorlevel 1 pause
