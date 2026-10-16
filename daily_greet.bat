@echo off
REM auto_boss: unattended daily run. Double-click, or schedule it with Task Scheduler.
REM Sends until the platform quota is used up; restarts itself on errors.
REM Output is saved to logs\greet_*.log
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
python run.py greet --auto
