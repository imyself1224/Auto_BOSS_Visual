@echo off
REM auto_boss: start the QQ bot (also launches SnowLuma + QQ if they are not running).
REM Windows runs this at logon via a shortcut in the Startup folder; double-click works too.
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
python run.py bot
pause
