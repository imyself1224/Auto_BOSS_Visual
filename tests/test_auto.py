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
