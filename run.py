#!/usr/bin/env python
"""auto_boss 命令行入口。

    python run.py doctor          # 自检：配置、API、浏览器、视觉链路
    python run.py login           # 扫码登录，登录态存进独立 profile
    python run.py greet           # 按 config.toml 的 dry_run 设置运行
    python run.py greet --live    # 强制实发（覆盖配置，会二次确认）
    python run.py greet --limit 3 # 本次最多发 3 条
    python run.py greet --auto    # 无人值守：实发、不确认、出错自动重启，投到平台额度用完
    python run.py stats           # 看历史记录
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
from autoboss import prompts
from autoboss.agent import GreetAgent
from autoboss.browser import BrowserSession
from autoboss.deepseek import DeepSeek, DeepSeekError
from autoboss.store import Store
from autoboss.vision import Vision


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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
    print(f"  关键词      {cfg.get('search.query')}")
    print(f"  城市        {cfg.get('search.city')}")
    print(f"  视觉模型    {cfg.get('deepseek.vision_model')}")
    print(f"  今日上限    {cfg.get('limits.daily_cap')}")
    print(f"  dry_run     {cfg.get('safety.dry_run')}")
    profile = cfg.section("profile")
    if not profile.get("highlights"):
        print("  ⚠  [profile].highlights 是空的 —— 招呼语会显得空洞，建议填 1~2 条硬成绩")
        ok = False
    else:
        print(f"  个人背景    {len(profile.get('highlights', []))} 条成绩，"
              f"{len(profile.get('skills', []))} 项技能")

    # 求职者画像：没有它就没法判断岗位能不能投，greet 会直接停机
    kb_file = Path(str(cfg.get("resume.kb_path", "resume/kb.md")))
    if kb_file.is_file() and kb_file.read_text(encoding="utf-8").strip():
        print(f"  求职画像    {kb_file}（{len(kb_file.read_text(encoding='utf-8'))} 字）")
    else:
        print(f"  ✗ 找不到求职者画像：{kb_file}")
        print("     它是判断岗位能不能投的唯一依据。把简历交给模型，让它总结出")
        print("     「哪些岗位族可以投、优先级如何」，存成这个 markdown 文件。")
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
_FINAL_KINDS = {"done", "quota", "captcha", "login", "stop", "interrupt"}


def cmd_greet(cfg, args):
    log_path = _start_log()
    print(f"[log] 本次输出同时写入 {log_path}")
    if args.auto:
        return _greet_auto(cfg, args)

    live = args.live
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

    run_id = new_run_id()
    client, store, vision, session = build(cfg, run_id)
    # 注意这里必须是 False 而不是 None：
    # GreetAgent 里 dry_run=None 表示"读配置"，而配置默认 dry_run=true，
    # 所以传 None 会让 --live 静默退化成演练模式。
    agent = GreetAgent(cfg, client, session, store, vision,
                       limit=args.limit, dry_run=(False if live else None))
    try:
        agent.run()
    finally:
        store.close()
    return 0


def _greet_auto(cfg, args, max_restarts: int = 5, wait_sec: float = 60.0):
    """无人值守：实发，出错就等一会儿重启，直到额度用完 / 来源走完 / 需要人工。

    重启是安全的：已发的、已沟通过的都在去重库里，不会重复打招呼；
    推荐流和搜索结果会从头扫，被过滤过的岗位只多花一次读屏。
    """
    restarts = 0
    while True:
        print(f"\n[auto] 第 {restarts + 1} 轮开始（{datetime.now():%H:%M:%S}）")
        cfg = cfgmod.load()        # 每轮重读配置，中途改了 config.toml 也能生效
        kind = "error"
        store = None
        try:
            run_id = new_run_id()
            client, store, vision, session = build(cfg, run_id)
            agent = GreetAgent(cfg, client, session, store, vision,
                               limit=args.limit, dry_run=False)
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

        if kind in _FINAL_KINDS:
            reason = {"done": "所有来源都走完了", "quota": "额度已满",
                      "captcha": "出现验证码，请人工处理后重跑",
                      "login": "需要扫码登录：先运行 python run.py login",
                      "stop": "按规则停机（见上方原因）",
                      "interrupt": "已中断"}[kind]
            print(f"\n[auto] 结束：{reason}")
            return 0 if kind in ("done", "quota") else 1

        restarts += 1
        if restarts > max_restarts:
            print(f"\n[auto] 连续出错 {max_restarts} 次，放弃。看日志排查。")
            return 1
        print(f"[auto] {wait_sec:.0f} 秒后自动重启（第 {restarts}/{max_restarts} 次）…")
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

    sub.add_parser("stats", help="查看历史记录")

    args = parser.parse_args()
    cfg = cfgmod.load()

    handlers = {
        "doctor": cmd_doctor,
        "login": cmd_login,
        "greet": cmd_greet,
        "stats": cmd_stats,
    }
    try:
        return handlers[args.cmd](cfg, args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
