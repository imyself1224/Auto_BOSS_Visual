@echo off
REM auto_boss: 爸妈版无人值守投递（上海 / 无简历 / 月薪 5000+）。
REM 使用独立的 Edge profile 和 data/parents 数据目录，和本人版互不影响。
REM 第一次先手动跑 `python run.py --config config.parents.toml login` 扫码。
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
python run.py --config config.parents.toml greet --auto
