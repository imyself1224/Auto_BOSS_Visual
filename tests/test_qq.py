"""QQ 推送 / 遥控的测试。

OneBot 那头用本地起的假 websocket 服务代替，不需要真的 NapCat 和 QQ 号。
投递流程沿用 test_agent_flow 的打桩。

    python tests/test_qq.py
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from websockets.sync.server import serve  # noqa: E402

from autoboss import config as cfgmod  # noqa: E402
from autoboss.agent import StopRun  # noqa: E402
from autoboss.qq import Notifier  # noqa: E402
from autoboss.qqbot import QQBot  # noqa: E402
from autoboss.qqlaunch import QQLauncher  # noqa: E402
from test_agent_flow import GOOD_JOB, agent_env, silence  # noqa: E402

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


class FakeOneBot:
    """本地假 OneBot 正向 WS 服务：记下收到的动作和鉴权头。"""

    def __init__(self, retcode: int = 0):
        self.retcode = retcode
        self.received: list[dict] = []
        self.auth: list[str | None] = []
        self.server = serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self, ws):
        self.auth.append(ws.request.headers.get("Authorization"))
        for raw in ws:
            data = json.loads(raw)
            self.received.append(data)
            # 真实的 OneBot 会回一条带同样 echo 的动作回执
            if isinstance(data, dict) and data.get("echo"):
                ws.send(json.dumps({
                    "status": "ok" if self.retcode in (0, 1) else "failed",
                    "retcode": self.retcode,
                    "echo": data["echo"],
                }))

    def close(self):
        self.server.shutdown()


def make_cfg(port: int = 1, enabled: bool = True, owner: int = 10001):
    cfg = cfgmod.load()
    cfg._raw["qq"] = {"enabled": enabled, "ws_url": f"ws://127.0.0.1:{port}",
                      "owner_qq": owner, "access_token_env": "TEST_ONEBOT_TOKEN"}
    cfg._env["TEST_ONEBOT_TOKEN"] = "secret"
    return cfg


def wait_for(cond, timeout=3.0):
    import time
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.05)


# ===========================================================================
def test_notifier_sends_private_msg():
    fake = FakeOneBot()
    try:
        n = Notifier(make_cfg(fake.port))
        n.job_sent("Agent算法实习生", "百度", "400-500元/天", 3, 200)
        wait_for(lambda: fake.received)
        check("收到 1 个动作", len(fake.received), 1)
        act = fake.received[0] if fake.received else {}
        check("动作是私聊", act.get("action"), "send_private_msg")
        check("发给大号", act.get("params", {}).get("user_id"), 10001)
        msg = act.get("params", {}).get("message", "")
        check_true("消息里有岗位和公司", "Agent算法实习生" in msg and "百度" in msg)
        check_true("消息里有今日计数", "第 3 条" in msg)
        check("带上 access token", fake.auth[0] if fake.auth else None, "Bearer secret")
    finally:
        fake.close()


def test_notifier_disabled_or_down_is_harmless():
    check("没启用时不发", Notifier(make_cfg(enabled=False)).send("x"), False)
    check("没填大号时不发", Notifier(make_cfg(owner=0)).send("x"), False)
    # 端口 1 上没有服务：要返回 False，不能抛异常打断投递
    # outbox 指到临时目录，别把测试消息留在真实的 logs/ 里（下次连上会被补发）
    n = Notifier(make_cfg(port=1))
    n.outbox_dir = Path(tempfile.mkdtemp(prefix="qq_outbox_test_"))
    check("连不上时返回 False 不抛", n.send("x"), False)
    check("断网时消息存进 outbox", len(list(n.outbox_dir.glob("*.json"))), 1)


def test_notifier_flushes_outbox_when_back_online():
    fake = FakeOneBot()
    try:
        n = Notifier(make_cfg(fake.port))
        n.outbox_dir = Path(tempfile.mkdtemp(prefix="qq_outbox_test_"))
        # 先制造一条断网消息
        n._enqueue("断网时的通知")
        check("补发成功", n.flush(), True)
        wait_for(lambda: fake.received)
        check("补发了 1 条", len(fake.received), 1)
        check("补发内容正确",
              fake.received[0].get("params", {}).get("message") if fake.received else "",
              "断网时的通知")
        check("发完清空 outbox", len(list(n.outbox_dir.glob("*.json"))), 0)
    finally:
        fake.close()


def test_notifier_queues_when_onebot_reports_failure():
    # 断网时本地 WS 还通，但 OneBot 发 QQ 会失败并回 retcode != 0
    fake = FakeOneBot(retcode=1200)
    try:
        n = Notifier(make_cfg(fake.port))
        n.outbox_dir = Path(tempfile.mkdtemp(prefix="qq_outbox_test_"))
        check("回执失败 → send 返回 False", n.send("发不出去"), False)
        check("消息存进 outbox", len(list(n.outbox_dir.glob("*.json"))), 1)
    finally:
        fake.close()


def test_agent_notifies_each_sent_job():
    silence()

    class Rec:
        def __init__(self):
            self.jobs = []

        def job_sent(self, title, company, salary, total, cap):
            self.jobs.append((title, company, total))

    scene = {"page_state": "job_list", "jobs": [GOOD_JOB]}
    with agent_env([scene] * 6, daily_cap=1, dry_run=False) as (agent, *_):
        agent.notifier = Rec()
        try:
            agent._loop()
        except StopRun:
            pass
        check("每发一条推一条", len(agent.notifier.jobs), 1)
        if agent.notifier.jobs:
            check("推的是发出去的那条", agent.notifier.jobs[0][0], GOOD_JOB["title"])


def test_agent_stops_on_remote_flag():
    silence()
    scene = {"page_state": "job_list", "jobs": [GOOD_JOB]}
    with tempfile.TemporaryDirectory() as tmp, \
            agent_env([scene] * 6, dry_run=False) as (agent, page, *_):
        agent.stop_flag = Path(tmp) / ".stop_request"
        agent.stop_flag.touch()
        kind = None
        try:
            agent._loop()
        except StopRun as exc:
            kind = exc.kind
        check("看到停止标记就停机", kind, "stop")
        check("停机前一次都没点", len(page.clicks), 0)


# ---------------------------------------------------------------------------
class FakeBotConn:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def send_private(self, uid, text):
        self.sent.append((uid, text))


def make_bot(tmp: str) -> QQBot:
    cfg = make_cfg()
    # 指到临时库 / 临时文件，别读真实记录，也别在项目根目录留下关键词文件
    # （只改这个实例的类，不污染 Config 本身）
    cfg.__class__ = type("TmpConfig", (cfgmod.Config,), {
        "db_path": property(lambda self: Path(tmp) / "t.db"),
        "stop_flag": property(lambda self: Path(tmp) / ".stop_request"),
        "qq_keywords_path": property(lambda self: Path(tmp) / ".qq_keywords.txt")})
    bot = QQBot(cfg, Path(__file__).resolve().parent.parent / "run.py")

    # ★ 测试绝不能真的起投递进程 —— 那会用真实配置去 BOSS 直聘实发 ★
    def no_spawn(*a, **kw):
        raise AssertionError(f"测试里不允许启动子进程：{a[0] if a else ''}")
    bot.popen = no_spawn

    # ★ 同理，测试绝不能真的执行 shutdown ★
    def no_power(*a, **kw):
        raise AssertionError(f"测试里不允许执行系统命令：{a[0] if a else ''}")
    bot.run_cmd = no_power
    return bot


def test_bot_only_obeys_owner_private_chat():
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(tmp)
        bot.bot = FakeBotConn()
        bot._on_event({"post_type": "message", "message_type": "private",
                       "user_id": 99999, "raw_message": "投递"})
        bot._on_event({"post_type": "message", "message_type": "group",
                       "user_id": 10001, "raw_message": "投递"})
        bot._on_event({"post_type": "message", "message_type": "private",
                       "user_id": 10001, "raw_message": "帮助"})
        check("陌生人、群消息、不带 / 的都不理", bot.bot.sent, [])
        bot._on_event({"post_type": "message", "message_type": "private",
                       "user_id": 10001, "raw_message": "/帮助"})
        check("大号的私聊会回复", len(bot.bot.sent), 1)
        check_true("回复的是帮助", bot.bot.sent and "指令" in bot.bot.sent[0][1])
        check("没有误启动任务", bot.proc, None)
        bot._on_event({"post_type": "message", "message_type": "private",
                       "user_id": 10001, "raw_message": "/&#91;x&#93;"})
        check_true("CQ 转义会还原", len(bot.bot.sent) == 2 and "「[x]」" in bot.bot.sent[1][1])


def test_bot_command_parsing():
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(tmp)
        calls = []
        bot.start = (lambda live, limit, keywords=None:
                     calls.append((live, limit, keywords)) or "ok")
        bot.handle("投递")
        bot.handle("投递 10")
        bot.handle("投递 大模型算法 Agent")
        bot.handle("投递 大模型算法、Agent 10")
        bot.handle("投递 abc")
        bot.handle("演练")
        bot.handle("演练 3")
        bot.handle("搜索 推荐算法")
        check("指令参数解析", calls,
              [(True, None, None), (True, 10, None),
               (True, None, ["大模型算法", "Agent"]),
               (True, 10, ["大模型算法", "Agent"]),
               (True, None, ["abc"]),
               (False, 5, None), (False, 3, None),
               (True, None, ["推荐算法"])])
        check_true("未知指令有提示", "不认识" in bot.handle("吃饭"))
        check_true("空闲时停止", "没有任务" in bot.handle("停止"))
        check_true("空闲状态", "空闲" in bot.handle("状态"))


def test_bot_keywords_set_and_clear():
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(tmp)
        reply = bot.handle("关键词 大模型算法、Agent RAG")
        check_true("设定成功", "3 个" in reply)
        check("写进关键词文件", bot.pending_keywords(), ["大模型算法", "Agent", "RAG"])
        plan = bot.handle("关键词")
        check_true("查看时列出关键词", "大模型算法" in plan and "QQ 指定" in plan)
        check_true("状态里提示已设定", "已设定 3 个关键词" in bot.handle("状态"))
        check_true("清空回复", "已清空" in bot.handle("关键词 清空"))
        check("文件被删掉", bot.keywords_path.exists(), False)
        check("恢复成 config 的关键词", bot.pending_keywords(), [])


def test_bot_start_passes_keywords_to_greet():
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(tmp)
        captured: dict = {}

        class Proc:
            pid = 4321
            returncode = None

            def poll(self):
                return None

        def fake_popen(argv, **kw):
            captured["argv"] = list(argv)
            return Proc()

        bot.popen = fake_popen
        bot._watch = lambda proc: None      # 别起后台线程
        bot._set_pending(["大模型算法", "Agent"])
        reply = bot.start(live=True, limit=10)
        argv = captured["argv"]
        check_true("子进程带上了 --keywords", "--keywords" in argv)
        check("关键词用竖线连接",
              argv[argv.index("--keywords") + 1] if "--keywords" in argv else None,
              "大模型算法|Agent")
        check_true("实发走 --auto", "--auto" in argv)
        check_true("回复里有条数", "最多 10 条" in reply)
        check_true("回复里有关键词数", "2 个关键词" in reply)

        # 指令里直接带关键词时，优先于「/关键词」设下的那组
        bot.proc = None
        bot.start(live=False, limit=None, keywords=["推荐算法"])
        argv = captured["argv"]
        check("指令关键词优先",
              argv[argv.index("--keywords") + 1] if "--keywords" in argv else None,
              "推荐算法")
        check_true("演练走 --dry", "--dry" in argv)


def test_bot_today_lists_sent_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(tmp)
        check_true("没库时也能回", "还没有" in bot.handle("今日"))
        conn = sqlite3.connect(str(Path(tmp) / "t.db"))
        conn.execute("CREATE TABLE greeted (job_key TEXT PRIMARY KEY, title TEXT, "
                     "company TEXT, salary TEXT, message TEXT, status TEXT, note TEXT, "
                     "greeted_at TEXT)")
        now = datetime.now().isoformat(timespec="seconds")
        conn.executemany("INSERT INTO greeted VALUES (?,?,?,?,?,?,?,?)", [
            ("a", "Agent算法实习生", "百度", "400元/天", "", "sent", "", now),
            ("b", "数据标注", "某司", "", "", "filtered", "", now),
            ("c", "推荐算法实习", "字节", "", "", "sent", "", "2020-01-01T10:00:00"),
        ])
        conn.commit()
        conn.close()
        reply = bot.handle("今日")
        check_true("列出今天发出的", "Agent算法实习生 @ 百度" in reply)
        check_true("不列被过滤的", "数据标注" not in reply)
        check_true("不列往日的", "字节" not in reply)
        check_true("计数正确", "今日已投 1 条" in reply)
        check_true("状态里也有计数", "今日已投 1 条" in bot.handle("状态"))


# ---------------------------------------------------------------------------
#  浏览器独占锁
# ---------------------------------------------------------------------------
HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from autoboss.lock import BrowserLock
lock = BrowserLock(Path(sys.argv[1]))   # 必须留住引用：被回收会关掉句柄、锁随之释放
assert lock.acquire()
print("held", flush=True)
time.sleep(60)
"""


def hold_lock_in_subprocess(path: Path):
    """另起一个进程拿住锁，模拟正在跑的 daily_greet.bat。"""
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(path),
                             str(Path(__file__).resolve().parent.parent)],
                            stdout=subprocess.PIPE, text=True)
    check("子进程拿到了锁", proc.stdout.readline().strip(), "held")
    return proc


def test_lock_is_exclusive_and_freed_on_kill():
    from autoboss.lock import BrowserLock
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".browser.lock"
        proc = hold_lock_in_subprocess(path)
        try:
            check("别的进程占着时拿不到", BrowserLock(path).acquire(), False)
            check("能看出被占用", BrowserLock(path).held_by_other(), True)
        finally:
            proc.kill()
            proc.wait()
        # 被强杀也不留死锁：系统会释放
        lock = BrowserLock(path)
        check("持有进程被杀后能拿到", lock.acquire(), True)
        lock.release()


def test_greet_refused_while_browser_busy():
    import run
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg()
        lock_file = Path(tmp) / ".browser.lock"
        flag = Path(tmp) / ".stop_request"
        cfg.__class__ = type("TmpConfig", (cfgmod.Config,), {
            "lock_path": property(lambda self: lock_file),
            "stop_flag": property(lambda self: flag)})
        ran = []
        guarded = run.with_browser_lock(lambda c, a: ran.append(1) or 0)

        proc = hold_lock_in_subprocess(lock_file)
        try:
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                code = guarded(cfg, None)
            check("浏览器被占用时退出码 = EXIT_BUSY", code, run.EXIT_BUSY)
            check("被拒绝时任务一步都没跑", ran, [])
        finally:
            proc.kill()
            proc.wait()

        flag.touch()      # 上次崩溃留下的停止标记
        check("锁空出来后正常运行", guarded(cfg, None), 0)
        check("任务确实跑了", ran, [1])
        check("过期的停止标记被清掉", flag.exists(), False)


def test_bot_sees_task_started_elsewhere():
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(tmp)
        lock_file = Path(tmp) / ".browser.lock"
        flag = Path(tmp) / ".stop_request"
        base = type(bot.cfg)
        bot.cfg.__class__ = type("TmpConfig2", (base,), {
            "stop_flag": property(lambda self: flag)})
        from autoboss.lock import BrowserLock
        bot.lock = BrowserLock(lock_file)

        proc = hold_lock_in_subprocess(lock_file)
        try:
            reply = bot.handle("投递")
            check_true("外部任务在跑时拒绝投递", "不启动" in reply)
            check("没有启动子进程", bot.proc, None)
            check_true("状态能看到外部任务", "不是从 QQ 启动" in bot.handle("状态"))
            check_true("停止对外部任务也有效", "已发出停止指令" in bot.handle("停止"))
            check("停止标记已建好", flag.exists(), True)
        finally:
            proc.kill()
            proc.wait()
        check_true("外部任务结束后显示空闲", "空闲" in bot.handle("状态"))


def test_bot_power_commands():
    import subprocess
    from autoboss import qqbot
    with tempfile.TemporaryDirectory() as tmp:
        bot = make_bot(tmp)
        calls = []
        codes = iter([0, 0, 0, 1])
        bot.run_cmd = lambda argv, **kw: (calls.append(list(argv))
                                          or subprocess.CompletedProcess(argv, next(codes), "", ""))
        old_name = qqbot.os.name
        try:
            qqbot.os.name = "nt"
            reboot = bot.handle("重启")
            shutdown = bot.handle("关机")
            cancel = bot.handle("取消")
            nothing = bot.handle("取消")
        finally:
            qqbot.os.name = old_name
        delay = str(qqbot.POWER_DELAY_SEC)
        check("重启命令", calls[0][:4], ["shutdown", "/r", "/t", delay])
        check("关机命令", calls[1][:4], ["shutdown", "/s", "/t", delay])
        check("取消命令", calls[2], ["shutdown", "/a"])
        check_true("重启回复带倒计时和取消方法", "重启" in reboot and "/取消" in reboot)
        check_true("关机回复", "关机" in shutdown)
        check_true("取消成功", "已取消" in cancel)
        check_true("没有可取消的", "没有待执行" in nothing)


# ---------------------------------------------------------------------------
def make_launcher(snowluma_up: bool, qq_up: bool, auto_launch: bool = True):
    cfg = cfgmod.load()
    cfg._raw["qq"] = {"auto_launch": auto_launch, "qq_path": sys.executable}
    launcher = QQLauncher(cfg)
    launcher.console_log_path = None        # 测试别在真实 SnowLuma/logs 里建文件
    calls: list[list[str]] = []
    launcher.popen = lambda argv, **kw: calls.append(list(argv))
    launcher.port_open = lambda port: snowluma_up
    launcher.process_running = lambda image: qq_up
    return launcher, calls


def test_launcher_starts_whats_missing():
    launcher, calls = make_launcher(snowluma_up=False, qq_up=False)
    with redirect_stdout(io.StringIO()):
        launcher.ensure()
    check("两个都没开 → 开两个", len(calls), 2)
    check_true("先开 SnowLuma", calls and calls[0][-1].endswith("launcher.bat"))
    check_true("再开 QQ", len(calls) > 1 and calls[1] == [sys.executable])

    launcher, calls = make_launcher(snowluma_up=True, qq_up=False)
    with redirect_stdout(io.StringIO()):
        launcher.ensure()
    check("SnowLuma 已开 → 只开 QQ", calls, [[sys.executable]])


def test_launcher_skips_running_or_disabled():
    launcher, calls = make_launcher(snowluma_up=True, qq_up=True)
    with redirect_stdout(io.StringIO()):
        launcher.ensure()
    check("都在跑 → 什么都不开", calls, [])

    launcher, calls = make_launcher(snowluma_up=False, qq_up=False, auto_launch=False)
    with redirect_stdout(io.StringIO()):
        launcher.ensure()
    check("auto_launch=false → 什么都不开", calls, [])


if __name__ == "__main__":
    for fn in (test_notifier_sends_private_msg, test_notifier_disabled_or_down_is_harmless,
               test_notifier_flushes_outbox_when_back_online,
               test_notifier_queues_when_onebot_reports_failure,
               test_agent_notifies_each_sent_job, test_agent_stops_on_remote_flag,
               test_bot_only_obeys_owner_private_chat, test_bot_command_parsing,
               test_bot_keywords_set_and_clear, test_bot_start_passes_keywords_to_greet,
               test_bot_today_lists_sent_jobs, test_lock_is_exclusive_and_freed_on_kill,
               test_greet_refused_while_browser_busy, test_bot_sees_task_started_elsewhere,
               test_launcher_starts_whats_missing, test_launcher_skips_running_or_disabled,
               test_bot_power_commands):
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
