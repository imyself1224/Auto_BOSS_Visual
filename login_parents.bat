@echo off
REM 首次使用 / 登录失效时：用爸妈版的独立 Edge profile 扫码登录。
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
python run.py --config config.parents.toml login
pause
