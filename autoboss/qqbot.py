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
from datetime import datetime
from pathlib import Path

from websockets.exceptions import ConnectionClosed

from .lock import BrowserLock
from .qq import Notifier, OneBot
from .qqlaunch import QQLauncher

HELP = """🤖 auto_boss 指令（以 / 开头）
/投递          实发，投到平台额度用完
/投递 10       实发，本次最多 10 条
/演练 5        只筛选不发送，最多 5 条（默认 5）
/停止          让正在跑的任务收尾退出
/状态          是否在跑、今日已投多少
/今日          今天投了哪些岗位
/统计          今日各状态计数 + 最近几次运行
/日志 [20]     最近一份运行日志的末尾几行
/重启          重启电脑（60 秒后执行）
/关机          关闭电脑（60 秒后执行）
/取消          撤销还没执行的重启 / 关机
/帮助          显示本说明"""

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
        # 起子进程的入口。测试里换成桩，保证测试永远不会真的起一个实发任务
        self.popen = subprocess.Popen
        # 执行 shutdown 命令的入口。测试里换成桩，保证测试永远不会真的关机
        self.run_cmd = subprocess.run

        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.task_desc = ""
        self.task_started = 0.0
        self._stop_deadline = 0.0

    # ------------------------------------------------------------------
    def run(self) -> int:
        if not self.cfg.get("qq.enabled", False) or self.owner <= 0:
            print("QQ 机器人没启用：在 config.toml 的 [qq] 里设 enabled = true，并在 .env 里填上 OWNER_QQ（你的大号 QQ 号）。")
            return 1

        QQLauncher(self.cfg).ensure()
        print(f"连接 OneBot：{self.notifier.ws_url}，只接受 QQ {self.owner} 的私聊指令")
        backoff = 3.0
        first = True
        while True:
            try:
                with OneBot(self.notifier.ws_url, self.notifier.token) as bot:
                    self.bot = bot
                    backoff = 3.0
                    print(f"[{datetime.now():%H:%M:%S}] 已连接")
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
                print(f"[{datetime.now():%H:%M:%S}] 连接断开（{type(exc).__name__}: {exc}），"
                      f"{backoff:.0f}s 后重连")
            finally:
                self.bot = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

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
        if cmd in ("投递", "开始", "start"):
            return self.start(live=True, limit=self._int(arg, None))
        if cmd in ("演练", "dry"):
            return self.start(live=False, limit=self._int(arg, 5))
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
    #  任务控制
    # ------------------------------------------------------------------
    def _running(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def _external_running(self) -> bool:
        """有不是 bot 启动的进程正在用浏览器（bat / 命令行 / login / doctor）。"""
        return not self._running() and self.lock.held_by_other()

    def start(self, live: bool, limit: int | None) -> str:
        if self._running():
            return f"已经有任务在跑：{self.task_desc}\n要换任务先发「/停止」"
        if self._external_running():
            return ("已经有一个任务在使用浏览器（不是从 QQ 启动的，比如 daily_greet.bat）。\n"
                    "这次不启动，免得两个任务抢同一个浏览器。要换任务先发「/停止」")

        args = [sys.executable, str(self.run_py), "greet"]
        if live:
            args.append("--auto")       # 实发、不确认、出错自动重启
        else:
            args.append("--dry")
        if limit is not None:
            args += ["--limit", str(limit)]

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
        with self._lock:
            self.proc = proc
            self.task_desc = desc
            self.task_started = time.time()
            self._stop_deadline = 0.0
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        print(f"  启动任务：{desc}（pid {proc.pid}）")

        warn = ("\n⚠️ 实发模式：会真实点「立即沟通」、消耗额度、触达 HR"
                if live else "")
        return f"▶️ 已启动：{desc}（pid {proc.pid}）{warn}\n每投出一条都会推送给你"

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
