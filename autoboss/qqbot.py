"""`run.py bot`：常驻的 QQ 遥控进程。

只听大号（`[qq].owner_qq`）的私聊，群消息、陌生人一律不理。

投递任务以**子进程**方式跑 `run.py greet ...`，而不是在本进程里起线程：
* 投递那边崩了、卡死了，bot 照样能收指令、能把它杀掉；
* 投递进程自己就会推送每条结果（autoboss.qq.Notifier），
  所以用 bat / 计划任务直接跑 greet 时，大号同样收得到通知。

「停止」走两步：先建停止标记文件，投递进程在扫描/投递的间隙看到就正常收尾
（落库、推总结）；等太久还没退出再强杀。

浏览器独占锁（autoboss/lock.py）让 bot 能看见**不是它启动的**投递（比如双击的
daily_greet.bat）：这时「投递」会被拒绝，「状态」能看到它在跑，「停止」也能让它收尾
（停止标记对所有投递进程都有效；只是外部进程 bot 没法强杀）。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from websockets.exceptions import ConnectionClosed

from . import config as cfgmod
from . import keywords as kw
from . import preferences
from .lock import BrowserLock
from .qq import Notifier, OneBot
from .qqlaunch import QQLauncher

HELP = """🤖 auto_boss 指令（以 / 开头）

📮 投递
/投递                    实发，投到平台额度用完，关键词用 config.toml 的
/投递 10                 实发，本次最多 10 条
/投递 大模型算法 Agent   实发，**只搜**这几个关键词（空格/逗号/顿号分隔）
/投递 大模型算法 本科 300
                         关键词 + 学历 + 日薪一起给：只搜大模型算法、
                         学历要求不高于本科、日薪中位 ≥300
/搜索 关键词             同「/投递 关键词」
/演练 [关键词...] [5]    只筛选不发送（默认 5 条）

🎓 学历 / 薪资（设定后一直沿用，下次 /投递 自动带上）
/学历                    看当前学历设置
/学历 本科               设定你的最高学历：岗位要求高于它的不投
                         可选 不限 / 大专 / 本科 / 硕士 / 博士
/薪资                    看当前日薪下限
/薪资 300                设定日薪下限；也可写 月薪8000、8k，自动折成日薪
/薪资 不限               不按薪资筛

💡 数字怎么认：裸数字默认是**条数**（/投递 关键词 10）。
   想改日薪写 薪资300 / 300元/天 / 300/天 / ≥300；
   「/投递 关键词 本科 300」里和学历一起写的 300 也按日薪算。
   写「10条」永远不会有歧义。
   指令里写「学历不限」「薪资不限」可以一次清掉对应设置。

/关键词                  看当前检索关键词
/关键词 大模型算法 Agent  设定下次任务的检索关键词（会一直沿用）
/关键词 清空              恢复用 config.toml 里的 [search].queries
/停止                    让正在跑的任务收尾退出
/状态                    是否在跑、今日已投多少、学历/薪资设置
/今日                    今天投了哪些岗位
/统计                    今日各状态计数 + 最近几次运行
/日志 [20]               最近一份运行日志的末尾几行
/重启                    重启电脑（60 秒后执行）
/关机                    关闭电脑（60 秒后执行）
/取消                    撤销还没执行的重启 / 关机
/帮助                    显示本说明

每次任务开始前会把本次要搜的关键词和筛选条件单独推给你。"""

def cq_unescape(text: str) -> str:
    """raw_message 是 CQ 码字符串，[ ] , & 会被转义成 &#91; 之类，还原成原字符。"""
    return (text.replace("&#91;", "[").replace("&#93;", "]")
            .replace("&#44;", ",").replace("&amp;", "&"))


# 发出停止标记后最多等这么久，还不退出就强杀。
# 两次投递之间的随机间隔最长 60s，再留出一次点击 + 读屏的时间。
STOP_GRACE_SEC = 150
# 单条 QQ 消息别太长，列表类回复截断到这么多行
MAX_LINES = 40
# /重启、/关机 发出后等这么久才执行，期间发 /取消 可以撤销（防手滑）
POWER_DELAY_SEC = 60
# 与 run.py 的 EXIT_BUSY 一致：浏览器被占用、任务没有启动
EXIT_BUSY = 3


class QQBot:
    def __init__(self, cfg, run_py: Path):
        self.cfg = cfg
        self.run_py = run_py
        self.root = run_py.parent
        self.notifier = Notifier(cfg)
        self.owner = self.notifier.owner
        self.bot: OneBot | None = None
        self.lock = BrowserLock(cfg.lock_path)
        # 「/关键词 ...」设下来、下次任务沿用的关键词（一行一个）
        self.keywords_path = cfg.qq_keywords_path
        # 「/学历」「/薪资」「/投递 关键词 学历 日薪」设下来的筛选条件（JSON）
        self.prefs_path = cfg.qq_prefs_path
        # 起子进程的入口。测试里换成桩，保证测试永远不会真的起一个实发任务
        self.popen = subprocess.Popen
        # 执行 shutdown 命令的入口。测试里换成桩，保证测试永远不会真的关机
        self.run_cmd = subprocess.run

        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.task_desc = ""
        self.task_started = 0.0
        self._stop_deadline = 0.0
        self._last_report = 0.0
        self._last_disconnect_report = 0.0

    # ------------------------------------------------------------------
    def run(self) -> int:
        if not self.cfg.get("qq.enabled", False) or self.owner <= 0:
            print("QQ 机器人没启用：在 config.toml 的 [qq] 里设 enabled = true，并在 .env 里填上 OWNER_QQ（你的大号 QQ 号）。")
            return 1

        QQLauncher(self.cfg).ensure()
        print(f"连接 OneBot：{self.notifier.ws_url}，只接受 QQ {self.owner} 的私聊指令")
        backoff = 3.0
        first = True
        failures = 0
        while True:
            try:
                with OneBot(self.notifier.ws_url, self.notifier.token) as bot:
                    self.bot = bot
                    backoff = 3.0
                    failures = 0
                    print(f"[{datetime.now():%H:%M:%S}] 已连接")
                    # 断网期间没发出去的通知，连上先补发
                    self.notifier.flush()
                    if first:
                        self.reply("🤖 auto_boss 已上线，发「/帮助」查看指令")
                        first = False
                    for event in bot.events():
                        self._on_event(event)
            except KeyboardInterrupt:
                print("\nbot 已退出。")
                if self._running():
                    print("  投递任务仍在后台运行，会照常推送结果；"
                          "要停可以重开 bot 后发「/停止」。")
                return 0
            except (OSError, TimeoutError, ConnectionClosed) as exc:
                failures += 1
                print(f"[{datetime.now():%H:%M:%S}] 连接断开（{type(exc).__name__}: {exc}），"
                      f"{backoff:.0f}s 后重连")
                if failures >= 10:
                    self.report_disconnect(exc, failures)
            except Exception as exc:  # noqa: BLE001
                # 开机自启是隐藏窗口，没人看得到栈 —— 推到 QQ 上（断网时排队）
                self.report_crash(exc)
            finally:
                self.bot = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def report_crash(self, exc: BaseException) -> None:
        """把异常和最近日志推给大号。断网时 Notifier 会先存进 outbox，连上后补发。"""
        print(f"[{datetime.now():%H:%M:%S}] bot 出错：{type(exc).__name__}: {exc}")
        tb = traceback.format_exc().strip()
        if tb and not tb.endswith("NoneType: None"):
            print(tb)
        # 同一类错误别刷屏：10 分钟内只推第一条
        now = time.time()
        if now - self._last_report < 600:
            return
        self._last_report = now
        head = f"❌ auto_boss bot 出错：{type(exc).__name__}: {exc}"
        tail = self._log_tail(30)
        body = f"{head}\n\n最近日志：\n{tail}" if tail else (f"{head}\n\n{tb}" if tb else head)
        # QQ 单条消息太长会被吞，截断
        self.notifier.send(body[-1800:])

    def report_disconnect(self, exc: BaseException, failures: int) -> None:
        """连续多次连不上 OneBot：推一条带日志尾巴的提醒。

        半小时最多推一条；断网时 OneBot 也连不上，消息会进 outbox 等恢复。
        """
        now = time.time()
        if now - self._last_disconnect_report < 1800:
            return
        self._last_disconnect_report = now
        head = (f"⚠️ auto_boss 已经连续 {failures} 次连不上 QQ/OneBot："
                f"{type(exc).__name__}: {exc}")
        tail = self._log_tail(20)
        body = f"{head}\n\n最近日志：\n{tail}" if tail else head
        self.notifier.send(body[-1800:])

    def _log_tail(self, lines: int = 30) -> str:
        """开机自启时 stdout 被重定向到 logs/bot_YYYYMMDD.log，取最后几行。"""
        path = self.root / "logs" / f"bot_{datetime.now():%Y%m%d}.log"
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:]).strip()
        except OSError:
            return ""

    def reply(self, text: str):
        if self.bot is not None:
            self.bot.send_private(self.owner, text)

    # ------------------------------------------------------------------
    def _on_event(self, event: dict):
        if (event.get("post_type") != "message"
                or event.get("message_type") != "private"
                or int(event.get("user_id") or 0) != self.owner):
            return
        text = cq_unescape(str(event.get("raw_message") or "")).strip()
        # 只有 / 开头的才算指令，大号跟小号平常聊天不会被当成指令
        if not text.startswith(("/", "／")):
            return
        text = text[1:].strip()
        if not text:
            return
        print(f"[{datetime.now():%H:%M:%S}] 收到指令：{text}")
        try:
            self.reply(self.handle(text))
        except Exception as exc:  # noqa: BLE001 —— 一条指令出错不能把 bot 带崩
            self.reply(f"❌ 执行出错：{type(exc).__name__}: {exc}")

    def handle(self, text: str) -> str:
        """解析一条指令，返回要回复的文字。"""
        parts = text.split()
        cmd, rest = parts[0].lower(), parts[1:]
        arg = rest[0] if rest else ""

        if cmd in ("帮助", "help", "菜单", "?", "？"):
            return HELP
        if cmd in ("投递", "开始", "start", "搜索", "search"):
            return self.task_cmd(rest, live=True)
        if cmd in ("演练", "dry"):
            return self.task_cmd(rest, live=False)
        if cmd in ("学历", "education", "edu"):
            return self.education_cmd(" ".join(rest))
        if cmd in ("薪资", "日薪", "工资", "salary", "wage"):
            return self.wage_cmd(" ".join(rest))
        if cmd in ("关键词", "检索词", "keywords", "kw"):
            return self.keyword_cmd(" ".join(rest))
        if cmd in ("停止", "stop"):
            return self.stop()
        if cmd in ("状态", "status"):
            return self.status()
        if cmd in ("今日", "今天", "记录", "today"):
            return self.today()
        if cmd in ("统计", "stats"):
            return self.stats()
        if cmd in ("日志", "log"):
            return self.log_tail(min(self._int(arg, 15) or 15, MAX_LINES))
        if cmd in ("重启", "reboot"):
            return self.power(reboot=True)
        if cmd in ("关机", "shutdown"):
            return self.power(reboot=False)
        if cmd in ("取消", "cancel"):
            return self.power_cancel()
        return f"不认识的指令「{parts[0]}」，发「/帮助」查看可用指令"

    # ------------------------------------------------------------------
    #  电脑电源
    # ------------------------------------------------------------------
    def power(self, reboot: bool) -> str:
        what = "重启" if reboot else "关机"
        if os.name != "nt":
            return f"❌ 只支持 Windows，没有{what}"
        r = self.run_cmd(
            ["shutdown", "/r" if reboot else "/s", "/t", str(POWER_DELAY_SEC),
             "/c", f"auto_boss：QQ 遥控{what}"],
            capture_output=True, text=True, errors="ignore",
        )
        if r.returncode != 0:
            # 1190 = 已经有一个关机/重启在倒计时
            msg = (r.stderr or r.stdout or "").strip() or f"退出码 {r.returncode}"
            return f"❌ {what}没有安排上：{msg}\n已有倒计时的话先发「/取消」"
        print(f"  已安排{what}：{POWER_DELAY_SEC}s 后")
        note = ""
        if self._running() or self._external_running():
            self.stop()
            note = "\n正在跑的投递已发出停止指令，来不及收尾的会被关机打断"
        return (f"⚠️ 电脑将在 {POWER_DELAY_SEC} 秒后{what}{note}\n"
                f"反悔的话 {POWER_DELAY_SEC} 秒内发「/取消」")

    def power_cancel(self) -> str:
        if os.name != "nt":
            return "❌ 只支持 Windows"
        r = self.run_cmd(["shutdown", "/a"], capture_output=True, text=True, errors="ignore")
        if r.returncode != 0:
            return "没有待执行的重启 / 关机"
        print("  已取消重启 / 关机")
        return "✅ 已取消重启 / 关机"

    @staticmethod
    def _int(s: str, default):
        try:
            n = int(s)
            return n if n > 0 else default
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    #  学历 / 薪资（和「/投递」合并：关键词、学历、日薪一条指令给全）
    # ------------------------------------------------------------------
    def _prefs(self) -> preferences.Prefs:
        return preferences.load_prefs(self.prefs_path)

    def _save_prefs(self, prefs: preferences.Prefs) -> None:
        preferences.save_prefs(self.prefs_path, prefs)

    @staticmethod
    def prefs_help() -> str:
        return ("学历可写 不限 / 大专 / 本科 / 硕士 / 博士；"
                "薪资写日薪 300，或 月薪8000、8k（自动折成日薪）；"
                "「不限」= 不按这项筛。设定存在 .qq_prefs.json，"
                "删掉它就全部回到 config.toml。")

    def education_cmd(self, raw: str) -> str:
        """「/学历 [学历]」：不带参数 = 查看，带了 = 设定。"""
        prefs = self._prefs()
        # 多写几个词时只看第一个：「/学历 本科 硕士」按本科算
        text = (raw.split() or [""])[0]
        if not text:
            return ("🎓 当前学历 / 薪资设置\n" + preferences.describe(prefs)
                    + "\n\n" + self.prefs_help())
        if preferences.is_clear(text):
            prefs = preferences.Prefs(education="", min_wage=prefs.min_wage)
            self._save_prefs(prefs)
            return ("✅ 已取消学历限制，之后岗位要求多高都不拦\n\n"
                    + preferences.describe(prefs))
        value = preferences.normalize_education(text)
        if value is None:
            hint = ("\n「X及以上」是岗位要求的写法，这里填你自己的最高学历。"
                    if preferences.looks_like_education(text) else "")
            return f"❌ 看不懂这个学历「{text}」。{hint}\n{self.prefs_help()}"
        prefs = preferences.Prefs(education=value, min_wage=prefs.min_wage)
        self._save_prefs(prefs)
        return (f"✅ 已设定学历：{value}（岗位要求高于它的不投）\n\n"
                + preferences.describe(prefs))

    def wage_cmd(self, raw: str) -> str:
        """「/薪资 [日薪]」：不带参数 = 查看，带了 = 设定。"""
        prefs = self._prefs()
        text = raw.strip()
        if not text:
            return ("💰 当前学历 / 薪资设置\n" + preferences.describe(prefs)
                    + "\n\n" + self.prefs_help())
        if preferences.is_clear(text):
            prefs = preferences.Prefs(education=prefs.education, min_wage=0.0)
            self._save_prefs(prefs)
            return "✅ 已取消日薪限制\n\n" + preferences.describe(prefs)
        wage = preferences.parse_wage(text)
        problem = preferences.wage_problem(wage)
        if problem:
            return f"❌ {problem}\n{self.prefs_help()}"
        prefs = preferences.Prefs(education=prefs.education, min_wage=wage)
        self._save_prefs(prefs)
        return (f"✅ 已设定日薪下限：≥{wage:g} 元/天（区间取中位）\n\n"
                + preferences.describe(prefs))

    def task_cmd(self, rest: list[str], live: bool) -> str:
        """「/投递 ...」「/演练 ...」：关键词、条数、学历、日薪一条指令给全。

        `/投递 大模型算法 本科 300` = 只搜大模型算法、学历要求 ≤本科、日薪 ≥300。
        """
        args = preferences.parse_task_args(rest)
        if args.errors:
            return ("❌ " + "；".join(args.errors)
                    + "\n例：/投递 大模型算法 本科 300   或   /投递 大模型算法 10")
        prefs = self._prefs()
        if args.touched:
            prefs = prefs.merged(args)
            self._save_prefs(prefs)
        words = args.keywords or self.pending_keywords()
        limit = args.limit
        if not live and limit is None:
            limit = 5                 # 「/演练」默认 5 条，和以前一致
        reply = self.start(live=live, limit=limit, keywords=words or None, prefs=prefs)
        if args.touched:
            reply = ("✅ 已记住 " + preferences.status_line(prefs)
                     + "（下次「/投递」沿用）\n" + reply)
        return reply

    # ------------------------------------------------------------------
    #  检索关键词
    # ------------------------------------------------------------------
    # 「/关键词 清空」等恢复 config 的写法
    _CLEAR_WORDS = ("清空", "清除", "默认", "重置", "恢复", "clear", "reset", "default")

    def pending_keywords(self) -> list[str]:
        """QQ 上设定、下次任务沿用的关键词；没设过就是空列表。"""
        try:
            text = self.keywords_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return kw.normalize(text.splitlines())

    def _set_pending(self, words: list[str]) -> None:
        if not words:
            self.keywords_path.unlink(missing_ok=True)
            return
        self.keywords_path.parent.mkdir(parents=True, exist_ok=True)
        self.keywords_path.write_text("\n".join(words) + "\n", encoding="utf-8")

    def keyword_cmd(self, raw: str) -> str:
        """「/关键词 [关键词...]」：不带参数 = 查看，带了 = 设定，清空 = 恢复 config。"""
        words = kw.split_keywords(raw)
        if not words:
            return self.plan_text()
        if words[0].lower() in self._CLEAR_WORDS:
            self._set_pending([])
            return ("✅ 已清空 QQ 指定的关键词，之后的任务用 config.toml 的 "
                    "[search].queries\n\n" + self.plan_text())
        self._set_pending(words)
        return (f"✅ 已设定 {len(words)} 个检索关键词，下次「/投递」「/演练」生效：\n"
                + kw.block(words)
                + "\n发「/关键词」查看，「/关键词 清空」恢复 config.toml")

    def plan_text(self) -> str:
        """当前会搜哪些关键词 —— 「/关键词」的回复，也是任务开始推送的同一份编排。"""
        pending = self.pending_keywords()
        cfg = self.cfg
        try:
            # bot 可能已经常驻好几天，config.toml 改完要能立即反映出来；
            # 读坏了（正在编辑、文件暂时不在）就退回启动时那份。
            cfg = cfgmod.load()
        except (Exception, SystemExit):  # noqa: BLE001
            pass
        plan = kw.planned_sources(cfg, pending)
        picked = kw.queries_of(plan)
        extra = [kw.label(kind, q) for kind, q in plan if kind != "search"]

        head = ("🔎 当前检索关键词（QQ 指定，发「/关键词 清空」恢复 config.toml）"
                if pending else "🔎 当前检索关键词（来自 config.toml）")
        lines = [head]
        if picked:
            lines.append(f"共 {len(picked)} 个，按优先级：")
            lines.append(kw.block(picked))
        else:
            lines.append("没有配置关键词，只走推荐流")
        if extra:
            lines.append("另外还会走：" + " → ".join(extra))
        lines.append(preferences.status_line(self._prefs()))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  任务控制
    # ------------------------------------------------------------------
    def _running(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def _external_running(self) -> bool:
        """有不是 bot 启动的进程正在用浏览器（bat / 命令行 / login / doctor）。"""
        return not self._running() and self.lock.held_by_other()

    def start(self, live: bool, limit: int | None, keywords: list[str] | None = None,
              prefs: preferences.Prefs | None = None) -> str:
        if self._running():
            return f"已经有任务在跑：{self.task_desc}\n要换任务先发「/停止」"
        if self._external_running():
            return ("已经有一个任务在使用浏览器（不是从 QQ 启动的，比如 daily_greet.bat）。\n"
                    "这次不启动，免得两个任务抢同一个浏览器。要换任务先发「/停止」")

        # 这次只搜哪些词：指令里带的 > 之前「/关键词」设定的 > config.toml
        words = kw.normalize(keywords) if keywords else self.pending_keywords()
        # 学历 / 日薪：None = 没设过（用 config.toml），"" / 0 = 明确不限
        prefs = self._prefs() if prefs is None else prefs

        args = [sys.executable, str(self.run_py), "greet"]
        if live:
            args.append("--auto")       # 实发、不确认、出错自动重启
        else:
            args.append("--dry")
        if limit is not None:
            args += ["--limit", str(limit)]
        if words:
            # 竖线分隔：关键词里可能带空格或逗号，用参数传更稳
            args += ["--keywords", "|".join(words)]
        if prefs.education is not None:
            # 「不限」也要显式传：它要盖住 config.toml 里的 max_education
            args += ["--education", prefs.education or "不限"]
        if prefs.min_wage is not None:
            args += ["--min-wage", f"{prefs.min_wage:g}"]

        self.cfg.stop_flag.unlink(missing_ok=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        # 独立进程组：在 bot 窗口按 Ctrl+C 不会连带把投递进程打断
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        # 输出不用接：greet 自己会写 logs/greet_*.log，「日志」指令读的就是它
        proc = self.popen(args, cwd=str(self.root), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)
        desc = (("实发" if live else "演练")
                + (f"，最多 {limit} 条" if limit else "，投到额度用完"))
        if words:
            desc += f"，只搜 {len(words)} 个关键词"
        if prefs.education is not None:
            desc += "，学历" + (prefs.education or "不限")
        if prefs.min_wage is not None:
            desc += "，日薪" + (f"≥{prefs.min_wage:g}" if prefs.min_wage else "不限")
        with self._lock:
            self.proc = proc
            self.task_desc = desc
            self.task_started = time.time()
            self._stop_deadline = 0.0
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        print(f"  启动任务：{desc}（pid {proc.pid}）")

        warn = ("\n⚠️ 实发模式：会真实点「立即沟通」、消耗额度、触达 HR"
                if live else "")
        extra = ""
        if not prefs.is_empty():
            extra = "\n" + preferences.status_line(prefs)
        return (f"▶️ 已启动：{desc}（pid {proc.pid}）{warn}{extra}\n"
                "本次检索关键词马上推送给你\n每投出一条也会推送")

    def stop(self) -> str:
        if self._external_running():
            self.cfg.stop_flag.touch()
            return ("⏹ 已发出停止指令。正在跑的任务不是从 QQ 启动的，"
                    "它会在当前这一条处理完后自己收尾（这种任务没法强制结束）")
        if not self._running():
            return "现在没有任务在跑"
        self.cfg.stop_flag.touch()
        with self._lock:
            self._stop_deadline = time.time() + STOP_GRACE_SEC
        return (f"⏹ 已发出停止指令，当前这一条处理完就收尾"
                f"（最多等 {STOP_GRACE_SEC}s，超时强制结束）")

    def _watch(self, proc: subprocess.Popen):
        """等子进程退出：超过停止宽限期就强杀；退出后清标记、报告异常退出。"""
        killed = False
        while proc.poll() is None:
            with self._lock:
                deadline = self._stop_deadline
            if deadline and time.time() > deadline and not killed:
                proc.kill()
                killed = True
            time.sleep(1.0)

        self.cfg.stop_flag.unlink(missing_ok=True)
        with self._lock:
            desc = self.task_desc
            if self.proc is proc:
                self.proc = None
        code = proc.returncode
        print(f"  任务结束：{desc}，退出码 {code}")
        # 正常结束 / 按规则停机时投递进程自己已经推过总结了，
        # 这里只补它没机会说话的情况：被强杀、崩溃。
        if killed:
            self.notifier.send(f"⏹ 任务没有按时收尾，已强制结束（{desc}）")
        elif code == EXIT_BUSY:
            self.notifier.send("⚠️ 没有启动：另一个任务刚好占用了浏览器，等它结束再发「/投递」")
        elif code not in (0, 1):
            self.notifier.send(f"❌ 任务异常退出（退出码 {code}，{desc}），发「/日志」看原因")

    # ------------------------------------------------------------------
    #  查询
    # ------------------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.cfg.db_path.exists():
            return []
        conn = sqlite3.connect(str(self.cfg.db_path))
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:     # 还没建表
            return []
        finally:
            conn.close()

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _sent_today(self) -> int:
        rows = self._query("SELECT COUNT(*) FROM greeted WHERE status='sent' "
                           "AND substr(greeted_at,1,10)=?", (self._today(),))
        return int(rows[0][0]) if rows else 0

    def status(self) -> str:
        cap = int(self.cfg.get("limits.daily_cap", 10))
        head = f"今日已投 {self._sent_today()} 条（本地上限 {cap}）"
        pending = self.pending_keywords()
        if pending:
            head += f"\n🔎 已设定 {len(pending)} 个关键词（发「/关键词」查看）"
        head += "\n" + preferences.status_line(self._prefs())
        if self._external_running():
            return f"🏃 有任务在运行（不是从 QQ 启动的，比如 daily_greet.bat）\n{head}"
        if not self._running():
            return f"💤 空闲\n{head}"
        mins = (time.time() - self.task_started) / 60
        with self._lock:
            stopping = bool(self._stop_deadline)
            pid = self.proc.pid if self.proc else "?"
        state = "收尾中" if stopping else "运行中"
        return f"🏃 {state}：{self.task_desc}\n已运行 {mins:.0f} 分钟（pid {pid}）\n{head}"

    def today(self) -> str:
        rows = self._query(
            "SELECT title, company, salary, greeted_at FROM greeted "
            "WHERE status='sent' AND substr(greeted_at,1,10)=? ORDER BY greeted_at",
            (self._today(),))
        if not rows:
            return "今天还没有投递"
        lines = [f"📋 今日已投 {len(rows)} 条"]
        for i, (title, company, salary, at) in enumerate(rows[-MAX_LINES:],
                                                         max(len(rows) - MAX_LINES, 0) + 1):
            lines.append(f"{i}. {at[11:16]} {title} @ {company}"
                         + (f"（{salary}）" if salary else ""))
        if len(rows) > MAX_LINES:
            lines.insert(1, f"（只列最近 {MAX_LINES} 条）")
        return "\n".join(lines)

    def stats(self) -> str:
        names = {"sent": "已发送", "dry_run": "演练", "failed": "失败",
                 "filtered": "被过滤", "already": "已沟通过"}
        lines = ["📊 今日"]
        rows = self._query("SELECT status, COUNT(*) FROM greeted "
                           "WHERE substr(greeted_at,1,10)=? GROUP BY status", (self._today(),))
        lines += [f"  {names.get(s, s)} {n}" for s, n in rows] or ["  还没有动作"]
        lines.append("最近 5 次运行")
        for started, greeted, failed, stopped in self._query(
                "SELECT started_at, greeted, failed, stopped_by FROM runs "
                "ORDER BY started_at DESC LIMIT 5"):
            lines.append(f"  {(started or '')[5:16]} 发出 {greeted} 失败 {failed}"
                         f" {(stopped or '进行中/未收尾')[:30]}")
        return "\n".join(lines)

    def log_tail(self, n: int) -> str:
        logs = sorted(self.cfg.logs_dir.glob("greet_*.log"))
        if not logs:
            return "还没有运行日志"
        latest = logs[-1]
        text = latest.read_text(encoding="utf-8", errors="replace")
        lines = [ln.rstrip() for ln in text.splitlines()
                 if ln.strip() and not set(ln.strip()) <= set("─═")]
        return f"📄 {latest.name}\n" + "\n".join(lines[-n:])
