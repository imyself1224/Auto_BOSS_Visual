"""`run.py greet --auto` 的重启逻辑测试。浏览器和 agent 全部打桩。

    python tests/test_auto.py
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name, got, want):
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 得到 {got!r}，期望 {want!r}")


def check_true(name, cond):
    check(name, bool(cond), True)


class FakeStore:
    def close(self):
        pass


def drive(outcomes: list[str]) -> tuple[int, int]:
    """按 outcomes 依次结束每一轮（"raise" = 抛异常），返回 (退出码, 跑了几轮)。"""
    runs = []

    class FakeAgent:
        def __init__(self, *a, **kw):
            self.dry_run = kw.get("dry_run")
            self.stop_kind = ""
            self.stats = {}

        def filter_summary(self):
            return ""

        def run(self):
            outcome = outcomes[len(runs)]
            runs.append(outcome)
            if outcome == "raise":
                raise RuntimeError("CDP 连接中断")
            self.stop_kind = outcome

    run.build = lambda cfg, run_id: (None, FakeStore(), None, None)
    run.GreetAgent = FakeAgent
    run.time.sleep = lambda s: None

    # ★ 测试绝不能真的给大号发 QQ ★
    # 真实配置里 [qq].enabled=true、.env 里也有 OWNER_QQ，不挡住的话每跑一次测试
    # 大号就会收到一串「任务结束」。
    class QuietNotifier:
        enabled = False

        def send(self, text):
            return False

    run.Notifier = lambda cfg: QuietNotifier()
    args = SimpleNamespace(limit=None)
    with redirect_stdout(io.StringIO()):
        code = run._greet_auto(run.cfgmod.load(), args, max_restarts=3, wait_sec=0)
    return code, len(runs)


def test_restarts_until_quota():
    code, n = drive(["error", "raise", "quota"])
    check("出错两次后额度用完：跑了 3 轮", n, 3)
    check("额度用完算正常结束", code, 0)


def test_captcha_not_restarted():
    code, n = drive(["captcha", "quota"])
    check("验证码不重启", n, 1)
    check("验证码返回非 0", code, 1)


def test_dry_pool_not_restarted():
    code, n = drive(["dry", "quota"])
    check("连续没有可投岗位：收工不重启", n, 1)
    check("没岗位可投不算出错", code, 0)


def test_gives_up_after_max_restarts():
    code, n = drive(["error"] * 10)
    check("最多重启 3 次 = 跑 4 轮", n, 4)
    check("放弃时返回非 0", code, 1)


def test_start_notice_lists_keywords():
    """任务开始前推给 QQ 的文字里要能看出这次搜哪些词。"""
    import run as run_mod
    from autoboss import config as cfgmod
    cfg = cfgmod.load()
    cfg._raw["search"] = {"queries": ["大模型算法", "Agent"], "query": "算法工程师"}
    cfg._raw["source"] = {"mode": "both", "recommend_last": True, "expect_tabs": []}

    text = run_mod._start_notice(cfg, [], dry_run=False, limit=5)
    check_true("标题是实发", "投递任务开始" in text and "实发" in text)
    check_true("带上本次条数", "最多 5 条" in text)
    check_true("列出关键词", "大模型算法" in text and "Agent" in text)
    check_true("带上关键词个数", "2 个" in text)
    check_true("提到推荐流", "推荐流" in text)

    text = run_mod._start_notice(cfg, ["推荐算法"], dry_run=True, limit=None)
    check_true("标明来自 QQ 指令", "来自 QQ 指令" in text)
    check_true("只列指定的词", "推荐算法" in text and "Agent" not in text)
    check_true("指定关键词时不走推荐流", "推荐流" not in text)
    check_true("标明演练", "演练" in text)

    # 学历 / 日薪：QQ 指令带上时要出现在开始通知里
    text = run_mod._start_notice(cfg, ["大模型算法"], dry_run=False, limit=None,
                                 education="本科", min_wage=500)
    check_true("带上学历档位", "学历要求 本科" in text)
    check_true("带上 degree 参数", "degree=203" in text)
    check_true("带上日薪下限", "≥500 元/天" in text)

    # 没指定时显示 config.toml 里真正生效的值
    text = run_mod._start_notice(cfg, [], dry_run=False, limit=None)
    check_true("回落到 config 的学历", "学历要求 不限" in text)
    check_true("回落到 config 的日薪", "≥300 元/天" in text)


def test_degree_url_param():
    """学历要拼成 BOSS 搜索地址的 degree 参数（平台筛，不花模型调用）。"""
    from autoboss import config as cfgmod
    cfg = cfgmod.load()
    cfg._raw["search"] = {"query": "算法工程师", "city": "101010100",
                          "job_type": "1902"}
    url = cfg.search_url(1, "大模型算法", degree="203,204")
    check_true("URL 带 degree", "degree=203,204" in url)
    check_true("URL 仍带 jobType", "jobType=1902" in url)
    check_true("URL 仍带关键词", "%E5%A4%A7%E6%A8%A1%E5%9E%8B" in url)
    check_true("不设学历时没有 degree", "degree=" not in cfg.search_url(1, "大模型算法"))


def test_cli_education_and_wage_args():
    check("硕士", run._cli_education("硕士"), "硕士")
    check("不限 → 空串（要盖住 config）", run._cli_education("不限"), "")
    check("没传保持 None", run._cli_education(None), None)
    check("日薪", run._cli_min_wage(300), 300.0)
    check("0 = 不限", run._cli_min_wage(0), 0.0)
    check("没传保持 None", run._cli_min_wage(None), None)

    for bad in ("本科及以上", "火星"):
        try:
            run._cli_education(bad)
        except SystemExit:
            check(f"「{bad}」被拒绝", True, True)
        else:
            check(f"「{bad}」应当拒绝", False, True)

    try:
        run._cli_min_wage(99999)
    except SystemExit:
        check("离谱日薪被拒绝", True, True)
    else:
        check("离谱日薪应当拒绝", False, True)


def test_notify_start_sends_to_qq():
    class Rec:
        enabled = True

        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    import run as run_mod
    from autoboss import config as cfgmod
    cfg = cfgmod.load()
    cfg._raw["search"] = {"queries": ["大模型算法"], "query": ""}
    cfg._raw["source"] = {"mode": "search", "recommend_last": True}
    rec = Rec()
    with redirect_stdout(io.StringIO()):
        run_mod._notify_start(rec, cfg, [], dry_run=True, limit=None)
    check("开始前推 1 条", len(rec.sent), 1)
    check_true("推的是关键词清单", rec.sent and "大模型算法" in rec.sent[0])


if __name__ == "__main__":
    for fn in (test_restarts_until_quota, test_captcha_not_restarted, test_dry_pool_not_restarted,
               test_gives_up_after_max_restarts, test_start_notice_lists_keywords,
               test_degree_url_param, test_cli_education_and_wage_args,
               test_notify_start_sends_to_qq):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} 抛异常: {exc}")
            traceback.print_exc()
    total = PASSED + len(FAILED)
    print(f"通过 {PASSED}/{total}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(1 if FAILED else 0)
