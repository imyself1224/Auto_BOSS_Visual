"""配置隔离与无简历提示词的测试。

    python tests/test_config.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoboss import config as cfgmod  # noqa: E402
from autoboss import prompts  # noqa: E402
from autoboss.store import Store  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want):
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 得到 {got!r}，期望 {want!r}")


def check_true(name: str, cond: bool):
    check(name, bool(cond), True)


def test_default_profile():
    cfg = cfgmod.load()
    root = cfgmod.PROJECT_ROOT
    check("默认档位名", cfg.name, "self")
    check("默认数据目录是项目根目录", cfg.data_dir, root)
    check("默认 Edge profile", cfg.user_data_dir, root / "browser_profile")
    check("默认数据库", cfg.db_path, root / "autoboss.db")
    check("默认日志目录", cfg.logs_dir, root / "logs")
    check("默认截图目录", cfg.shots_dir, root / "shots")
    # 浏览器锁是全局的：两个 Edge profile 也不能同时操作
    check("锁始终在项目根目录", cfg.lock_path, root / ".browser.lock")


def test_parents_profile():
    cfg = cfgmod.load(cfgmod.PROJECT_ROOT / "config.parents.toml")
    root = cfgmod.PROJECT_ROOT
    check("爸妈档位名", cfg.name, "parents")
    check("爸妈数据目录", cfg.data_dir, root / "data" / "parents")
    check("爸妈 Edge profile", cfg.user_data_dir, root / "browser_profile_parents")
    check("爸妈数据库独立", cfg.db_path, root / "data" / "parents" / "autoboss.db")
    check("爸妈日志独立", cfg.logs_dir, root / "data" / "parents" / "logs")
    check("爸妈截图独立", cfg.shots_dir, root / "data" / "parents" / "shots")
    check("爸妈独立调试端口", cfg.get("browser.cdp_port"), 9223)
    check("无简历模式", cfg.get("resume.required"), False)
    check("不做 fit", cfg.get("job_filter.require_fit"), False)
    check("排除实习", cfg.get("job_filter.exclude_internship"), True)
    check("月薪下限", float(cfg.get("job_filter.min_monthly_wage")), 5000.0)
    check("日薪下限关掉", float(cfg.get("job_filter.min_daily_wage")), 0.0)
    check("上海城市代码", str(cfg.get("search.city")), "101020100")
    check("城市必须含上海", cfg.get("job_filter.require_city"), "上海")


def test_relative_path():
    cfg = cfgmod.load()
    check("相对路径按项目根目录解析",
          cfg.project_path("resume/kb.md"), cfgmod.PROJECT_ROOT / "resume" / "kb.md")
    absolute = cfgmod.PROJECT_ROOT / "resume" / "kb.md"
    check("绝对路径不变", cfg.project_path(absolute), absolute)


def test_no_resume_prompt():
    no_fit = prompts.list_user("", require_fit=False)
    with_fit = prompts.list_user("画像内容", require_fit=True)
    check_true("无简历提示词带说明", "无简历模式" in no_fit)
    check_true("无简历提示词没有 fit 字段", '"fit"' not in no_fit)
    check_true("无简历提示词没有匹配档位", "最匹配" not in no_fit)
    check_true("有简历提示词带画像", "画像内容" in with_fit)
    check_true("有简历提示词带 fit 字段", '"fit"' in with_fit)
    check_true("有简历提示词带档位", "最匹配" in with_fit)
    check_true("无简历提示词更短", len(no_fit) < len(with_fit))


def test_store_creates_data_dir():
    """doctor 第一次跑爸妈版时，data/parents/ 还不存在。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "data" / "parents" / "autoboss.db"
        store = Store(path, "test")
        try:
            check_true("数据库父目录自动创建", path.is_file())
        finally:
            store.close()


if __name__ == "__main__":
    for fn in (test_default_profile, test_parents_profile,
               test_relative_path, test_no_resume_prompt,
               test_store_creates_data_dir):
        fn()
    total = PASSED + len(FAILED)
    print(f"通过 {PASSED}/{total}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(1 if FAILED else 0)
