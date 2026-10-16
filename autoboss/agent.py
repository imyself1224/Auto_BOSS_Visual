"""主控状态机：扫列表 → 生成招呼语 → 点沟通 → 补发定制话术 → 校验 → 记录。

整个流程每一步都靠**看截图**来决策，不读 DOM、不用 CSS 选择器。
这是刻意的取舍：

* BOSS 列表页的薪资金额是**字体加密**的，DOM 里拿到的是私有区乱码（U+E000–F8FF），
  而截图里显示的是正常数字。
* 「立即沟通」按钮会检查 `event.isTrusted`，JS 注入的 click 会被拒。
  我们走 CDP 的 Input.dispatchMouseEvent，产生的是真正的用户事件。
* 页面结构改版频繁，选择器一改就全废；视觉方案对此免疫。

代价是慢（每个职位 4~6 次模型调用）和贵。对一个每天只发 10 条的场景，可以接受。
"""
from __future__ import annotations

import random
import time
from pathlib import Path

from . import filters, humanize, overlay, prompts
from .deepseek import DeepSeekError
from .store import job_key

# 出现这些字样就说明该停机了 —— 要么配额用尽，要么触发了风控。
QUOTA_MARKERS = ("主动沟通数已达上限", "今日开聊次数已用完", "沟通次数已用完",
                 "今日沟通次数", "次数已用完", "已达上限", "沟通上限", "额度已用完",
                 "明天再来")
CAPTCHA_MARKERS = ("拖动滑块", "安全验证", "请完成验证", "滑动验证", "点击验证",
                   "security-check", "人机验证")


class StopRun(Exception):
    def __init__(self, reason: str, kind: str = "stop"):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind   # stop | quota | captcha | login | error


class GreetAgent:
    def __init__(self, cfg, client, session, store, vision, limit: int | None = None,
                 dry_run: bool | None = None, verbose: bool = True):
        self.cfg = cfg
        self.client = client
        self.session = session
        self.store = store
        self.vision = vision
        self.verbose = verbose
        self.page = None

        self.dry_run = bool(cfg.get("safety.dry_run", True)) if dry_run is None else dry_run
        # cap 是**当天**累计上限；limit 是**本次运行**最多发几条（--limit）。
        # 两者不能合成一个数：以前 cap = min(daily_cap, limit) 再减今天已发的，
        # 结果今天已发 5 条时 `--limit 5` 一条都不发就停机。
        self.cap = int(cfg.get("limits.daily_cap", 10))
        self.run_limit = limit
        self.min_interval = float(cfg.get("limits.min_interval_sec", 25))
        self.max_interval = float(cfg.get("limits.max_interval_sec", 60))
        self.send_custom = bool(cfg.get("greeting.send_custom_message", True))

        # 职位来源："recommend" 走搜索框旁的推荐流；"search" 走关键词搜索
        self.source_mode = str(cfg.get("source.mode", "recommend")).lower()
        self.base_url = cfg.search_url(1)
        self.feed_url = self.base_url        # 进入推荐流后会被更新成实际地址
        self._scroll_pos = 0                 # 推荐流是无限滚动，用它渐进下探
        self._bottom_hits = 0                # 连续几次滚不动了（翻到页脚）
        self._filtered_seen: set[str] = set()
        # 本轮已经动过的职位（不论成败）。
        #
        # 少了这个会死循环：某个职位能被选中、但打招呼永远失败（比如按钮定位不到），
        # 失败后 stalls 又被重置，于是它被反复选中，程序原地打转。
        # 记下来就能保证每一轮都在往前推进。
        self._attempted: set[str] = set()

        # BOSS 自带筛选器，形如 [("求职类型", "实习")]。见 _apply_boss_filters 的说明。
        self.boss_filters: list[tuple[str, str]] = [
            (str(p[0]), str(p[1])) for p in (cfg.get("job_filter.boss_filters", []) or [])
            if isinstance(p, (list, tuple)) and len(p) >= 2
        ]
        self._filters_applied = False
        # 最近一次按文字点开的筛选按钮外框（CSS 像素），关下拉时用
        self._filter_btn_box: tuple[float, float, float, float] | None = None
        # 筛选条件是否会随 URL 保留。点完筛选器后地址变了就说明会。
        self._filters_persist = False
        # 连续多少屏挑不出合适的职位才收工。推荐流是无限滚动，
        # 往下翻一段通常还能再出来一批，所以别翻两下就放弃。
        self.max_stalls = int(cfg.get("limits.max_empty_scans", 6))

        self.rules = filters.Rules(
            require_internship=bool(cfg.get("job_filter.require_internship", True)),
            min_daily_wage=float(cfg.get("job_filter.min_daily_wage", 300)),
            work_days_per_month=float(cfg.get("job_filter.work_days_per_month", 21.75)),
            allow_unparsable=bool(cfg.get("job_filter.allow_unparsable", False)),
            require_city=str(cfg.get("job_filter.require_city", "北京")),
            fit_scores=dict(cfg.get("job_filter.fit_scores", {}) or {}),
            prefer_districts=list(cfg.get("job_filter.prefer_districts", []) or []),
            prefer_district_bonus=float(
                cfg.get("job_filter.prefer_district_bonus", 1.0)),
        )

        # 求职者画像（外接知识库）—— 判断岗位「能不能投、先投谁」的唯一依据。
        # 它就是一个你自己维护的 markdown 文件，程序只读、从不改写。
        # 每扫一屏职位，这段文字会被原样拼进提示词。
        self.kb_path = Path(str(cfg.get("resume.kb_path", "resume/kb.md")))
        self.kb = (self.kb_path.read_text(encoding="utf-8").strip()
                   if self.kb_path.is_file() else "")

        self.stats = {"sent": 0, "dry_run": 0, "failed": 0, "already": 0, "filtered": 0}
        self.stop_kind = ""

    # ------------------------------------------------------------------
    def log(self, msg: str):
        if self.verbose:
            print(msg)

    def _text_model(self) -> str:
        return str(self.cfg.get("deepseek.text_model", "deepseek-flash"))

    # ------------------------------------------------------------------
    def run(self):
        """跑一轮。结束方式记在 `self.stop_kind`，供 `run.py greet --auto` 决定要不要重启：

        done（来源走完）/ quota（额度满）/ captcha / login / stop / interrupt —— 不重启；
        error（断线、模型调用失败、页面异常…）—— 可以重启接着投，去重库保证不重复发。
        """
        stopped_by = "正常结束"
        self.stop_kind = "done"
        try:
            self._require_kb()
            self.page = self.session.start()
            self._enforce_hours()
            self._ensure_login()
            self._loop()

        except StopRun as exc:
            self.stop_kind = exc.kind
            stopped_by = f"{exc.kind}: {exc.reason}"
            print(f"\n⏹  停机 —— {exc.reason}")
            self.store.log("stop", exc.reason)
        except KeyboardInterrupt:
            self.stop_kind = "interrupt"
            stopped_by = "用户中断"
            print("\n⏹  收到 Ctrl+C，正在收尾…")
        except DeepSeekError as exc:
            self.stop_kind = "error"
            stopped_by = f"error: {exc}"
            print(f"\n❌ DeepSeek 调用失败 —— {exc}")
            self.store.log("error", str(exc))
        except Exception as exc:  # noqa: BLE001 —— 兜底，保证 finally 里能落库
            self.stop_kind = "error"
            stopped_by = f"error: {type(exc).__name__}: {exc}"
            print(f"\n❌ 未预期错误 —— {type(exc).__name__}: {exc}")
            self.store.log("error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.store.finish(self.stats["sent"], self.stats["already"],
                              self.stats["failed"], stopped_by)
            self.session.stop()
            self._summary()

    # ------------------------------------------------------------------
    def _require_kb(self):
        """没有求职者画像就没法判断岗位能不能投 —— 在开浏览器之前就拦住。"""
        if self.kb:
            return
        raise StopRun(
            f"找不到求职者画像：{self.kb_path}\n"
            "  它是判断岗位能不能投的唯一依据（路径见 config.toml 的 [resume]）。\n"
            "  把你的简历交给模型，让它总结出「哪些岗位族可以投、优先级如何」，\n"
            "  存成这个 markdown 文件，然后重跑。",
            "stop",
        )

    # ------------------------------------------------------------------
    def _enforce_hours(self):
        lo, hi = (self.cfg.get("limits.active_hours", [0, 24]) + [0, 24])[:2]
        hour = time.localtime().tm_hour
        if not (int(lo) <= hour < int(hi)):
            raise StopRun(
                f"当前 {hour} 点不在允许的运行时段 [{lo}, {hi}) 内。"
                "凌晨批量操作是最容易被风控标记的行为之一。",
                "stop",
            )

    def _ensure_login(self):
        self.log("→ 打开职位列表页，检查登录状态…")
        self.page.navigate("https://www.zhipin.com/web/geek/job", settle=3.0)
        self.page = self.session.sync_page(self.page)

        if self.page.is_logged_in():
            self.log("✓ 已登录（检测到 wt2）")
            return

        print(
            "\n" + "=" * 62 +
            "\n  需要登录\n"
            "  请在刚打开的浏览器窗口里用 BOSS 直聘 App 扫码登录。\n"
            f"  登录态会保存在 {self.cfg.user_data_dir}，之后不用重复登录。\n"
            f"  等待中（最多 5 分钟）…\n" + "=" * 62
        )
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2.5)
            if self.page.is_logged_in():
                self.log("✓ 登录成功")
                humanize.pause(1.5, 2.5)
                self.page.navigate("https://www.zhipin.com/web/geek/job", settle=3.0)
                return
        raise StopRun("等待登录超时（5 分钟）", "login")

    # ------------------------------------------------------------------
    def _sources(self) -> list[tuple[str, str]]:
        """本轮要走的职位来源，按顺序：[("recommend", ""), ("search", "大模型 实习"), ...]。

        推荐流是有限的：加了「求职类型→实习」之后一个求职期望下只有二三十个岗位，
        翻到页脚就没了，离平台每天约 150 条的额度差得远。
        所以 mode = "both" 时推荐流走完接着按 [search].queries 逐个搜。
        """
        mode = self.source_mode
        queries = [str(q).strip() for q in (self.cfg.get("search.queries", []) or [])
                   if str(q).strip()]
        if not queries and str(self.cfg.get("search.query", "")).strip():
            queries = [str(self.cfg.get("search.query")).strip()]
        out: list[tuple[str, str]] = []
        if mode in ("recommend", "both"):
            out.append(("recommend", ""))
            # 推荐旁边的求职期望标签，如「算法工程师(北京)」—— 又是一个独立的岗位池
            out.extend(("expect", str(t).strip())
                       for t in (self.cfg.get("source.expect_tabs", []) or []) if str(t).strip())
        if mode in ("search", "both"):
            out.extend(("search", q) for q in queries)
        return out or [("recommend", "")]

    def _start_source(self, kind: str, query: str):
        """切到一个新来源：地址、滚动位置、筛选器状态都从头来。"""
        self.source_mode = kind
        self.base_url = self.cfg.search_url(1, query) if kind == "search" else self.cfg.search_url(1)
        self._expect_tab = query if kind == "expect" else ""
        self._source_ok = True
        self.feed_url = self.base_url
        self._scroll_pos = 0
        self._bottom_hits = 0
        self._filters_applied = False
        self._filters_persist = False
        self._filter_btn_box = None

    def _loop(self):
        budget = self.cap - self.store.greeted_today()
        if budget <= 0:
            raise StopRun(f"今日配额已用尽（上限 {self.cap} 条）", "quota")
        if self.run_limit is not None:
            budget = min(budget, int(self.run_limit))

        sources = self._sources()
        def label(k, q):
            return {"recommend": "推荐流", "expect": f"「{q}」标签"}.get(k, f"搜「{q}」")
        src = " → ".join(label(k, q) for k, q in sources)
        mode = "演练模式（不会真的发送）" if self.dry_run else "实发模式"

        lines = [
            "─" * 62,
            f"  职位来源：{src}",
            f"  硬性条件：{'实习' if self.rules.require_internship else '不限类型'}"
            f" · 日薪中位 ≥{self.rules.min_daily_wage:.0f}"
            f" · {self.rules.require_city}",
            f"  投递档位：{'、'.join(self.rules.fit_scores) or '—'}"
            f"（未列出的档位 = 不发）",
            f"  优先区域：{'、'.join(self.rules.prefer_districts) or '—'}",
            f"  求职画像：{self.kb_path}",
            f"  本次最多发 {budget} 条（今日上限 {self.cap}）"
            f"   间隔 {self.min_interval:.0f}–{self.max_interval:.0f}s",
            f"  {mode}",
            "─" * 62,
        ]
        if self.boss_filters:
            lines.insert(2, f"  平台筛选：{self._filters_desc()}")
        self.log("\n" + "\n".join(lines))

        for n, (kind, query) in enumerate(sources, 1):
            self._start_source(kind, query)
            if len(sources) > 1:
                self.log(f"\n══ 来源 {n}/{len(sources)}：{label(kind, query)} ══")
            self._enter_source()
            if not self._source_ok:
                continue
            self._scan_source(budget, f"list_p{n}")

        self.log("\n  所有来源都走完了")

    def _scan_source(self, budget: int, tag: str):
        """在当前来源里一屏屏往下扫，直到连续 max_stalls 屏没有可投的、或者翻到底。"""
        stalls = 0
        while stalls < self.max_stalls:
            if self.stats["sent"] + self.stats["dry_run"] >= budget:
                raise StopRun(f"本次任务配额已满（{budget} 条）", "quota")

            data = self._look(prompts.list_user(self.kb), tag)
            self._check_risk(data)

            state = data.get("page_state")
            if state in ("captcha", "login"):
                raise StopRun(f"页面状态异常：{state}",
                              "captcha" if state == "captcha" else "login")

            if state != "job_list":
                # 页面跑到别处去了（弹窗点错、误进聊天页、空占位…）。
                # 这时候读到的"卡片"一律不作数，重新进一次来源。
                stalls += 1
                self.log(f"  ! 页面状态「{state}」不是职位列表，重新进入来源")
                self._enter_source()
                continue

            jobs = data.get("jobs") or []
            targets = self._rank_targets(jobs) if jobs else []

            if targets:
                stalls = 0
                # ★ 先把这一屏能投的全部投完，再往下翻 ★
                # 每条 _greet_one 都会先点开目标卡片、再核对「立即沟通」按钮所属的
                # 职位标题，点偏了会被拦下来，所以不用每条之间重新读屏。
                for i, job in enumerate(targets):
                    if self.stats["sent"] + self.stats["dry_run"] >= budget:
                        raise StopRun(f"本次任务配额已满（{budget} 条）", "quota")
                    self._greet_one(job)
                    if i < len(targets) - 1:
                        humanize.human_delay(self.min_interval, self.max_interval)
            else:
                stalls += 1

            self._advance_screen()
            # 连着两次滚不动 = 翻到页脚了。以前这里还会对着同一屏再看好几次，
            # 每次都白花一次模型调用。
            if self._bottom_hits >= 2:
                self.log("  · 已翻到列表底部，这个来源走完了")
                return

    # ------------------------------------------------------------------
    def _click_norm(self, nx: float, ny: float):
        """归一化坐标 → 视口坐标 → 拟人化点击。"""
        vw, vh = self.page.viewport()
        cx, cy = overlay.norm_to_css(float(nx), float(ny), (vw, vh))
        humanize.move_and_click(self.page, cx, cy, (vw, vh))

    def _advance_screen(self) -> float:
        """把左侧职位列表往下翻一屏。返回真实位移像素。

        ★ 这里**只滚，不重进来源**。★
        以前每翻一屏都要 `_enter_source()` 重新导航一次推荐流地址 ——
        等于每投几条就把池子刷新一遍。推荐流是平台按次给的资源，重载既浪费名额，
        也会把这一屏还没投的岗位冲掉。往下翻本来就只是"滚动"，不需要重新加载页面。
        """
        # 每次只滚大半屏，让相邻两屏有重叠。
        # 以前固定滚 700–1000px，而一屏只能完整看到三张卡（每张约 180px）、
        # 底下那张半截的不读 —— 每翻一次就有一两张卡从没被看过，直接漏投。
        # 重叠部分的卡会被去重库 / _attempted / _filtered_seen 挡掉，不会重复处理。
        _, vh = self.page.viewport()
        moved = self._scroll_list(int(vh * random.uniform(0.5, 0.6)))
        self._scroll_pos += int(moved)
        # 滚了不到 1/10 屏也算到底（页脚露出来时只能再挪几十像素）
        self._bottom_hits = self._bottom_hits + 1 if moved < vh * 0.1 else 0
        if moved <= 0:
            self.log("  ! 滚轮没产生位移（已到底、手势被丢弃，或被右栏详情面板截走）")
        else:
            self.log(f"  ↓ 向下翻找新职位（滚了 {moved:.0f}px，累计 {self._scroll_pos}px）")
        return moved

    def _scroll_list(self, dy: int) -> float:
        """只在**左侧职位列表**上方派发滚轮，返回真实位移像素。

        必须显式指定 x，不能让它取默认值。`BrowserSession.scroll()` 默认在
        视口正中 (w/2, h/2) 派发，归一化正好是 (500, 500) —— 那个点落在
        **右栏职位详情面板里面**。右栏是独立子窗口、有自己的滚动条，滚轮打在那儿
        就只滚那篇职位描述，左边岗位列表纹丝不动，日志里却照样打印"已滚动 Npx"。

        返回的是 `window.scrollY` 的真实变化量：合成手势可能被丢弃（见 browser.scroll
        的说明），也可能被右栏截走，两种情况下返回 0，调用方据此判断有没有白滚。
        """
        vw, vh = self.page.viewport()
        # x 取 22% 视口宽：落在左栏卡片上。取 50% 就打进右栏详情面板了。
        return self.page.scroll(int(dy), x=vw * 0.22, y=vh * 0.6)

    def _filters_desc(self) -> str:
        return "、".join(f"{n}→{v}" for n, v in self.boss_filters) or "无"

    def _apply_boss_filters(self):
        """点 BOSS 自带的筛选器（默认是「求职类型 → 实习」）。

        这一步不能省。推荐流给的是跟"求职意向"匹配的岗位，实际绝大多数是全职岗；
        如果只靠卡片级过滤，结果就是把看到的一条条全滤掉，一条都发不出去。
        筛选器负责**把池子换对**，卡片级过滤负责**把个别漏网的挑出去**，
        两者是配合关系，不是二选一。
        """
        if not self.boss_filters:
            return

        for name, option in self.boss_filters:
            before_url = self.page.current_url()
            self.log(f"  · 设置筛选：{name} → {option}")

            opt = self._open_filter_and_find(name, option)
            if opt is None:
                continue

            self._click_norm(opt["x"], opt["y"])
            humanize.pause(1.6, 2.6)
            self._close_filter_menu(option)

            # 如果筛选条件进了 URL，就记下来 —— 之后直接导航到这个地址，
            # 不用每次都重新点一遍筛选器（每次能省下 2 次视觉调用）
            after_url = self.page.current_url()
            if after_url and after_url != before_url and after_url != self.feed_url:
                self.feed_url = after_url
                self._filters_persist = True
                self.log("    ✓ 已生效，且筛选条件进了 URL（后续直接复用该地址）")
            else:
                self.log(f"    ✓ 已选择「{option}」")

    # ------------------------------------------------------------------
    def _find_filter_button(self, name: str, hint: str = "") -> tuple[float, float] | None:
        """在筛选栏里按**文字**找到「name」这个按钮，返回它外框中心的归一化坐标。

        为什么不直接问"「求职类型」按钮的中心点在哪" ——
        那一排按钮挨得太近：「求职类型」「职位类型」「薪资待遇」的中心彼此只差
        半个按钮宽。模型报单点时是**自信地报错**：confidence 0.85~0.95，
        坐标却稳定地落在隔壁按钮上，光看返回值完全看不出异常。
        而"局部放大复核"是围着这个**错点**放大的，框里根本没有正确的那个按钮，
        所以复核也救不回来（实测连着两轮点成「薪资待遇」「职位类型」）。

        改成让它把**整排按钮连同外框**读出来，程序按文字精确匹配，歧义就消掉了：
        模型只需要"读得对"，不需要"点得准"。
        """
        data = self._look(prompts.filter_row(hint), "filter_row")
        raw = data.get("buttons") or []
        buttons = [b for b in raw if isinstance(b, dict)]

        # 精确匹配优先，其次前缀匹配（模型有时会把「求职类型·实习」整串抄进 text）
        for exact in (True, False):
            for b in buttons:
                text = str(b.get("text") or "").strip()
                if not text:
                    continue
                if (text == name) if exact else text.startswith(name):
                    point = self._box_center(b.get("box"))
                    if point:
                        value = b.get("current_value")
                        self.log(f"    · 筛选栏读到 {len(buttons)} 个按钮，"
                                 f"按文字定位到「{text}」"
                                 + (f"（当前已选 {value}）" if value else ""))
                        return point

        self.log(f"    ! 筛选栏里没读到「{name}」"
                 f"（读到：{'、'.join(str(b.get('text')) for b in buttons) or '空'}）")
        return None

    @staticmethod
    def _box_center(box) -> tuple[float, float] | None:
        """归一化外框 → 中心点。框缺失或退化就返回 None。"""
        if not isinstance(box, dict):
            return None
        try:
            x1, y1 = float(box["x1"]), float(box["y1"])
            x2, y2 = float(box["x2"]), float(box["y2"])
        except (KeyError, TypeError, ValueError):
            return None
        if x2 <= x1 or y2 <= y1:
            return None
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    def _open_filter_and_find(self, name: str, option: str) -> dict | None:
        """点开「name」的下拉、在里面找到「option」，返回它的坐标。失败返回 None。

        第一步先用 `_find_filter_button` 按文字把按钮定准；点完仍然
        **核对实际展开的是哪个下拉**（提示词让模型报 `open_menu_for`）——
        文字匹配对了也可能手滑点到边缘，这道校验留着当兜底，
        不对就 Esc 关掉、把"点错成了什么"回喂给下一次。

        在这之前先试 `_dom_filter_pick`：按页面文字直接取按钮外框。
        视觉那条路实测会把「求职类型」读对、框却往右偏到「薪资待遇」上，
        连续几轮都开错下拉；按文字取几何位置没有这个问题，而且不花模型调用。
        """
        opt = self._dom_filter_pick(name, option)
        if opt is not None:
            return opt

        hint = ""
        for attempt in range(2):
            point = self._find_filter_button(name, hint)
            if point is None:
                # 整排没读成（比如模型没照格式返回）—— 退回"单点定位 + 局部放大复核"
                tag = "filter_" + name + ("_retry" if attempt else "")
                btn = self.vision.locate(
                    self.page, prompts.filter_dropdown(name, hint), tag,
                    force_refine=True,
                    # 又宽又矮：把整条筛选栏都框进来。用默认的 ±70 正方形，
                    # 正确的那个按钮会被裁掉半边（实见「求职类型」只剩「类型」），
                    # 模型读不全文字就只能继续认错隔壁。
                    refine_pad=(200, 45))
                if btn is None:
                    self.log(f"    ! 没找到「{name}」筛选器，跳过（卡片级过滤仍会生效）")
                    return None
                point = (btn["x"], btn["y"])

            self._click_norm(*point)
            humanize.pause(1.0, 1.8)

            opt, opened = self._find_in_open_menu(name, option, attempt)
            if opt is not None:
                return opt

            if opened and name not in opened:
                self.log(f"    ! 点开的是「{opened}」不是「{name}」—— 关掉重来")
                hint = f"上一次点成了「{opened}」"
            else:
                self.log(f"    ! 下拉里没找到「{option}」"
                         f"（展开的是「{opened or '看不出来'}」）")
                hint = ""
            self.page.press_key("Escape")
            humanize.pause(0.5, 0.9)

        self.log(f"    ! 两次都没设成「{name} → {option}」，放弃（卡片级过滤仍会生效）")
        return None

    def _dom_filter_pick(self, name: str, option: str) -> dict | None:
        """按页面文字点开「name」，再在它正下方找「option」。返回选项的归一化坐标。

        只取几何位置，不碰页面逻辑：点击仍是 CDP 可信鼠标事件。
        找不到（按钮已选中后文字变成了选项值、改版换了文案、有歧义）就返回 None，
        交回视觉那条路处理。
        """
        buttons = self.page.find_text_boxes(name)
        if len(buttons) != 1:
            return None
        bx, by, bw, bh = buttons[0]
        bcx = bx + bw / 2
        self._filter_btn_box = buttons[0]
        vw, vh = self.page.viewport()
        humanize.move_and_click(self.page, bcx, by + bh / 2, (vw, vh))
        humanize.pause(1.0, 1.8)

        # 只认按钮正下方、水平离得不远的那个 —— 卡片标签里也可能有「实习」两个字
        opts = [b for b in self.page.find_text_boxes(option)
                if 0 <= b[1] - (by + bh) <= 400 and abs(b[0] + b[2] / 2 - bcx) <= 300]
        if not opts:
            self.log(f"    · 按页面文字点开了「{name}」，但下拉里没找到「{option}」，改用截图识别")
            self.page.press_key("Escape")
            humanize.pause(0.5, 0.9)
            return None

        ox, oy, ow, oh = min(opts, key=lambda b: (b[1], abs(b[0] + b[2] / 2 - bcx)))
        nx, ny = overlay.css_to_norm(ox + ow / 2, oy + oh / 2, (vw, vh))
        self.log(f"    · 按页面文字定位到「{name}」→「{option}」")
        return {"x": nx, "y": ny, "confidence": 1.0}

    def _close_filter_menu(self, option: str):
        """选完选项把下拉收起来。

        「求职类型」是多选下拉，勾上「实习」后**不会自己收起**，
        会一直盖着第一张职位卡 —— 那张卡读不全就被跳过，等于漏投。
        先 Esc、再把鼠标挪到页面左侧空白处（下拉是悬停展开的）；
        还开着就再点一下按钮本身把它收起来。
        """
        self.page.press_key("Escape")
        vw, vh = self.page.viewport()
        self.page.mouse_move(vw * 0.04, vh * 0.6)
        humanize.pause(0.6, 1.0)

        btn = self._filter_btn_box
        if btn is None:
            return
        bx, by, bw, bh = btn
        bcx = bx + bw / 2

        def still_open() -> bool:
            return any(0 <= b[1] - (by + bh) <= 400 and abs(b[0] + b[2] / 2 - bcx) <= 300
                       for b in self.page.find_text_boxes(option))

        if still_open():
            humanize.move_and_click(self.page, bcx, by + bh / 2, (vw, vh))
            self.page.mouse_move(vw * 0.04, vh * 0.6)
            humanize.pause(0.6, 1.0)
            if still_open():
                self.log("    ! 筛选下拉收不起来，第一张卡可能被挡住")

    def _find_in_open_menu(self, name: str, option: str, attempt: int):
        """在当前展开的下拉里找 option。

        返回 (坐标 dict 或 None, 模型报的"这个下拉属于哪个按钮")。

        第二个值**定位失败时也有** —— 判断"下拉开错了没有"全靠它，
        而"开错下拉"的典型症状正是选项定位失败，所以不能用 locate
        （它失败时直接返回 None，把线索丢了）。
        """
        tag = f"filter_opt_{option}_a{attempt + 1}"
        data, pick = self.vision.look_and_pick(
            self.page, prompts.filter_option(option), tag)
        if pick is None:
            # 实测下拉偶尔渲染得比截图慢，第一次看不到、第二次就有了。
            # 多等一会儿再问一次，比整轮重来便宜。
            humanize.pause(1.2, 2.0)
            data, pick = self.vision.look_and_pick(
                self.page, prompts.filter_option(option), tag + "b")

        opened = str((data or {}).get("open_menu_for") or "").strip()
        if opened and name not in opened:
            return None, opened          # 下拉开错了，找到的坐标也不可信

        if pick is None:
            seen = (data or {}).get("visible_options") or []
            if seen:
                self.log(f"      下拉里看到的是："
                         f"{'、'.join(str(x) for x in seen[:8])}")
            return None, opened

        return {"x": pick[0], "y": pick[1],
                "confidence": float((data or {}).get("confidence") or 0)}, opened

    # ------------------------------------------------------------------
    def _enter_source(self):
        """导航到职位来源；推荐模式下确保停在推荐流，并恢复滚动位置。

        推荐流是**页面状态**而不是 URL 状态，所以打完招呼从聊天页回来时不能
        直接回搜索结果页 —— 那样会掉出推荐流。这里的做法是：进入后先问一次
        「当前是不是推荐流」，不是就点一下「推荐」入口，
        并把点完之后**实际**的地址记下来供后续直接使用。
        """
        self.page = self.session.sync_page(self.page)
        # 推荐模式用记录下来的推荐流地址；关键词模式用当前页码对应的地址
        # （两者不能混用，否则翻页时会一直停在第一页）
        # 两种来源都用 feed_url：筛选条件进了 URL 后它会被更新成带筛选的地址。
        # 以前搜索模式导航的是 base_url，重进一次筛选就丢了，池子又变回全职岗。
        target = self.feed_url
        self.page.navigate(target, settle=random.uniform(2.8, 4.2))
        self.page = self.session.sync_page(self.page)

        if self.source_mode == "recommend":
            # 已经拿到推荐流专属地址时就不用再问模型了，直接用地址进
            if self.feed_url == self.base_url:
                data = self._look(prompts.RECOMMEND_ENTRY_USER, "recommend_entry")
                self._check_risk(data)
                if data.get("found") and not data.get("already_on_recommend"):
                    c = data.get("click") or {}
                    if c.get("x") is not None:
                        vw, vh = self.page.viewport()
                        cx, cy = overlay.norm_to_css(float(c["x"]), float(c["y"]), (vw, vh))
                        humanize.move_and_click(self.page, cx, cy, (vw, vh))
                        humanize.pause(1.8, 3.0)
                        self.page = self.session.sync_page(self.page)
                        url = self.page.current_url()
                        if url and url != self.feed_url:
                            self.feed_url = url
                            self.log(f"  · 已进入推荐流：{url[:78]}")

        if self.source_mode == "expect" and self.feed_url == self.base_url:
            if not self._click_expect_tab(self._expect_tab):
                self.log(f"  ! 页面上没找到「{self._expect_tab}」标签，跳过这个来源")
                self._source_ok = False
                return

        # 筛选器：只要还没点过（或点了但没进 URL），每次进来都要确保它是生效的。
        # 进了 URL 的情况只需第一次点，之后地址本身就带着筛选条件。
        if self.boss_filters and not (self._filters_applied and self._filters_persist):
            self._apply_boss_filters()
            self._filters_applied = True

        if self._scroll_pos:
            self._scroll_list(self._scroll_pos)
            humanize.pause(0.5, 1.0)

    def _click_expect_tab(self, tab: str) -> bool:
        """按页面文字点「推荐」旁边的求职期望标签（如「算法工程师(北京)」）。"""
        vw, vh = self.page.viewport()
        boxes = self.page.find_text_boxes(tab)
        if not boxes:
            # 「算法工程师」和「(北京)」可能是两个文字节点：按前半段找，只认左上角那一排
            head = tab.split("(")[0].split("（")[0]
            boxes = [b for b in self.page.find_text_boxes(head, prefix=True)
                     if b[1] < vh * 0.2 and b[0] < vw * 0.5]
        if not boxes:
            return False
        x, y, w, h = boxes[0]
        humanize.move_and_click(self.page, x + w / 2, y + h / 2, (vw, vh))
        humanize.pause(1.8, 3.0)
        self.page = self.session.sync_page(self.page)
        self.log(f"  · 已切到「{tab}」标签")
        return True

    # ------------------------------------------------------------------
    def _rank_targets(self, jobs: list[dict]) -> list[dict]:
        """硬条件过滤 + 软条件排序，返回**这一屏所有**可投的职位，分数高的在前。

        返回值从"单个"改成"列表"，是因为一屏里的合格岗位必须**连续投完再走**：
        以前每投一条就 _enter_source() 重进一次推荐流，屏幕上剩下那些已经判合格的
        岗位会跟着这次刷新一起消失，白扔平台名额、也白扔已经花掉的模型调用。
        """
        candidates: list[tuple[float, dict, filters.Verdict]] = []

        for job in jobs:
            title = (job.get("title") or "").strip()
            company = (job.get("company") or "").strip()
            if not title or not company:
                continue    # 读不全就不碰，宁可不发也不能误判去骚扰

            if self.store.already_greeted(title, company):
                continue

            if job_key(title, company) in self._attempted:
                continue    # 本轮已经试过了，无论成败都不再重复

            if job.get("greeted") is True:
                # 页面上显示「继续沟通」但本地没记录 —— 可能是你以前手动打过招呼
                self.store.record(title, company, job.get("salary", ""), "",
                                  "already", "页面显示已沟通，跳过")
                self.stats["already"] += 1
                self.log(f"  · 跳过（已沟通过）：{title} @ {company}")
                continue

            verdict = filters.evaluate(job, self.rules)
            if not verdict.ok:
                self._mark_filtered(job, verdict)
                continue

            candidates.append((verdict.score, job, verdict))

        if not candidates:
            return []

        candidates.sort(key=lambda t: t[0], reverse=True)
        self.log(f"  ↳ 这一屏有 {len(candidates)} 个可投，按优先度依次处理：")
        for _, job, verdict in candidates:
            self.log(f"    · {job.get('title')} @ {job.get('company')}  {verdict}")
        return [job for _, job, _ in candidates]

    def _mark_filtered(self, job: dict, verdict: filters.Verdict):
        """记下被过滤的职位。同一职位在本次运行里只提示一次，免得刷屏。"""
        key = job_key(job.get("title", ""), job.get("company", ""))
        if key in self._filtered_seen:
            return
        self._filtered_seen.add(key)
        self.stats["filtered"] += 1
        reason = verdict.reasons[0] if verdict.reasons else "不符合条件"
        self.log(f"  ✗ 过滤：{job.get('title')} @ {job.get('company')} —— {reason}")
        self.store.record(job.get("title", ""), job.get("company", ""),
                          job.get("salary", ""), "", "filtered", reason[:200])

    # ------------------------------------------------------------------
    def _greet_one(self, job: dict):
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        salary = job.get("salary") or ""
        self._attempted.add(job_key(title, company))
        self.log(f"\n  ▸ {title}  @ {company}  {salary}")

        # 只有在需要补发定制话术时才去调文本模型。
        # 关掉之后整个流程不碰聊天页 —— 不生成、不输入、不校验，
        # 既省调用，也少一次"替用户多说话"的机会。
        message = ""
        if self.send_custom:
            composed = self._compose_greeting(job)
            if composed is None:
                self.stats["failed"] += 1
                self.store.record(title, company, salary, "", "failed", "招呼语生成失败")
                return
            message = composed

        # 演练模式到此为止。绝不点「立即沟通」——
        # 那一下本身就会消耗配额并真实触达 HR，演练不该产生任何外部影响。
        if self.dry_run:
            self.stats["dry_run"] += 1
            note = "演练模式，未发送" if not message else f"演练模式，未发送｜拟发：{message}"
            self.store.record(title, company, salary, message, "dry_run", note)
            return

        try:
            # 定位不到按钮只是这一个职位的事，跳过继续，不要终止整轮 ——
            # 之前这里抛 StopRun，导致一个职位识别失败就整个程序收工。
            if not self._click_greet(job):
                self.stats["failed"] += 1
                self.store.record(title, company, salary, message, "failed",
                                  "未能确认「立即沟通」按钮（找不到，或按钮不属于该职位）")
                self.log("    ✗ 没能确认沟通按钮，跳过这个职位")
                return

            self._handle_popup()

            # 补发定制话术（默认关闭）。开启时会进聊天页再发一条。
            if self.send_custom and not self._send_custom(message, company):
                self.stats["failed"] += 1
                self.store.record(title, company, salary, message, "failed",
                                  "自定义话术未确认发出")
                return

            self.stats["sent"] += 1
            self.store.record(title, company, salary, message, "sent")
            total = self.store.greeted_today()
            self.log(f"    ✓ 已打招呼（今日累计 {total}/{self.cap}）")

        except StopRun:
            raise
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            self.store.record(title, company, salary, message, "failed", str(exc)[:200])
            self.log(f"    ✗ 失败：{exc}")

    # ------------------------------------------------------------------
    def _compose_greeting(self, job: dict) -> str | None:
        """用文本模型生成招呼语。"""
        profile = self.cfg.section("profile")
        prompt = prompts.greeting(
            profile, job,
            str(self.cfg.get("greeting.style", "简洁专业")),
            int(self.cfg.get("greeting.max_chars", 90)),
        )
        try:
            text = self.client.chat(
                prompt,
                model=self._text_model(),
                max_tokens=400,
                temperature=1.3,   # 调高一点，避免每个职位都是同一个句式
                thinking=bool(self.cfg.get("deepseek.thinking", False)),
            )
        except DeepSeekError as exc:
            self.log(f"    ! 生成失败：{exc}")
            return None

        return self._sanitize(text)

    @staticmethod
    def _sanitize(text: str) -> str:
        """清掉模型偶尔带出来的引号、前缀和换行。"""
        t = (text or "").strip()
        # 代码围栏：开头和结尾都要去，只去掉一半会把 ``` 留在消息里发出去
        if t.startswith("```"):
            t = t.split("\n", 1)[-1].strip()
        if t.endswith("```"):
            t = t[:-3].strip()
        # 去掉整句包裹的引号
        if len(t) >= 2 and t[0] in "\"'“「" and t[-1] in "\"'”」":
            t = t[1:-1].strip()
        # 去掉 "招呼语：" 这类前缀
        for prefix in ("招呼语：", "招呼语:", "消息：", "消息:", "回复：", "回复:"):
            if t.startswith(prefix):
                t = t[len(prefix):].strip()
        t = t.replace("\n", " ").strip()
        return t

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_title(text: str) -> str:
        t = "".join((text or "").split())
        for junk in ("...", "…"):
            t = t.replace(junk, "")
        return t

    @classmethod
    def _same_job_title(cls, a: str, b: str) -> bool:
        """判断两个职位标题是不是同一个。

        列表里的标题常被截断成「某某工程师（S...」，所以只比较靠前的部分。
        任一边为空时返回 True —— 信息不足就不拦，避免把能发的也挡掉。
        """
        na, nb = cls._normalize_title(a), cls._normalize_title(b)
        if not na or not nb:
            return True
        k = min(len(na), len(nb), 8)
        return na[:k] == nb[:k]

    def _open_job_detail(self, job: dict, retry: bool = False):
        """点开这个职位的卡片，让右侧详情面板切到它。

        这一步**不能省**。推荐流是「左卡片列表 + 右详情面板」的布局，
        而我们是按分数挑的职位，右侧面板默认显示的却可能是另一个卡片的详情。
        不先切过去，屏幕上那个「立即沟通」按钮就属于别的职位 ——
        我们会给一个没被筛过的职位打招呼。

        ★ 为什么点"框的中心"而不是模型报的点 ★
        实测让模型直接报中心点时，它会贴着卡片**右边缘**报：卡片框是 x[127,372]，
        它报 370 —— 点下去正好落在卡片边界上，**什么都不会发生**（不报错、不选中、
        没有选中高亮），右侧面板还停在空占位，于是「立即沟通」找不到，
        一条完全匹配的岗位被记成 failed 丢掉（虾皮「AI Agent算法工程师」那条就是这么没的）。
        现在改成让模型报卡片外框，中心在 `_card_point` 里算。

        `retry=True` 是重试路径：不再用扫描时记下的坐标（那次扫描已经过去几秒，
        列表可能已经动过），改成让模型**按职位名重新找一次卡片**。
        """
        dom = self._dom_card_point(job)
        if dom is not None:
            vw, vh = self.page.viewport()
            humanize.move_and_click(self.page, dom[0], dom[1], (vw, vh))
            humanize.pause(0.8, 1.6)
            return

        point = self._card_point(job)

        if retry:
            found = self.vision.locate(
                self.page,
                prompts.find_target(
                    f"职位「{job.get('title')}」的职位卡片",
                    "它在**左侧**的职位卡片列表里。请报**这张卡片本身**的中心坐标"
                    "（卡片上第一行左边是职位标题）。不要报右侧详情面板里的内容，"
                    "也不要报列表上别的那几张卡。",
                ),
                "refind_card",
            )
            if found is not None:
                point = (found["x"], found["y"])

        if point is None:
            # 读不到卡片位置就跳过"切详情"这一步，直接去点沟通按钮 ——
            # 按钮所属职位的核对（在 _click_greet 里）仍然是最后一道闸。
            return

        self._click_norm(*point)
        humanize.pause(0.8, 1.6)

    def _dom_card_point(self, job: dict) -> tuple[float, float] | None:
        """按页面文字在**左栏**找这张卡的标题，返回它的 CSS 中心点。找不到返回 None。

        模型给的卡片框会串到隔壁：实测同一屏两张「百度在线」的卡，
        点成了「财务BP实习生」，右栏切错，目标岗位就此被跳过。
        标题按前缀匹配（列表里的标题常被截断成「…（S...」）；
        有好几张同名的（「算法工程师」很常见），再看标题正下方的公司名来挑。

        模型抄标题时常把全角括号写成半角（「(27实习转正)」vs 页面上的「（27实习转正）」），
        前缀就对不上了。所以按这个顺序退：原样 → 括号全半角互换 → 只取括号前那段；
        最后一档太宽泛（「算法工程师」能命中好几张），**必须**公司名也对上才算。
        """
        title = self._normalize_title(job.get("title") or "")
        company = str(job.get("company") or "").strip()
        if not title:
            return None
        vw, _ = self.page.viewport()

        def in_left(text):
            return [b for b in self.page.find_text_boxes(text, prefix=True) if b[0] < vw * 0.5]

        comps_cache: list = []

        def by_company(boxes):
            if not company:
                return []
            if not comps_cache:
                comps_cache.append(in_left(company))
            return [b for b in boxes if any(0 < c[1] - b[1] <= 200 for c in comps_cache[0])]

        to_full = str.maketrans("()", "（）")
        to_half = str.maketrans("（）", "()")
        variants = list(dict.fromkeys([title, title.translate(to_full), title.translate(to_half)]))
        for variant in variants:
            left = in_left(variant)
            if len(left) > 1:
                left = by_company(left)
            if len(left) == 1:
                x, y, w, h = left[0]
                return (x + w / 2, y + h / 2)

        head = title.split("(")[0].split("（")[0].strip()
        if len(head) >= 2 and head != title:
            left = by_company(in_left(head))
            if len(left) == 1:
                x, y, w, h = left[0]
                return (x + w / 2, y + h / 2)
        return None

    def _card_point(self, job: dict) -> tuple[float, float] | None:
        """卡片该点哪个点：优先用 card_box 的中心，退回旧的 card_click。"""
        point = self._box_center(job.get("card_box"))
        if point is not None:
            return point

        legacy = job.get("card_click") or {}
        if isinstance(legacy, dict) and legacy.get("x") is not None:
            return (float(legacy["x"]), float(legacy["y"]))
        return None

    def _click_greet(self, job: dict) -> bool:
        """点「立即沟通」，并且确认点的是目标职位。

        定位不到、或按钮不属于目标职位时**重来一次**：先按职位名重找卡片、再重找按钮。
        一次失败就放弃的代价太大 —— 卡片点偏是**静默**的（不报错、不选中），
        而重新扫一屏要好几次模型调用。
        """
        title = job.get("title") or ""

        # 上一个职位的「已向BOSS发送消息」弹窗若还盖着，这一个的按钮就看不见。
        # 按文字查一下，不花模型调用。
        if not self.send_custom and self._dom_stay_on_page() == "closed":
            self.log("    · 清掉了残留的发送成功弹窗")

        for attempt in range(2):
            # 1) 先把详情面板切到目标职位（第二次尝试会按职位名重找卡片）
            self._open_job_detail(job, retry=bool(attempt))

            # 2) 定位沟通按钮（提示词里只认「立即沟通」这一个文案）
            found = self.vision.locate(self.page, prompts.GREET_BUTTON_USER, "greet_btn")
            if found is None:
                self.log("    ! 没找到「立即沟通」按钮"
                         + ("，重找一次卡片再试" if attempt == 0 else "，放弃这个职位"))
                continue

            # 3) 核对按钮所属职位 —— 这是防止打错人的最后一道闸
            context = str((found.get("raw") or {}).get("context_title") or "")
            if context and not self._same_job_title(context, title):
                self.log(f"    ! 按钮所属「{context}」与目标「{title}」不符"
                         + ("，重找一次卡片再试" if attempt == 0 else "，跳过以免打错人"))
                continue

            # 点之前先记下页面上已有的额度字样，点完只看**新冒出来**的 ——
            # 职位描述里偶尔也会出现「上限」之类的词，不能一见就停。
            before = set(self.page.text_markers(QUOTA_MARKERS))
            self._click_norm(found["x"], found["y"])
            humanize.pause(1.2, 2.4)
            self.page = self.session.sync_page(self.page)
            hit = [m for m in self.page.text_markers(QUOTA_MARKERS) if m not in before]
            if hit:
                # 用户也会在手机上投，本地计数看不到那部分 —— 真正的上限只能听平台的。
                raise StopRun(f"平台提示今日额度已用完（页面出现「{hit[0]}」）", "quota")
            return True

        return False

    def _handle_popup(self):
        """处理点完「立即沟通」后弹出的提示框。

        ★ 这里选错按钮的代价很大 ★

        实测那个弹窗长这样：
            标题「已向BOSS发送消息」
            按钮「留在此页」  「继续沟通」
        两个按钮都能让弹窗消失，但**「继续沟通」会把你带进聊天页**。
        本程序默认只打招呼、不对话，所以必须点「留在此页」。

        之前用关键词匹配（"继续"、"沟通"）挑按钮，恰好选中了后者 ——
        于是每打完一次招呼就闯进聊天页，这正是要避免的行为。
        现在改成让模型直接标注每个按钮会不会离开列表页，按模式来选。

        只打招呼时先按页面文字点「留在此页」并核对弹窗真的关了：
        实测模型报的按钮坐标偏上（报 y=559，按钮实际在 562–600），
        点空了弹窗还盖着，后面每个职位都找不到「立即沟通」。
        """
        if not self.send_custom:
            state = self._dom_stay_on_page()
            if state == "closed":
                self.log("    · 弹窗：点「留在此页」，已关闭")
                return
            if state == "stuck":
                self.log("    ! 弹窗点了两次 + Esc 还在，交给截图识别再试")

        data = self._look(prompts.POPUP_USER, "popup")
        self._check_risk(data)

        if not data.get("popup_visible"):
            return

        # 只打招呼 → 优先"留在本页"；要补发话术 → 优先"进聊天页"
        pref, alt = ("chat_click", "stay_click") if self.send_custom \
            else ("stay_click", "chat_click")
        target = data.get(pref) or data.get(alt)

        if isinstance(target, dict) and target.get("x") is not None:
            self.log(f"    · 弹窗：{data.get('popup_title') or '提示'}"
                     f" → 点{'留在列表页' if pref == 'stay_click' else '进聊天页'}")
            self._click_norm(float(target["x"]), float(target["y"]))
            humanize.pause(1.2, 2.2)
            self.page = self.session.sync_page(self.page)
            return

        # 模型没给汇总字段 → 退回按钮级判断
        buttons = data.get("buttons") or []
        if not buttons:
            self.log("    · 有弹窗但没识别到按钮，跳过")
            return

        want_leave = self.send_custom
        pick = next((b for b in buttons if b.get("leaves_page") == want_leave), None)
        if pick is None:
            pick = next((b for b in buttons if b.get("primary")), None)
        if pick is None and not want_leave:
            stay_words = ("留在此页", "留在本页", "关闭", "知道了", "完成", "取消", "好的")
            pick = next((b for b in buttons
                         if any(w in (b.get("text") or "") for w in stay_words)), None)
        if pick is None:
            pick = buttons[0]

        c = pick.get("click") or {}
        if c.get("x") is None:
            self.log("    · 弹窗按钮坐标无效，跳过")
            return

        self.log(f"    · 弹窗：点「{pick.get('text')}」")
        self._click_norm(float(c["x"]), float(c["y"]))
        humanize.pause(1.2, 2.2)
        self.page = self.session.sync_page(self.page)

    _STAY_TEXTS = ("留在此页", "留在本页")

    def _dom_stay_on_page(self) -> str:
        """按页面文字点「留在此页」，直到它从页面上消失。

        返回 "absent"（页面上本来就没有）/ "closed"（点掉了）/ "stuck"（点不掉）。
        """
        def boxes():
            return [b for t in self._STAY_TEXTS for b in self.page.find_text_boxes(t)]

        found = boxes()
        if not found:
            return "absent"
        vw, vh = self.page.viewport()
        for _ in range(2):
            x, y, w, h = found[0]
            humanize.move_and_click(self.page, x + w / 2, y + h / 2, (vw, vh))
            humanize.pause(1.0, 1.8)
            found = boxes()
            if not found:
                self.page = self.session.sync_page(self.page)
                return "closed"
        self.page.press_key("Escape")
        humanize.pause(0.6, 1.0)
        return "stuck" if boxes() else "closed"

    # ------------------------------------------------------------------
    def _send_custom(self, message: str, company: str) -> bool:
        """在聊天页把 AI 生成的定制话术补发出去。

        必须补发的原因：平台强制先发它自己的默认招呼语，且**不允许**把自定义内容
        设为默认招呼语。所以想在第一条消息里体现个性化，只能在系统招呼语之后追加。
        """
        data = self._look(prompts.CHAT_USER, "chat")
        self._check_risk(data)

        if not data.get("chat_open"):
            self.log("    · 当前不在聊天页，去会话列表找…")
            if not self._open_recent_conversation():
                return False
            data = self._look(prompts.CHAT_USER, "chat2")
            self._check_risk(data)
            if not data.get("chat_open"):
                return False

        # 输入框很小、位置又靠屏幕底部，点空的代价高（可能误点到别处）。
        # 所以 CHAT_USER 给的坐标信心不足时，单独定位一次 ——
        # Vision.locate 内部会在信心低时自动做局部放大复核，不用再手动加一轮。
        box = data.get("input_box") or {}
        if box.get("x") is None or float(data.get("confidence") or 0) < 0.7:
            found = self.vision.locate(
                self.page,
                prompts.find_target("聊天窗口底部那个可以打字的消息输入框",
                                    "注意不要选成页面顶部的搜索框。"),
                "chat_input",
            )
            if found is None:
                return False
            box = {"x": found["x"], "y": found["y"]}

        vw, vh = self.page.viewport()
        cx, cy = overlay.norm_to_css(float(box["x"]), float(box["y"]), (vw, vh))
        humanize.move_and_click(self.page, cx, cy, (vw, vh))
        humanize.pause(0.5, 1.1)

        self.page.type_text(message)
        humanize.pause(0.7, 1.6)

        send = data.get("send_button") or {}
        if send.get("x") is not None:
            sx, sy = overlay.norm_to_css(float(send["x"]), float(send["y"]), (vw, vh))
            humanize.move_and_click(self.page, sx, sy, (vw, vh))
        else:
            self.page.press_key("Enter")
        humanize.pause(1.5, 2.6)

        # 核对是否真的发出去了 —— 不核对就可能在"以为发了"的状态下白白消耗配额
        try:
            check = self._look(prompts.verify_message(message), "verify")
        except DeepSeekError as exc:
            self.log(f"    ! 发送校验失败（{exc}），按未确认处理")
            return False

        if check.get("sent"):
            return True

        note = check.get("note") or "未在对话中找到该消息"
        self.log(f"    ! 未确认发出：{note}（输入框为空={check.get('input_box_empty')}）")
        return False

    def _open_recent_conversation(self) -> bool:
        """去聊天页打开最上面的那条会话。"""
        self.page.navigate("https://www.zhipin.com/web/geek/chat",
                           settle=random.uniform(3.0, 4.0))
        self.page = self.session.sync_page(self.page)

        data = self._look(prompts.CHAT_LIST_USER, "chatlist")
        self._check_risk(data)
        convs = data.get("conversations") or []
        if not convs:
            self.log("    · 会话列表是空的")
            return False

        c = convs[0].get("click") or {}
        if c.get("x") is None:
            return False

        vw, vh = self.page.viewport()
        cx, cy = overlay.norm_to_css(float(c["x"]), float(c["y"]), (vw, vh))
        humanize.move_and_click(self.page, cx, cy, (vw, vh))
        humanize.pause(1.5, 2.5)
        return True

    # ------------------------------------------------------------------
    def _look(self, prompt: str, tag: str) -> dict:
        return self.vision.look(self.page, prompt, tag=tag)

    def _check_risk(self, data: dict):
        """任何一次视觉返回都过一遍风控关键词检查。"""
        blob = str(data.get("risk_hint") or "") + " " + str(data.get("visible_hint") or "")

        for marker in QUOTA_MARKERS:
            if marker in blob:
                raise StopRun(f"平台提示配额用尽（命中「{marker}」）", "quota")
        for marker in CAPTCHA_MARKERS:
            if marker in blob:
                raise StopRun(
                    f"出现人机验证（命中「{marker}」）。已停机，请人工在浏览器里处理后重跑。",
                    "captcha",
                )

        state = data.get("page_state")
        if state == "captcha":
            raise StopRun("页面进入验证码状态。已停机，请人工处理。", "captcha")

    # ------------------------------------------------------------------
    def _summary(self):
        s = self.stats
        print(
            f"\n{'═' * 62}\n"
            f"  本次运行结束\n"
            f"    已发送    {s['sent']}\n"
            f"    演练      {s['dry_run']}\n"
            f"    已沟通过  {s['already']}\n"
            f"    被过滤    {s['filtered']}\n"
            f"    失败      {s['failed']}\n"
            f"    模型调用  {self.client.call_count} 次"
            f"（约 {self.client.total_tokens} tokens）\n"
            f"    截图存档  {self.vision.shots_dir}\n"
            f"{'═' * 62}"
        )
