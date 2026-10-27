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

import base64
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

from websockets.exceptions import ConnectionClosed

from . import config as cfgmod
from . import keywords as kw
from . import preferences
from .browser import CDPConnection, BrowserSession, port_open
from .lock import BrowserLock
from .qq import Notifier, OneBot
from .qqlaunch import QQLauncher

HELP = """🤖 auto_boss 投递指令

/切换              在本人版 / 爸妈版之间切换（关掉当前 Edge，打开另一个）
/投递（条数）       实发，不写条数就投到平台额度用完
/演练（条数）       只筛选不发送（默认 5 条）
/学历（档位/不限）   岗位学历要求，如 /学历 硕士、/学历 不限
/薪资（日薪/不限）   日薪下限，如 /薪资 300；也认 月薪8000 / 8k
/关键词（词.../清空） 检索关键词，如 /关键词 大模型算法 Agent
/配置（清空）       查看当前配置；清空 = 恢复最开始的状态
/停止              让正在跑的任务收尾退出

完整指令表见下图（其余指令都在图里）。"""

DEFAULT_PROFILE = "self"
# 爸妈版的配置别名（--config app.name=parents）；/切换 只切档位，不再做指令前缀。
PARENT_ALIASES = ("爸妈", "父母", "parents", "parent")
PARENTS_CONFIG = "config.parents.toml"

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
        # 当前活跃账号档位：/切换 在 self / parents 之间来回切，并持久化。
        self.active_path = self.root / ".qq_active_profile"
        self.active_profile = self._load_active_profile()
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
        # 当前任务用的是哪份配置：停止标记 / 状态 / 统计都要按它来。
        self.task_cfg = cfg
        self.task_profile = DEFAULT_PROFILE
        self._stop_deadline = 0.0
        self._last_report = 0.0
        self._last_disconnect_report = 0.0

    # ------------------------------------------------------------------
    #  账号档位
    # ------------------------------------------------------------------
    @staticmethod
    def _profile_key(value) -> str:
        key = str(value or DEFAULT_PROFILE).strip().lower()
        if key in ("", DEFAULT_PROFILE, "本人"):
            return DEFAULT_PROFILE
        if key in PARENT_ALIASES:
            return "parents"
        raise RuntimeError(f"不认识的账号档位「{value}」，目前只有本人版和爸妈版")

    def profile_cfg(self, profile=DEFAULT_PROFILE):
        """取某个账号的配置；爸妈版从 config.parents.toml 读，每次现读，改完立即生效。"""
        key = self._profile_key(profile)
        if key == DEFAULT_PROFILE:
            return self.cfg
        path = self.root / PARENTS_CONFIG
        if not path.is_file():
            raise RuntimeError(
                f"找不到爸妈版配置：{path}\n"
                "先确认 config.parents.toml 在项目根目录（README「给爸妈投」一节）。")
        try:
            return cfgmod.load(path)
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from exc

    def profile_label(self, profile=DEFAULT_PROFILE) -> str:
        return "爸妈版" if self._profile_key(profile) == "parents" else "本人版"

    # ------------------------------------------------------------------
    #  /切换：在两个账号档位之间切换（同时只保留一个 Edge）
    # ------------------------------------------------------------------
    def _load_active_profile(self) -> str:
        try:
            key = self.active_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return DEFAULT_PROFILE
        return "parents" if key in ("parents", "爸妈", "父母") else DEFAULT_PROFILE

    def _save_active_profile(self) -> None:
        try:
            self.active_path.write_text(self.active_profile + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"  [switch] 活跃档位写入失败：{exc}")

    def active_cfg(self):
        """当前活跃档位的配置。"""
        return self.profile_cfg(self.active_profile)

    @staticmethod
    def _kill_port_owner(port: int) -> bool:
        """CDP 关不掉时，按监听端口找到 Edge 进程强杀。"""
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, errors="ignore").stdout
        except OSError:
            return False
        for line in out.splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[0].upper() == "TCP"
                    and parts[1].endswith(f":{port}")
                    and parts[3].upper() == "LISTENING"):
                subprocess.run(["taskkill", "/F", "/T", "/PID", parts[4]],
                               capture_output=True, text=True, errors="ignore")
                return True
        return False

    def _close_browser(self, cfg) -> bool:
        """关掉某个档位的 Edge；没开也算成功。"""
        port = int(cfg.get("browser.cdp_port", 9222) or 9222)
        if not port_open(port):
            return True
        ws = None
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=3) as resp:
                ws = json.loads(resp.read().decode("utf-8")).get("webSocketDebuggerUrl")
        except (OSError, ValueError, TypeError):
            ws = None
        if ws:
            try:
                conn = CDPConnection(ws, timeout=5)
                try:
                    conn.send("Browser.close", timeout=5)
                except Exception:  # noqa: BLE001 —— 关闭时连接可能直接断开
                    pass
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.time() + 6
        while time.time() < deadline:
            if not port_open(port):
                return True
            time.sleep(0.25)
        # CDP 不认 Browser.close 时兜底强杀
        if self._kill_port_owner(port):
            time.sleep(1.0)
        return not port_open(port)

    def _launch_browser(self, cfg) -> tuple[bool, str]:
        """打开某个档位的 Edge；已经开着就直接复用。"""
        port = int(cfg.get("browser.cdp_port", 9222) or 9222)
        if port_open(port):
            return True, ""
        try:
            BrowserSession(cfg).launch()
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            return False, str(exc) or type(exc).__name__
        return port_open(port), ""

    def switch_cmd(self, rest: list[str]) -> str:
        """「/切换」：在本人版 / 爸妈版之间切换，只保留一个 Edge。"""
        if rest:
            return ("❌ 「/切换」不带参数；直接发 /切换 就会在本人版和爸妈版之间切换。")
        if self._running():
            return (f"已经有任务在跑：{self.task_desc}\n"
                    "先发「/停止」，等它收尾后再 /切换。")
        if self._external_running():
            return ("已经有一个任务在使用浏览器（不是从 QQ 启动的）。\n"
                    "先发「/停止」，等它收尾后再 /切换。")

        target = "parents" if self.active_profile == DEFAULT_PROFILE else DEFAULT_PROFILE
        try:
            new_cfg = self.profile_cfg(target)
        except RuntimeError as exc:
            return f"❌ {exc}"
        try:
            old_cfg = self.active_cfg()
        except RuntimeError:
            old_cfg = self.cfg
        old_label = self.profile_label(self.active_profile)
        new_label = self.profile_label(target)

        old_closed = self._close_browser(old_cfg)
        self.active_profile = target
        self._save_active_profile()
        launched, error = self._launch_browser(new_cfg)

        lines = [f"🔄 已从{old_label}切换到{new_label}"]
        lines.append(f"Edge：{new_cfg.user_data_dir.name}"
                     f"（端口 {new_cfg.get('browser.cdp_port')}）")
        if not old_closed:
            lines.append(f"⚠️ {old_label}的 Edge 没关掉，可能还开着；"
                         "需要的话手动关一下。")
        if not launched:
            lines.append(f"⚠️ {new_label}的 Edge 没启动：{error or '端口未响应'}；"
                         "下次 /投递 会自动再试。")
        lines.append("现在 /投递、/演练、/配置、/状态 等都对"
                     f"「{new_label}」生效；同一时间只保留一个 Edge。")
        return "\n".join(lines)

    @staticmethod
    def _touch_stop(cfg):
        cfg.stop_flag.parent.mkdir(parents=True, exist_ok=True)
        cfg.stop_flag.touch()

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

    def reply_help(self):
        """`/帮助`：文本 + docs/commands.png **同一条消息**发出去。

        文本只列投递相关指令，完整指令表在图片里；图片用 OneBot 的
        `base64://` 图片段，SnowLuma / NapCat 都能直接上传。
        """
        if self.bot is None:
            return
        image = self.root / "docs" / "commands.png"
        segments = [{"type": "text", "data": {"text": HELP}}]
        if image.is_file():
            try:
                payload = base64.b64encode(image.read_bytes()).decode("ascii")
                segments.append({"type": "image",
                                 "data": {"file": f"base64://{payload}"}})
            except OSError as exc:
                print(f"  [qq] 帮助图片读取失败：{exc}")
        if len(segments) == 1:
            self.bot.send_private(self.owner, HELP)
            return
        try:
            self.bot.send_private(self.owner, segments)
        except Exception as exc:  # noqa: BLE001 —— 图片发不出去时退回纯文本
            print(f"  [qq] 帮助图片发送失败，退回纯文本："
                  f"{type(exc).__name__}: {exc}")
            self.bot.send_private(self.owner, HELP)

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
            reply = self.handle(text)
            if reply == HELP:
                self.reply_help()       # 文本 + 指令表图片，一条消息
            else:
                self.reply(reply)
        except Exception as exc:  # noqa: BLE001 —— 一条指令出错不能把 bot 带崩
            self.reply(f"❌ 执行出错：{type(exc).__name__}: {exc}")

    def handle(self, text: str) -> str:
        """解析一条指令，返回要回复的文字。"""
        parts = text.split()
        cmd, rest = parts[0].lower(), parts[1:]
        arg = rest[0] if rest else ""

        if cmd in ("帮助", "help", "菜单", "?", "？"):
            return HELP
        # /切换：在两个账号档位之间切换，后面的指令都作用于当前档位
        if cmd in ("切换", "switch"):
            return self.switch_cmd(rest)
        if cmd in PARENT_ALIASES:
            return ("爸妈版已经改成用「/切换」切账号了：先发 /切换，"
                    "再发 /投递、/配置、/状态 等。")
        if cmd in ("投递", "开始", "start"):
            return self.task_cmd(rest, live=True)
        if cmd in ("演练", "dry"):
            return self.task_cmd(rest, live=False)
        if cmd in ("搜索", "search"):
            return ("「/搜索」已经取消了：关键词用「/关键词 大模型算法 Agent」设定，"
                    "再发「/投递」。发「/配置」看当前设置。")
        if cmd in ("学历", "education", "edu"):
            return self.education_cmd(rest)
        if cmd in ("薪资", "日薪", "工资", "salary", "wage"):
            return self.wage_cmd(" ".join(rest))
        if cmd in ("配置", "设置", "config", "cfg"):
            return self.config_cmd(rest)
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
    #  爸妈版配置展示
    # ------------------------------------------------------------------
    def parents_config_text(self) -> str:
        """当前活跃档位是爸妈版时，`/配置` 显示的条件。"""
        try:
            cfg = self.profile_cfg("parents")
        except RuntimeError as exc:
            return f"❌ {exc}"
        education = preferences.education_from_config(cfg) or "不限"
        daily = float(cfg.get("job_filter.min_daily_wage", 0) or 0)
        monthly = float(cfg.get("job_filter.min_monthly_wage", 0) or 0)
        wages = []
        if daily > 0:
            wages.append(f"日薪中位 ≥{daily:g}")
        if monthly > 0:
            wages.append(f"月薪最低 ≥{monthly:g}")
        queries = kw.queries_of(kw.planned_sources(cfg))
        lines = [
            "👨‍👩‍👧 当前配置（爸妈版）",
            f"城市：{cfg.get('job_filter.require_city') or '不限'}",
            f"薪资：{' · '.join(wages) or '不限'}",
            f"学历：{education}",
            "岗位类型：排除实习" if cfg.get("job_filter.exclude_internship", False)
            else "岗位类型：不限",
            f"简历：{'需要' if cfg.get('resume.required', True) else '无简历模式'}",
            f"Edge profile：{cfg.user_data_dir.name}（端口 {cfg.get('browser.cdp_port')}）",
            f"数据目录：{cfg.data_dir}",
            f"关键词：{len(queries)} 个",
        ]
        if queries:
            lines.append(kw.block(queries))
        lines.append("")
        lines.append("直接发「/投递」（实发）或「/演练」（只筛不发）；"
                     "发「/切换」回到另一个账号。")
        return "\n".join(lines)

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
    #  学历 / 薪资 / 配置
    # ------------------------------------------------------------------
    def _prefs(self, cfg=None) -> preferences.Prefs:
        return preferences.load_prefs((cfg or self.cfg).qq_prefs_path)

    def _save_prefs(self, prefs: preferences.Prefs, cfg=None) -> None:
        preferences.save_prefs((cfg or self.cfg).qq_prefs_path, prefs)

    @staticmethod
    def prefs_help() -> str:
        return ("学历一次写一个档位（不限 / 大专 / 本科 / 硕士 / 博士），"
                "写「不限」= 不按学历筛；薪资写日薪 300，或 月薪8000、8k。"
                "设定存在 .qq_prefs.json，删掉它就全部回到 config.toml。")

    def education_cmd(self, rest: list[str]) -> str:
        """「/学历 [档位]」：不带参数 = 查看，带了 = 设定一个档位（作用于当前档位）。"""
        cfg = self.active_cfg()
        label = self.profile_label(self.active_profile)
        prefs = self._prefs(cfg)
        if not rest:
            return (f"🎓 {label}\n" + preferences.describe(prefs)
                    + "\n\n" + self.prefs_help())
        if len(rest) > 1:
            return ("❌ 一次只写一个学历档位，例如 /学历 硕士；"
                    "不限制写 /学历 不限。\n" + self.prefs_help())
        value, error = preferences.parse_education(rest[0])
        if value is None:
            return f"❌ {error}\n{self.prefs_help()}"
        prefs = prefs.with_education(value)
        self._save_prefs(prefs, cfg)
        degree = preferences.degree_param(value)
        url_line = (f"\nBOSS URL 参数：degree={degree}" if degree
                    else "\n（不带 degree 参数，不按学历筛）")
        return (f"✅ 已设定{label}学历：{value or '不限'}{url_line}\n\n"
                + preferences.describe(prefs))

    def wage_cmd(self, raw: str) -> str:
        """「/薪资 [日薪]」：不带参数 = 查看，带了 = 设定（作用于当前档位）。"""
        cfg = self.active_cfg()
        label = self.profile_label(self.active_profile)
        prefs = self._prefs(cfg)
        text = raw.strip()
        if not text:
            return (f"💰 {label}\n" + preferences.describe(prefs)
                    + "\n\n" + self.prefs_help())
        if preferences.is_clear(text):
            prefs = prefs.with_wage(0.0)
            self._save_prefs(prefs, cfg)
            return f"✅ 已取消{label}日薪限制\n\n" + preferences.describe(prefs)
        wage = preferences.parse_wage(text)
        problem = preferences.wage_problem(wage)
        if problem:
            return f"❌ {problem}\n{self.prefs_help()}"
        prefs = prefs.with_wage(wage)
        self._save_prefs(prefs, cfg)
        return (f"✅ 已设定{label}日薪下限：≥{wage:g} 元/天（区间取中位）\n\n"
                + preferences.describe(prefs))

    def task_cmd(self, rest: list[str], live: bool) -> str:
        """「/投递 [条数]」「/演练 [条数]」—— 只接受条数，作用于当前 /切换 到的账号。

        关键词、学历、薪资都是单独的指令（/关键词、/学历、/薪资）设定的，
        这样 /投递 的语义永远只有「发多少条」一件事。
        """
        rest = list(rest)
        limit = None
        if rest:
            if len(rest) > 1 or not rest[0].isdigit():
                return ("❌ 「/投递」只接受条数，例如 /投递 10。\n"
                        "关键词用 /关键词，学历用 /学历 硕士，薪资用 /薪资 300；"
                        "发 /配置 看当前设置。")
            limit = int(rest[0])
            if limit <= 0:
                return "❌ 条数要大于 0"
            if limit > preferences.MAX_LIMIT:
                return f"❌ 一次最多投 {preferences.MAX_LIMIT} 条"
        if not live and limit is None:
            limit = 5                 # 「/演练」默认 5 条
        try:
            return self.start(live=live, limit=limit)
        except RuntimeError as exc:
            return f"❌ {exc}"

    def config_cmd(self, rest: list[str] | None = None) -> str:
        """「/配置」查看；「/配置 清空」恢复当前账号的最初状态。"""
        rest = list(rest or [])
        if rest:
            if len(rest) == 1 and preferences.is_clear(rest[0]):
                return self.reset_cmd()
            return ("❌ 「/配置」只支持不带参数（查看）或「/配置 清空」"
                    "（恢复最开始的状态）")
        if self.active_profile == "parents":
            return self.parents_config_text()
        return self._config_text()

    def reset_cmd(self) -> str:
        """清掉当前账号 QQ 上设的关键词 / 学历 / 薪资，回到它的 config。"""
        cfg = self.active_cfg()
        cfg.qq_keywords_path.unlink(missing_ok=True)
        cfg.qq_prefs_path.unlink(missing_ok=True)
        label = self.profile_label(self.active_profile)
        text = (self.parents_config_text() if self.active_profile == "parents"
                else self._config_text())
        return f"✅ 已恢复「{label}」最开始的状态：关键词、学历、薪资都回到配置。\n\n" + text

    def _config_text(self) -> str:
        """当前真正生效的全部要求，一眼看全。"""
        active = self.active_cfg()
        cfg = active
        try:
            # bot 可能已经常驻好几天，配置文件改完要能立即反映出来；
            # 用当前账号那份配置的路径，--config 指定的账号不会串到默认配置。
            cfg = cfgmod.load(active.path)
        except (Exception, SystemExit):  # noqa: BLE001
            pass
        prefs = self._prefs(active)
        lines = [f"🔧 当前投递配置（{self.profile_label(self.active_profile)}）"]

        if prefs.education is None:
            education = preferences.education_from_config(cfg)
            edu_src = "config.toml"
        else:
            education = prefs.education
            edu_src = "QQ 设定"
        lines.append(f"🎓 学历要求：{education or '不限'}（{edu_src}）")
        degree = preferences.degree_param(education)
        if degree:
            lines.append(f"   BOSS URL：degree={degree}")

        if prefs.min_wage is None:
            wage = float(cfg.get("job_filter.min_daily_wage", 0) or 0)
            wage_src = "config.toml"
        else:
            wage = prefs.min_wage
            wage_src = "QQ 设定"
        wage_text = f"≥{wage:g} 元/天（区间取中位）" if wage > 0 else "不限"
        lines.append(f"💰 日薪下限：{wage_text}（{wage_src}）")

        pending = self.pending_keywords(active)
        plan = kw.planned_sources(cfg, pending)
        picked = kw.queries_of(plan)
        extra = [kw.label(kind, q) for kind, q in plan if kind != "search"]
        lines.append(f"🔎 检索关键词：{len(picked)} 个"
                     f"（{'QQ 设定' if pending else 'config.toml'}）")
        if picked:
            lines.append(kw.block(picked))
        else:
            lines.append("（没有关键词，只走推荐流）")
        if extra:
            lines.append("另外还会走：" + " → ".join(extra))

        conditions = ["只投实习" if cfg.get("job_filter.require_internship", True)
                      else "不限求职类型"]
        city = str(cfg.get("job_filter.require_city", "") or "")
        if city:
            conditions.append(f"城市 {city}")
        cap = cfg.get("limits.daily_cap")
        if cap:
            conditions.append(f"每日上限 {cap} 条")
        lines.append("🧭 其他硬条件（config.toml）：" + " · ".join(conditions))

        boss_filters = cfg.get("job_filter.boss_filters", []) or []
        pairs = [f"{p[0]}→{p[1]}" for p in boss_filters
                 if isinstance(p, (list, tuple)) and len(p) >= 2]
        if pairs:
            lines.append("🎛 平台筛选：" + "、".join(pairs))

        lines.append("")
        lines.append("注：学历由 BOSS 搜索地址的 degree 参数筛；BOSS 薪资档位只有月薪，"
                     "日薪下限由程序按卡片薪资精确比较。")
        lines.append("改条件：/学历 硕士 · /薪资 300 · /关键词 词1 词2")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  检索关键词
    # ------------------------------------------------------------------
    # 「/关键词 清空」等恢复 config 的写法
    _CLEAR_WORDS = ("清空", "清除", "默认", "重置", "恢复", "clear", "reset", "default")

    def pending_keywords(self, cfg=None) -> list[str]:
        """QQ 上设定、下次任务沿用的关键词；没设过就是空列表。"""
        path = (cfg or self.cfg).qq_keywords_path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return kw.normalize(text.splitlines())

    def _set_pending(self, words: list[str], cfg=None) -> None:
        path = (cfg or self.cfg).qq_keywords_path
        if not words:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(words) + "\n", encoding="utf-8")

    def keyword_cmd(self, raw: str) -> str:
        """「/关键词 [关键词...]」：不带参数 = 查看，带了 = 设定，清空 = 恢复 config。"""
        cfg = self.active_cfg()
        label = self.profile_label(self.active_profile)
        words = kw.split_keywords(raw)
        if not words:
            return self.plan_text()
        if words[0].lower() in self._CLEAR_WORDS:
            self._set_pending([], cfg)
            return (f"✅ 已清空{label} QQ 指定的关键词，之后的任务用 "
                    "config 的 [search].queries\n\n" + self.plan_text())
        self._set_pending(words, cfg)
        return (f"✅ 已设定{label} {len(words)} 个检索关键词，"
                "下次「/投递」「/演练」生效：\n"
                + kw.block(words)
                + "\n发「/关键词」查看，「/关键词 清空」恢复配置")

    def plan_text(self) -> str:
        """当前会搜哪些关键词 —— 「/关键词」的回复，也是任务开始推送的同一份编排。"""
        active = self.active_cfg()
        pending = self.pending_keywords(active)
        label = self.profile_label(self.active_profile)
        cfg = active
        try:
            # bot 可能已经常驻好几天，配置文件改完要能立即反映出来；
            # 读坏了（正在编辑、文件暂时不在）就退回启动时那份。
            cfg = cfgmod.load(active.path)
        except (Exception, SystemExit):  # noqa: BLE001
            pass
        plan = kw.planned_sources(cfg, pending)
        picked = kw.queries_of(plan)
        extra = [kw.label(kind, q) for kind, q in plan if kind != "search"]

        head = (f"🔎 {label}检索关键词（QQ 指定，发「/关键词 清空」恢复配置）"
                if pending else f"🔎 {label}检索关键词（来自配置）")
        lines = [head]
        if picked:
            lines.append(f"共 {len(picked)} 个，按优先级：")
            lines.append(kw.block(picked))
        else:
            lines.append("没有配置关键词，只走推荐流")
        if extra:
            lines.append("另外还会走：" + " → ".join(extra))
        lines.append(preferences.status_line(self._prefs(active)))
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
              prefs: preferences.Prefs | None = None,
              profile: str | None = None) -> str:
        key = self._profile_key(profile or self.active_profile)
        label = self.profile_label(key)
        if self._running():
            return f"已经有任务在跑：{self.task_desc}\n要换任务先发「/停止」"
        if self._external_running():
            return ("已经有一个任务在使用浏览器（不是从 QQ 启动的，比如 daily_greet.bat）。\n"
                    "这次不启动，免得两个任务抢同一个浏览器。要换任务先发「/停止」")

        # 两个账号各有一个 Edge profile，但同时只保留一个 Edge：
        # 启动当前档位前，先把另一个档位的浏览器关掉。
        other = "parents" if key == DEFAULT_PROFILE else DEFAULT_PROFILE
        try:
            self._close_browser(self.profile_cfg(other))
        except RuntimeError:
            pass

        task_cfg = self.profile_cfg(key)
        # 这次只搜哪些词：指令里带的 > 这个账号「/关键词」设定的 > 配置文件的
        words = kw.normalize(keywords) if keywords else self.pending_keywords(task_cfg)
        # 学历 / 日薪：None = 没设过（用配置文件的），"" / 0 = 明确不限
        prefs = self._prefs(task_cfg) if prefs is None else prefs

        # --config 必须放在子命令前面：run.py 的顶层参数。
        args = [sys.executable, str(self.run_py)]
        if key == "parents":
            args += ["--config", str(task_cfg.path)]
        args.append("greet")
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
            # 「不限」也要显式传：它要盖住配置文件里的学历
            args += ["--education", prefs.education or "不限"]
        if prefs.min_wage is not None:
            args += ["--min-wage", f"{prefs.min_wage:g}"]

        task_cfg.stop_flag.unlink(missing_ok=True)
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
        if key != DEFAULT_PROFILE:
            desc = f"{label}·{desc}"
        with self._lock:
            self.proc = proc
            self.task_desc = desc
            self.task_cfg = task_cfg
            self.task_profile = key
            self.task_started = time.time()
            self._stop_deadline = 0.0
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        print(f"  启动任务：{desc}（pid {proc.pid}）")

        warn = ("\n⚠️ 实发模式：会真实点「立即沟通」、消耗额度、触达 HR"
                if live else "")
        extra = ""
        if not prefs.is_empty():
            extra = "\n" + preferences.status_line(prefs)
        if key == "parents":
            tail = "\n爸妈版不逐条推送，结束后我会发总结"
        else:
            tail = "\n本次检索关键词马上推送给你\n每投出一条也会推送"
        return (f"▶️ 已启动：{desc}（pid {proc.pid}）{warn}{extra}{tail}")

    def stop(self) -> str:
        if self._external_running():
            # 外部任务不知道是哪个账号，两个停止标记都发；没有的那个不会有人看。
            self._touch_stop(self.cfg)
            try:
                self._touch_stop(self.profile_cfg("parents"))
            except RuntimeError:
                pass
            return ("⏹ 已发出停止指令。正在跑的任务不是从 QQ 启动的，"
                    "它会在当前这一条处理完后自己收尾（这种任务没法强制结束）")
        if not self._running():
            return "现在没有任务在跑"
        with self._lock:
            task_cfg = self.task_cfg
            profile = self.task_profile
        self._touch_stop(task_cfg)
        with self._lock:
            self._stop_deadline = time.time() + STOP_GRACE_SEC
        return (f"⏹ 已发出停止指令（{self.profile_label(profile)}），"
                f"当前这一条处理完就收尾（最多等 {STOP_GRACE_SEC}s，超时强制结束）")

    def _finish_text(self, cfg, profile: str, code: int) -> str:
        """爸妈版子进程没开 QQ 推送，任务结束时由 bot 补一条总结。"""
        names = {"sent": "已发送", "dry_run": "演练", "failed": "失败",
                 "filtered": "被过滤", "already": "已沟通过"}
        rows = self._query("SELECT status, COUNT(*) FROM greeted "
                           "WHERE substr(greeted_at,1,10)=? GROUP BY status",
                           (self._today(),), cfg=cfg)
        body = " · ".join(f"{names.get(s, s)} {n}" for s, n in rows) or "今日还没有动作"
        icon = "✅" if code == 0 else "⚠️"
        return f"{icon} {self.profile_label(profile)}任务结束（退出码 {code}）\n{body}"

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

        with self._lock:
            task_cfg = self.task_cfg
            profile = self.task_profile
            desc = self.task_desc
            if self.proc is proc:
                self.proc = None
        task_cfg.stop_flag.unlink(missing_ok=True)
        code = proc.returncode
        print(f"  任务结束：{desc}，退出码 {code}")
        # 正常结束 / 按规则停机时本人版投递进程自己已经推过总结了，
        # 这里只补它没机会说话的情况：被强杀、崩溃；爸妈版则统一补一条总结。
        if killed:
            self.notifier.send(
                f"⏹ {self.profile_label(profile)}任务没有按时收尾，已强制结束（{desc}）")
        elif code == EXIT_BUSY:
            self.notifier.send("⚠️ 没有启动：另一个任务刚好占用了浏览器，等它结束再发「/投递」")
        elif profile != DEFAULT_PROFILE:
            self.notifier.send(self._finish_text(task_cfg, profile, code))
        elif code not in (0, 1):
            self.notifier.send(f"❌ 任务异常退出（退出码 {code}，{desc}），发「/日志」看原因")

    # ------------------------------------------------------------------
    #  查询
    # ------------------------------------------------------------------
    def _query(self, sql: str, params: tuple = (), cfg=None) -> list[tuple]:
        db_path = (cfg or self.cfg).db_path
        if not db_path.exists():
            return []
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:     # 还没建表
            return []
        finally:
            conn.close()

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _sent_today(self, cfg=None) -> int:
        rows = self._query("SELECT COUNT(*) FROM greeted WHERE status='sent' "
                           "AND substr(greeted_at,1,10)=?", (self._today(),), cfg=cfg)
        return int(rows[0][0]) if rows else 0

    def status(self, profile: str | None = None) -> str:
        key = self._profile_key(profile or self.active_profile)
        cfg = self.profile_cfg(key)
        label = self.profile_label(key)
        running = self._running()
        if running:
            with self._lock:
                task_cfg = self.task_cfg
                task_profile = self.task_profile
                desc = self.task_desc
            cfg = task_cfg
            key = task_profile
            label = self.profile_label(key)
        cap = int(cfg.get("limits.daily_cap", 10))
        head = f"今日已投 {self._sent_today(cfg)} 条（本地上限 {cap}）"
        pending = self.pending_keywords(cfg)
        if pending:
            head += f"\n🔎 已设定 {len(pending)} 个关键词（发「/关键词」查看）"
        if key == "parents":
            education = preferences.education_from_config(cfg) or "不限"
            monthly = float(cfg.get("job_filter.min_monthly_wage", 0) or 0)
            wage = f"月薪最低 ≥{monthly:g}" if monthly > 0 else "薪资不限"
            head += f"\n🎓 学历 {education} · 💰 {wage}"
        else:
            head += "\n" + preferences.status_line(self._prefs(cfg))
        if self._external_running():
            return f"🏃 有任务在运行（不是从 QQ 启动的，比如 daily_greet.bat）\n{label}\n{head}"
        if not running:
            return f"💤 空闲\n{label}\n{head}"
        mins = (time.time() - self.task_started) / 60
        with self._lock:
            stopping = bool(self._stop_deadline)
            pid = self.proc.pid if self.proc else "?"
        state = "收尾中" if stopping else "运行中"
        return f"🏃 {state}：{desc}\n已运行 {mins:.0f} 分钟（pid {pid}）\n{label}\n{head}"

    def today(self, profile: str | None = None) -> str:
        cfg = self.profile_cfg(profile or self.active_profile)
        label = self.profile_label(profile or self.active_profile)
        rows = self._query(
            "SELECT title, company, salary, greeted_at FROM greeted "
            "WHERE status='sent' AND substr(greeted_at,1,10)=? ORDER BY greeted_at",
            (self._today(),), cfg=cfg)
        if not rows:
            return f"{label}今天还没有投递" if label != "本人版" else "今天还没有投递"
        lines = [f"📋 {label}今日已投 {len(rows)} 条" if label != "本人版"
                 else f"📋 今日已投 {len(rows)} 条"]
        for i, (title, company, salary, at) in enumerate(rows[-MAX_LINES:],
                                                         max(len(rows) - MAX_LINES, 0) + 1):
            lines.append(f"{i}. {at[11:16]} {title} @ {company}"
                         + (f"（{salary}）" if salary else ""))
        if len(rows) > MAX_LINES:
            lines.insert(1, f"（只列最近 {MAX_LINES} 条）")
        return "\n".join(lines)

    def stats(self, profile: str | None = None) -> str:
        cfg = self.profile_cfg(profile or self.active_profile)
        label = self.profile_label(profile or self.active_profile)
        names = {"sent": "已发送", "dry_run": "演练", "failed": "失败",
                 "filtered": "被过滤", "already": "已沟通过"}
        lines = [f"📊 {label}今日" if label != "本人版" else "📊 今日"]
        rows = self._query("SELECT status, COUNT(*) FROM greeted "
                           "WHERE substr(greeted_at,1,10)=? GROUP BY status",
                           (self._today(),), cfg=cfg)
        lines += [f"  {names.get(s, s)} {n}" for s, n in rows] or ["  还没有动作"]
        lines.append("最近 5 次运行")
        for started, greeted, failed, stopped in self._query(
                "SELECT started_at, greeted, failed, stopped_by FROM runs "
                "ORDER BY started_at DESC LIMIT 5", cfg=cfg):
            lines.append(f"  {(started or '')[5:16]} 发出 {greeted} 失败 {failed}"
                         f" {(stopped or '进行中/未收尾')[:30]}")
        return "\n".join(lines)

    def log_tail(self, n: int, profile: str | None = None) -> str:
        cfg = self.profile_cfg(profile or self.active_profile)
        logs = sorted(cfg.logs_dir.glob("greet_*.log"))
        if not logs:
            return f"{self.profile_label(profile or self.active_profile)}还没有运行日志"
        latest = logs[-1]
        text = latest.read_text(encoding="utf-8", errors="replace")
        lines = [ln.rstrip() for ln in text.splitlines()
                 if ln.strip() and not set(ln.strip()) <= set("─═")]
        return f"📄 {latest.name}\n" + "\n".join(lines[-n:])
