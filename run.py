#!/usr/bin/env python
"""auto_boss 命令行入口。

    python run.py doctor          # 自检：配置、API、浏览器、视觉链路
    python run.py login           # 扫码登录，登录态存进独立 profile
    python run.py greet           # 按 config.toml 的 dry_run 设置运行
    python run.py greet --live    # 强制实发（覆盖配置，会二次确认）
    python run.py greet --limit 3 # 本次最多发 3 条
    python run.py greet --auto    # 无人值守：实发、不确认、出错自动重启，投到平台额度用完
    python run.py greet --dry     # 强制演练（覆盖配置，QQ「演练」指令用它）
    python run.py greet --keywords "大模型算法 Agent"  # 只搜这几个词（QQ 指令也用它）
    python run.py greet --education 硕士 --min-wage 300
                                # 学历（单个档位）/ 日薪下限（QQ 指令也用它）
    python run.py stats           # 看历史记录
    python run.py bot             # QQ 机器人：推送投递结果、接收大号的遥控指令
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# 允许从项目根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from autoboss import config as cfgmod
from autoboss import keywords as kwlib
from autoboss import preferences
from autoboss import prompts
from autoboss.agent import GreetAgent
from autoboss.browser import BrowserSession
from autoboss.deepseek import DeepSeek, DeepSeekError
from autoboss.lock import BrowserLock
from autoboss.qq import Notifier
from autoboss.store import Store
from autoboss.vision import Vision


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# 浏览器已被别的进程占用时的退出码。QQ bot 靠它区分「没启动」和「崩溃」。
EXIT_BUSY = 3


def with_browser_lock(fn):
    """包装要用浏览器的命令：同一时间只允许一个进程操作浏览器。"""
    def wrapper(cfg, args):
        lock = BrowserLock(cfg.lock_path)
        if not lock.acquire():
            print("✗ 已经有一个任务在使用浏览器（daily_greet.bat、命令行或 QQ 启动的投递，"
                  "或者 login / doctor）。\n  等它结束，或者在 QQ 上发「/停止」后再试。")
            return EXIT_BUSY
        try:
            # 拿到锁 = 没有别的投递在跑，残留的停止标记一定是过期的
            cfg.stop_flag.unlink(missing_ok=True)
            return fn(cfg, args)
        finally:
            cfg.stop_flag.unlink(missing_ok=True)
            lock.release()
    return wrapper


def build(cfg, run_id: str, verbose: bool = True):
    client = DeepSeek(
        api_key=cfg.api_key(),
        base_url=str(cfg.get("deepseek.base_url", "https://api.deepseek.com")),
        timeout=int(cfg.get("deepseek.timeout_sec", 120)),
    )
    store = Store(cfg.db_path, run_id)
    vision = Vision(client, cfg, store, cfg.shots_dir, run_id, verbose=verbose)
    session = BrowserSession(cfg)
    return client, store, vision, session


# ===========================================================================
#  doctor —— 上线前把整条链路验一遍
# ===========================================================================
def cmd_doctor(cfg, args):
    print("=" * 62)
    print("  auto_boss 自检")
    print("=" * 62)

    ok = True

    # 1) 配置
    print("\n[1/5] 配置")
    plan = kwlib.planned_sources(cfg)
    picked = kwlib.queries_of(plan)
    shown = "、".join(picked[:6]) + ("…" if len(picked) > 6 else "")
    print(f"  关键词      {len(picked)} 个：{shown or '（无）'}")
    print(f"  城市        {cfg.get('search.city')}")
    print(f"  学历要求    {preferences.education_from_config(cfg) or '不限'}")
    print(f"  日薪下限    {cfg.get('job_filter.min_daily_wage')}")
    print(f"  视觉模型    {cfg.get('deepseek.vision_model')}")
    print(f"  今日上限    {cfg.get('limits.daily_cap')}")
    print(f"  dry_run     {cfg.get('safety.dry_run')}")
    # 求职者画像：没有它就没法判断岗位能不能投，greet 会直接停机
    kb_file = Path(str(cfg.get("resume.kb_path", "resume/kb.md")))
    if kb_file.is_file() and kb_file.read_text(encoding="utf-8").strip():
        print(f"  求职画像    {kb_file}（{len(kb_file.read_text(encoding='utf-8'))} 字）")
    else:
        print(f"  ✗ 找不到求职者画像：{kb_file}")
        print("     它是判断岗位能不能投的唯一依据。照 README「生成求职者画像」一节，")
        print("     让 DeepSeek 读你的简历生成，存成这个文件（格式见 resume/kb.example.md）。")
        ok = False

    # 2) API key
    print("\n[2/5] DeepSeek API key")
    try:
        key = cfg.api_key()
        print(f"  已读取      {key[:6]}…{key[-4:]}（长度 {len(key)}）")
    except SystemExit as exc:
        print(f"  ✗ {exc}")
        return 1
    client = DeepSeek(key, str(cfg.get("deepseek.base_url", "https://api.deepseek.com")),
                      int(cfg.get("deepseek.timeout_sec", 120)))

    # 3) 文本调用
    print("\n[3/5] 文本模型")
    try:
        t0 = time.time()
        reply = client.chat("只回复两个字：正常", model=str(cfg.get("deepseek.text_model")),
                            max_tokens=16)
        print(f"  ✓ {client.call_count} 次调用成功，{time.time() - t0:.1f}s，回复「{reply.strip()[:20]}」")
    except DeepSeekError as exc:
        print(f"  ✗ {exc}")
        return 1

    # 4) 浏览器
    print("\n[4/5] 浏览器")
    session = BrowserSession(cfg)
    try:
        page = session.start()
        vw, vh = page.viewport()
        print(f"  ✓ 已连接，视口 {vw}×{vh}，登录态 {'有' if page.is_logged_in() else '无'}")
    except SystemExit as exc:
        print(f"  ✗ {exc}")
        return 1

    # 5) 视觉链路（最关键的一环）
    print("\n[5/5] 视觉链路（网格叠加 → 截图 → 模型定位）")
    store = Store(cfg.db_path, new_run_id())
    vision = Vision(client, cfg, store, cfg.shots_dir, "doctor")
    try:
        page.navigate(cfg.search_url(1), settle=4.5)
        kb = kb_file.read_text(encoding="utf-8").strip() if kb_file.is_file() else ""
        data = vision.look(page, prompts.list_user(kb), tag="doctor")
        state = data.get("page_state")
        jobs = data.get("jobs") or []
        print(f"  ✓ 页面判定    {state}")
        print(f"  ✓ 识别职位    {len(jobs)} 个")
        for job in jobs[:3]:
            btn = job.get("greet_click") or {}
            print(f"      · {job.get('title')} @ {job.get('company')} "
                  f"[{job.get('button_text')}] "
                  f"fit={job.get('fit')} "
                  f"btn=({btn.get('x')},{btn.get('y')}) conf={job.get('confidence')}")
        if state != "job_list":
            print(f"  ⚠  页面状态不是职位列表，请人工看一眼浏览器窗口。提示：{data.get('visible_hint')}")
            ok = False
        elif not jobs:
            print("  ⚠  没识别到职位，可能是关键词没结果，或页面还没加载完。")
            ok = False
        elif not any(j.get("greet_click") for j in jobs):
            print("  ⚠  识别到职位但都没有沟通按钮坐标 —— 视觉定位需要调，看截图确认。")
            ok = False
        print(f"  ✓ 截图存档    {vision.shots_dir}")
    except DeepSeekError as exc:
        print(f"  ✗ 视觉调用失败：{exc}")
        ok = False
    finally:
        store.close()
        session.stop()

    print("\n" + "=" * 62)
    print("  自检通过 ✓  可以跑 `python run.py greet`" if ok
          else "  自检有问题，先看上面 ⚠ 和 ✗ 的提示")
    print("=" * 62)
    return 0 if ok else 1


# ===========================================================================
#  login
# ===========================================================================
def cmd_login(cfg, args):
    print("启动浏览器，请在窗口里扫码登录 BOSS 直聘…\n")
    session = BrowserSession(cfg)
    page = session.start()
    page.navigate("https://www.zhipin.com/web/geek/job", settle=4.0)

    if page.is_logged_in():
        print("✓ 已经是登录状态，不用重复登录。")
        session.stop()
        return 0

    print("等待扫码（最多 5 分钟）…")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2.5)
        if page.is_logged_in():
            print(f"\n✓ 登录成功。登录态已存入 {cfg.user_data_dir}")
            print("  之后直接跑 `python run.py greet` 即可。")
            session.stop()
            return 0
    print("\n✗ 超时未检测到登录态。")
    session.stop()
    return 1


# ===========================================================================
#  greet
# ===========================================================================
class _Tee:
    """把输出同时写到控制台和日志文件 —— 无人值守时事后只能靠它复盘。"""

    def __init__(self, stream, path: Path):
        self._stream = stream
        self._fh = path.open("a", encoding="utf-8")

    def write(self, data):
        try:
            self._stream.write(data)
        except UnicodeEncodeError:
            self._stream.write(data.encode("gbk", "replace").decode("gbk"))
        self._fh.write(data)
        self._fh.flush()
        return len(data)

    def flush(self):
        self._stream.flush()
        self._fh.flush()


def _start_log() -> Path:
    logs = Path(__file__).resolve().parent / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / f"greet_{datetime.now():%Y%m%d_%H%M%S}.log"
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)
    return path


# 这些结束方式说明"今天到此为止"或"需要人来处理"，不该自动重启
_FINAL_KINDS = {"done", "quota", "dry", "captcha", "login", "stop", "interrupt"}

_KIND_TEXT = {"done": "所有来源都走完了", "quota": "额度已满",
              "dry": "连续好几个来源都没有可投的岗位，先停下，过段时间再投",
              "captcha": "出现验证码，请人工处理后重跑",
              "login": "需要扫码登录：先运行 python run.py login",
              "stop": "按规则停机（见日志）", "interrupt": "已中断",
              "error": "异常退出（见日志）"}


def _tail_of(path: Path | None, lines: int = 25, limit: int = 1500) -> str:
    """日志末尾几行，用来随错误通知一起发出去。"""
    if path is None:
        return ""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()[-limit:]
    except OSError:
        return ""


def _notify_end(notifier, cfg, kind: str, stats: dict, dry_run: bool,
                why: str = "", log_path: Path | None = None):
    """一次投递任务结束时推一条总结。--auto 中途重启不算结束。"""
    if not notifier.enabled:
        return
    import sqlite3
    conn = sqlite3.connect(str(cfg.db_path))
    try:
        today = conn.execute(
            "SELECT COUNT(*) FROM greeted WHERE status='sent' "
            "AND substr(greeted_at,1,10)=?", (datetime.now().strftime("%Y-%m-%d"),)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        today = 0
    finally:
        conn.close()
    icon = {"done": "✅", "quota": "✅", "dry": "⏸"}.get(kind, "⚠️")
    head = "演练" if dry_run else "投递"
    count = (f"演练 {stats.get('dry_run', 0)}" if dry_run
             else f"发出 {stats.get('sent', 0)}")
    msg = (f"{icon} {head}任务结束：{_KIND_TEXT.get(kind, kind)}\n"
           f"{count} · 失败 {stats.get('failed', 0)} · "
           f"过滤 {stats.get('filtered', 0)}\n今日累计已投 {today} 条"
           + (f"\n过滤原因：{why}" if why else ""))
    # 异常退出时把日志尾巴一起发过去：大号上直接能看原因。
    # 断网发不出去也没关系，Notifier 会存进 logs/qq_outbox，连上再补发。
    if kind == "error":
        tail = _tail_of(log_path)
        if tail:
            msg += "\n\n--- 最近日志 ---\n" + tail
    notifier.send(msg)


def _hard_filters_line(cfg, education: str | None = None,
                       min_wage: float | None = None) -> str:
    """任务开始推送里的「学历 / 日薪」这一行。

    None 表示这次没指定，回落到 config.toml 的值 —— 推给大号的就是**真正生效**的条件。
    """
    if education is None:
        education = preferences.education_from_config(cfg)
    if min_wage is None:
        min_wage = float(cfg.get("job_filter.min_daily_wage", 0) or 0)
    degree = preferences.degree_param(education)
    edu_text = education or "不限"
    if degree:
        edu_text += f"（degree={degree}）"
    wage_text = f"中位 ≥{min_wage:g} 元/天" if min_wage > 0 else "不限"
    return f"🎓 学历要求 {edu_text} · 💰 日薪 {wage_text}"


def _start_notice(cfg, keywords, dry_run: bool, limit: int | None,
                  education: str | None = None,
                  min_wage: float | None = None) -> str:
    """任务开始前推给 QQ 的文字：这次到底搜哪些关键词、按什么条件筛。

    以前只有结束通知，开始时不说话 —— 搜「大模型算法」两屏就停的那次，
    人在 QQ 上完全看不出它到底试过哪些词（日志里才有）。
    """
    plan = kwlib.planned_sources(cfg, keywords)
    picked = kwlib.queries_of(plan)
    extra = [kwlib.label(kind, q) for kind, q in plan if kind != "search"]

    head = ("▶️ 演练任务开始（只筛选，不发送）" if dry_run
            else "▶️ 投递任务开始（实发）")
    if limit:
        head += f"，本次最多 {limit} 条"
    lines = [head]
    if picked:
        src = "，来自 QQ 指令" if keywords else "（config.toml）"
        lines.append(f"🔎 本次检索关键词 {len(picked)} 个{src}：")
        lines.append(kwlib.block(picked))
    else:
        lines.append("🔎 没有配置检索关键词，只走推荐流")
    if extra:
        lines.append("另外还会走：" + " → ".join(extra))
    lines.append(_hard_filters_line(cfg, education, min_wage))
    return "\n".join(lines)


def _notify_start(notifier, cfg, keywords, dry_run: bool, limit: int | None,
                  education: str | None = None,
                  min_wage: float | None = None):
    text = _start_notice(cfg, keywords, dry_run, limit, education, min_wage)
    print("\n" + text + "\n")
    if notifier.enabled:
        notifier.send(text)


def _cli_education(raw) -> str | None:
    """`--education` 的解析。None = 这次没指定；「不限」规范成空串。"""
    if raw is None:
        return None
    value = preferences.normalize_education(raw)
    if value is None:
        raise SystemExit(
            f"看不懂 --education {raw}：可写 不限 / 大专 / 本科 / 硕士 / 博士"
            "（填档位，不是「本科及以上」这种岗位要求写法）")
    return value


def _cli_min_wage(raw) -> float | None:
    """`--min-wage` 的解析。None = 这次没指定；0 = 不限。"""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"看不懂 --min-wage {raw}：写日薪数字，比如 300") from None
    problem = preferences.wage_problem(value)
    if problem:
        raise SystemExit(problem)
    return value


def cmd_greet(cfg, args):
    log_path = _start_log()
    print(f"[log] 本次输出同时写入 {log_path}")

    # QQ 指令 / --keywords 指定的关键词：只搜这些，覆盖 config 的 [search].queries
    words = kwlib.split_keywords(getattr(args, "keywords", "") or "")
    # 学历 / 日薪：None = 用 config.toml；"" / 0 = 明确不限
    education = _cli_education(getattr(args, "education", None))
    min_wage = _cli_min_wage(getattr(args, "min_wage", None))
    notifier = Notifier(cfg)
    if args.auto:
        _notify_start(notifier, cfg, words, dry_run=False, limit=args.limit,
                      education=education, min_wage=min_wage)
        return _greet_auto(cfg, args, log_path, keywords=words,
                           education=education, min_wage=min_wage)

    live = args.live and not args.dry
    if live:
        print("\n" + "!" * 62)
        print("  实发模式：程序会真实点击「立即沟通」并发送消息。")
        print("  这会消耗平台配额（免费账号约 10 次/天）并真实触达 HR。")
        print("  平台用户协议禁止自动化操作，账号存在被限制或封禁的风险。")
        print("!" * 62)
        if not args.yes:
            ans = input("\n  确认继续？输入 yes 回车：").strip().lower()
            if ans != "yes":
                print("  已取消。")
                return 1

    # 这里必须显式算出 True/False，不能把 None 传下去：
    # GreetAgent 里 dry_run=None 表示"读配置"，而配置默认 dry_run=true，
    # 于是 --live 会静默退化成演练模式（以前踩过）。算一次，通知和 agent 共用。
    dry = True if args.dry else (False if live else bool(cfg.get("safety.dry_run", True)))
    # 开始通知放在 build() 之前：API key 配错、浏览器起不来时，
    # 大号也能先在 QQ 上看到"这次要搜哪些词"，再收到失败总结。
    _notify_start(notifier, cfg, words, dry_run=dry, limit=args.limit,
                  education=education, min_wage=min_wage)

    run_id = new_run_id()
    client, store, vision, session = build(cfg, run_id)
    agent = GreetAgent(cfg, client, session, store, vision,
                       limit=args.limit, dry_run=dry, notifier=notifier,
                       keywords=words, education=education, min_wage=min_wage)
    try:
        agent.run()
    finally:
        store.close()
        _notify_end(notifier, cfg, agent.stop_kind or "error", agent.stats, agent.dry_run,
                    agent.filter_summary(), log_path)
    return 0


def _greet_auto(cfg, args, log_path: Path | None = None,
                max_restarts: int = 5, wait_sec: float = 60.0,
                keywords: list[str] | None = None,
                education: str | None = None,
                min_wage: float | None = None):
    """无人值守：实发，出错就等一会儿重启，直到额度用完 / 来源走完 / 需要人工。

    重启是安全的：已发的、已沟通过的都在去重库里，不会重复打招呼；
    推荐流和搜索结果会从头扫，被过滤过的岗位只多花一次读屏。
    """
    restarts = 0
    totals = {"sent": 0, "failed": 0, "filtered": 0}
    limit = args.limit
    # 关键词在整个 --auto 任务里固定：每轮重建 agent 时都传同一份，
    # 中途改了 config.toml 的 queries 也只影响下一次任务（自动重启不算新任务）。
    words = (keywords if keywords is not None
             else kwlib.split_keywords(getattr(args, "keywords", "") or ""))
    # 学历 / 日薪同理：这是「这一次任务」的条件，自动重启沿用同一份。
    if education is None:
        education = _cli_education(getattr(args, "education", None))
    if min_wage is None:
        min_wage = _cli_min_wage(getattr(args, "min_wage", None))
    while True:
        print(f"\n[auto] 第 {restarts + 1} 轮开始（{datetime.now():%H:%M:%S}）")
        cfg = cfgmod.load()        # 每轮重读配置，中途改了 config.toml 也能生效
        notifier = Notifier(cfg)
        kind = "error"
        store = None
        agent = None
        try:
            run_id = new_run_id()
            client, store, vision, session = build(cfg, run_id)
            agent = GreetAgent(cfg, client, session, store, vision,
                               limit=limit, dry_run=False, notifier=notifier,
                               keywords=words, education=education, min_wage=min_wage)
            agent.run()
            kind = agent.stop_kind or "done"
        except KeyboardInterrupt:
            print("\n[auto] 已中断。")
            return 130
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            print(f"\n[auto] 这一轮异常退出：{type(exc).__name__}: {exc}")
        finally:
            if store is not None:
                store.close()
            if agent is not None:
                for k in totals:
                    totals[k] += agent.stats.get(k, 0)

        if kind in _FINAL_KINDS:
            print(f"\n[auto] 结束：{_KIND_TEXT[kind]}")
            _notify_end(notifier, cfg, kind, totals, False,
                        agent.filter_summary() if agent is not None else "", log_path)
            return 0 if kind in ("done", "quota", "dry") else 1

        # --limit 是整个任务的上限，重启后只投剩下的
        if args.limit is not None:
            limit = args.limit - totals["sent"]
            if limit <= 0:
                print(f"\n[auto] 结束：已投满 {args.limit} 条")
                _notify_end(notifier, cfg, "quota", totals, False, log_path=log_path)
                return 0

        restarts += 1
        if cfg.stop_flag.exists():
            print("\n[auto] 收到 QQ 停止指令，不再重启")
            _notify_end(notifier, cfg, "stop", totals, False, log_path=log_path)
            return 1
        if restarts > max_restarts:
            print(f"\n[auto] 连续出错 {max_restarts} 次，放弃。看日志排查。")
            _notify_end(notifier, cfg, "error", totals, False, log_path=log_path)
            return 1
        print(f"[auto] {wait_sec:.0f} 秒后自动重启（第 {restarts}/{max_restarts} 次）…")
        notifier.send(f"⚠️ 这一轮异常退出，{wait_sec:.0f} 秒后自动重启"
                      f"（第 {restarts}/{max_restarts} 次）")
        time.sleep(wait_sec)


# ===========================================================================
#  stats
# ===========================================================================
def cmd_stats(cfg, args):
    import sqlite3
    if not cfg.db_path.exists():
        print("还没有任何记录。")
        return 0

    conn = sqlite3.connect(str(cfg.db_path))
    print("=" * 62)
    print("  今日")
    print("=" * 62)
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM greeted WHERE substr(greeted_at,1,10)=? "
        "GROUP BY status", (today,)).fetchall()
    if not rows:
        print("  今天还没有动作。")
    for status, n in rows:
        print(f"  {status:<10} {n}")

    print("\n" + "=" * 62)
    print("  最近 5 次运行")
    print("=" * 62)
    for r in conn.execute(
            "SELECT run_id, started_at, greeted, failed, stopped_by FROM runs "
            "ORDER BY started_at DESC LIMIT 5"):
        print(f"  {r[1]}  发出 {r[2]}  失败 {r[3]}  {r[4]}")

    print("\n" + "=" * 62)
    print("  最近 10 条招呼语")
    print("=" * 62)
    for r in conn.execute(
            "SELECT title, company, status, message FROM greeted "
            "ORDER BY greeted_at DESC LIMIT 10"):
        print(f"  [{r[2]}] {r[0]} @ {r[1]}\n        {r[3] or '（无）'}")

    conn.close()
    return 0


# ===========================================================================
#  bot
# ===========================================================================
def cmd_bot(cfg, args):
    from autoboss.qqbot import QQBot
    bot = QQBot(cfg, Path(__file__).resolve())
    try:
        return bot.run()
    except Exception as exc:  # noqa: BLE001 —— 隐藏窗口里没人看得到栈，推到 QQ 上
        bot.report_crash(exc)
        return 1


# ===========================================================================
def main():
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="BOSS 直聘自动打招呼 agent（视觉 + CDP + DeepSeek）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="自检整条链路")

    sub.add_parser("login", help="扫码登录并保存登录态")

    p_greet = sub.add_parser("greet", help="开始打招呼")
    p_greet.add_argument("--live", action="store_true",
                         help="实发模式（覆盖 config 里的 dry_run）")
    p_greet.add_argument("--limit", type=int, default=None,
                         help="本次最多发几条")
    p_greet.add_argument("--yes", action="store_true", help="跳过实发的二次确认")
    p_greet.add_argument("--auto", action="store_true",
                         help="无人值守：实发、不确认、出错自动重启，直到平台额度用完")

    p_greet.add_argument("--dry", action="store_true",
                         help="强制演练（覆盖 config 里的 dry_run，优先于 --live）")
    p_greet.add_argument("--keywords", default="", metavar="关键词",
                         help="只搜这些关键词（空格 / 逗号 / 顿号分隔），"
                              "覆盖 config 的 [search].queries，且不再走推荐流")
    p_greet.add_argument("--education", default=None, metavar="学历",
                         help="只看这一个学历档位（不限 / 大专 / 本科 / 硕士 / 博士）；"
                              "覆盖 config 的 education")
    p_greet.add_argument("--min-wage", type=float, default=None, metavar="日薪",
                         help="日薪下限（元/天），0 = 不限；"
                              "覆盖 config 的 min_daily_wage")

    sub.add_parser("stats", help="查看历史记录")

    sub.add_parser("bot", help="QQ 机器人：推送投递结果，接收大号的遥控指令")

    args = parser.parse_args()
    cfg = cfgmod.load()

    handlers = {
        "doctor": with_browser_lock(cmd_doctor),
        "login": with_browser_lock(cmd_login),
        "greet": with_browser_lock(cmd_greet),
        "stats": cmd_stats,
        "bot": cmd_bot,
    }
    try:
        return handlers[args.cmd](cfg, args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
