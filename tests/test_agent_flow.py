"""主控状态机的流程测试。

浏览器、视觉模型、DeepSeek 全部打桩，只保留真实的状态机逻辑和真实的 SQLite 存储。
目的是在没有 API key、不碰真实站点的情况下，验证这几件事：

  * 硬条件不通过的职位确实不会被打招呼
  * 同一屏的多个候选会按优先度排序，而不是从第一个开始发
  * 同屏扫描时被过滤的职位不会重复刷屏
  * 配额上限真的会停机
  * 推荐流翻不动时会往下滚动
  * 演练模式下一次点击都不会发生

    python tests/test_agent_flow.py
"""
from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoboss import config as cfgmod  # noqa: E402
from autoboss import humanize, prompts  # noqa: E402
from autoboss.agent import GreetAgent, StopRun  # noqa: E402
from autoboss.store import Store  # noqa: E402

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


# ===========================================================================
#  打桩
# ===========================================================================
class FakePage:
    def __init__(self):
        self.url = ""
        self.visited: list[str] = []
        self.total_scroll = 0
        self.scrolls: list[tuple[int, float | None, float | None]] = []
        self.clicks: list[tuple[float, float]] = []
        # 模拟 BOSS 把筛选条件写进 URL：在第 url_on_click_at 次点击后把地址换掉
        self.url_on_click: str | None = None
        self.url_on_click_at: int | None = None
        # 按页面文字查元素外框（CSS 像素）。默认查不到，走视觉那条路。
        self.text_boxes: dict[str, list[tuple[float, float, float, float]]] = {}
        self.page_text = ""

    def navigate(self, url, settle=0.0):
        self.url = url
        self.visited.append(url)
        return url

    def current_url(self):
        return self.url

    def viewport(self, refresh=True):
        return (1600, 900)

    def scroll(self, dy, x=None, y=None, tries=4):
        self.total_scroll += dy
        self.scrolls.append((dy, x, y))
        return dy

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.url_on_click and self.url_on_click_at == len(self.clicks):
            self.url = self.url_on_click

    def mouse_move(self, x, y):
        pass

    def type_text(self, text, **kw):
        pass

    def press_key(self, key):
        pass

    def evaluate(self, expr, timeout=20.0):
        return 0

    def find_text_boxes(self, text, prefix=False):
        if prefix:
            return [b for k, v in self.text_boxes.items() if k.startswith(text) for b in v]
        return list(self.text_boxes.get(text, []))

    def text_markers(self, markers):
        return [m for m in markers if m in self.page_text]

    def cookies(self):
        return {"wt2": "x"}

    def is_logged_in(self):
        return True


class FakeSession:
    def __init__(self, page):
        self._page = page

    def start(self):
        return self._page

    def sync_page(self, current):
        return current

    def stop(self):
        pass


class FakeVision:
    """按提示词内容分派脚本化的返回。"""

    def __init__(self, scenes: list[dict]):
        self.scenes = scenes
        self.index = 0
        self.list_calls = 0
        # 记录每次视觉调用问的是什么，用来断言"没有碰过聊天页"
        self.prompts_seen: list[str] = []
        # 提示词原文，用来断言"重试时换了问法"
        self.prompts_text: list[str] = []
        # 筛选下拉"实际展开的是哪个按钮"。默认展开对了；
        # 测试要模拟"点偏成隔壁薪资待遇"就把这个改掉。
        self.open_menu_for = "求职类型"
        # open_menu_for 的应答序列；设了就从这里依次取，用来模拟"第一次错、第二次对"
        self.open_menu_script: list[str] = []

    def look(self, page, prompt, tag="look", max_tokens=1500):
        self.prompts_seen.append(tag)
        self.prompts_text.append(prompt)
        # 职位列表页的提示词现在是 prompts.list_user(kb) —— 每次内容都不一样，
        # 认不出对象身份，所以按 agent 传的 tag（"list_p1"、"list_p2"…）分派。
        if tag.startswith("list_p"):
            self.list_calls += 1
            scene = self.scenes[self.index] if self.index < len(self.scenes) else {"jobs": []}
            return scene
        if prompt is prompts.RECOMMEND_ENTRY_USER:
            return {"found": False, "already_on_recommend": True, "confidence": 0.9}
        if prompt.startswith("【筛选栏】"):
            # 筛选栏整排按钮。默认「求职类型」的框是准的 —— 这正是要验证的：
            # 程序按文字取框中心，而不是听模型报的那个点。
            return {
                "buttons": [
                    {"text": "北京", "current_value": None,
                     "box": {"x1": 130, "y1": 150, "x2": 175, "y2": 175}},
                    {"text": "工作区域", "current_value": None,
                     "box": {"x1": 182, "y1": 150, "x2": 245, "y2": 175}},
                    {"text": "职位类型", "current_value": None,
                     "box": {"x1": 252, "y1": 150, "x2": 315, "y2": 175}},
                    # 注意：这一排三个挨得最近，值必须各不相同，好断言"取的是哪一个"
                    {"text": "求职类型", "current_value": "实习",
                     "box": {"x1": 322, "y1": 150, "x2": 400, "y2": 175}},
                    {"text": "薪资待遇", "current_value": None,
                     "box": {"x1": 407, "y1": 150, "x2": 480, "y2": 175}},
                ],
                "visible_hint": "筛选栏，求职类型已选实习",
            }
        if prompt is prompts.POPUP_USER:
            return {"popup_visible": False, "risk_hint": None}
        if prompt.startswith("筛选下拉菜单现在是展开的"):
            opened = (self.open_menu_script.pop(0) if self.open_menu_script
                      else self.open_menu_for)
            right = "求职类型" in opened
            return {
                # 下拉开错了，自然就找不到「实习」这个选项
                "found": right,
                "click": {"x": 300, "y": 400} if right else None,
                "open_menu_for": opened,
                "visible_options": (["不限", "全职", "兼职", "实习"] if right
                                    else ["不限", "3K以下", "3-5K", "5-10K"]),
                "confidence": 0.95,
            }
        return {"sent": True, "input_box_empty": True, "confidence": 0.95}

    def locate(self, page, prompt, tag, refine=True, min_confidence=0.55,
               force_refine=False, refine_pad=(70, 70)):
        # force_refine / refine_pad 是筛选器那一步要的：那一排按钮挨太近，
        # 必须做局部放大复核、而且复核框要能装下整条筛选栏。
        # 桩不关心这些，照给坐标。
        return {"x": 800, "y": 300, "confidence": 0.95, "raw": {}}

    def look_and_pick(self, page, prompt, tag, min_confidence=0.55):
        """真实 Vision 上这个是 look + 取坐标；桩这边直接给结果。

        筛选器那步靠返回里的 `open_menu_for` 判断"点开的下拉对不对"，
        所以这里要能报出"当前展开的是哪个下拉" —— 默认报对的那个，
        测试要模拟"点错成隔壁"就把 open_menu_for 改掉。
        """
        data = self.look(page, prompt, tag=tag)
        return data, (300, 400)

    def annotate(self, page, marks, tag):
        return ""

    def advance(self):
        self.index += 1


class FakeClient:
    call_count = 0
    total_tokens = 0

    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return "我在推荐系统方向做过完整项目，用双塔召回把 Recall@10 从 0.21 提到 0.34，与贵司岗位方向比较契合。"


@contextmanager
def agent_env(scenes, **kw):
    """保证断言失败时也会关掉 SQLite ——
    否则 Windows 上临时目录清理会报文件占用，把真正的失败原因盖掉。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        agent, page, store, vision, client = make_agent(scenes, tmp=tmp, **kw)
        try:
            yield agent, page, store, vision, client
        finally:
            store.close()


def make_agent(jobs_per_scene, daily_cap=10, source_mode="recommend", tmp=None,
               dry_run=True, keywords=None, education=None, min_wage=None):
    cfg = cfgmod.load()
    cfg._raw["source"]["mode"] = source_mode
    cfg._raw["limits"]["daily_cap"] = daily_cap
    cfg._raw["safety"]["dry_run"] = True
    # 关掉间隔，测试要快
    cfg._raw["limits"]["min_interval_sec"] = 0
    cfg._raw["limits"]["max_interval_sec"] = 0
    # 默认关掉 BOSS 筛选器点击：它也是"点击"，会干扰
    # test_dry_run_never_clicks 的断言。单独有测试覆盖它。
    cfg._raw.setdefault("job_filter", {})["boss_filters"] = []

    # 求职者画像：测试自带一份，不依赖仓库里那份真的 resume/kb.md。
    # （没有它 agent 会直接停机 —— 那是刻意的，见 GreetAgent._require_kb）
    kb_file = Path(tmp) / "kb.md"
    kb_file.write_text(
        "# 测试画像\n可投：推荐算法实习生、大模型算法实习生、Agent 岗位。\n",
        encoding="utf-8")
    cfg._raw.setdefault("resume", {})["kb_path"] = str(kb_file)

    page = FakePage()
    client = FakeClient()
    store = Store(Path(tmp) / "t.db", "test")
    vision = FakeVision(jobs_per_scene)
    agent = GreetAgent(cfg, client, FakeSession(page), store, vision,
                       dry_run=dry_run, verbose=False, keywords=keywords,
                       education=education, min_wage=min_wage)
    # 走 run() 时这一步由 run() 完成；测试直接调 _loop / _pick_target 就得自己设
    agent.page = page
    return agent, page, store, vision, client


def silence():
    """把拟人化延时全部去掉，否则测试跑几分钟。"""
    humanize.pause = lambda a, b: None
    humanize.sleep = lambda s: None
    humanize.human_delay = lambda a, b: None
    humanize.think_pause = lambda *a, **k: None
    humanize.move_and_click = lambda page, x, y, vp: page.click(x, y)


JOB = dict

# ===========================================================================
def test_filtering_and_ordering():
    silence()
    scene = {"page_state": "job_list", "jobs": [
        # 低分：档位「次匹配」、又不在海淀（但硬条件都满足）
        {"title": "数据标注实习生", "company": "A", "salary": "320元/天",
         "location": "北京·昌平区", "district": "昌平区", "job_type": "实习", "fit": "次匹配",
         "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95},
        # 高分：档位「最匹配」+ 在海淀
        {"title": "推荐算法实习生", "company": "B", "salary": "400-500元/天",
         "location": "北京·海淀区", "district": "海淀区", "job_type": "实习", "fit": "最匹配",
         "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95},
        # 硬条件不过：日薪不足
        {"title": "算法实习生", "company": "C", "salary": "150-200元/天",
         "location": "北京·海淀区", "district": "海淀区", "job_type": "实习", "fit": "最匹配",
         "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95},
    ]}
    with agent_env([scene] * 4) as (agent, page, store, vision, _):
        agent._enter_source()
        targets = agent._rank_targets(scene["jobs"])
        check("一屏内合格岗位全部入选（低分那条也算）", len(targets), 2)
        check("排第一的是高分岗位", targets[0]["title"], "推荐算法实习生")
        check("过滤计数 = 1", agent.stats["filtered"], 1)


def test_filtered_not_repeated():
    silence()
    scene = {"page_state": "job_list", "jobs": [
        {"title": "算法实习生", "company": "C", "salary": "150-200元/天",
         "location": "北京·海淀区", "district": "海淀区", "job_type": "实习", "fit": "最匹配",
         "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95},
    ]}
    with agent_env([scene] * 3) as (agent, page, store, vision, _):
        agent._enter_source()
        for _ in range(3):
            check("无可发职位", agent._rank_targets(scene["jobs"]), [])
        check("同屏重复扫描只计一次", agent.stats["filtered"], 1)


def test_agent_applies_education_and_wage_overrides():
    """QQ / CLI 传进来的学历、日薪要盖住 config，并真的参与过滤。"""
    silence()
    base = {"location": "北京·海淀区", "district": "海淀区", "job_type": "实习",
            "fit": "最匹配", "greet_click": {"x": 800, "y": 300},
            "greeted": False, "confidence": 0.95}
    scene = {"page_state": "job_list", "jobs": [
        {**base, "title": "大模型算法实习生", "company": "A", "salary": "600元/天",
         "tags": ["4天/周", "本科"]},
        {**base, "title": "多模态算法实习生", "company": "B", "salary": "600元/天",
         "tags": ["4天/周", "硕士"]},
        {**base, "title": "推荐算法实习生", "company": "C", "salary": "400元/天",
         "tags": ["4天/周", "本科"]},
        # 卡片上读不出学历：不拦（和城市读不到不拦一致）
        {**base, "title": "Agent实习生", "company": "D", "salary": "600元/天"},
    ]}
    with agent_env([scene] * 3, education="本科", min_wage=500) as (agent, page, store, vision, _):
        check("学历覆盖成 本科", agent.rules.education, "本科")
        check("日薪覆盖成 500", agent.rules.min_daily_wage, 500.0)
        agent._enter_source()
        titles = [j["title"] for j in agent._rank_targets(scene["jobs"])]
        check("只要学历和日薪都过关的", titles, ["大模型算法实习生", "Agent实习生"])
        check("过滤计数 = 2", agent.stats["filtered"], 2)


def test_degree_filter_in_url():
    """学历要直接写进 BOSS 地址，并由 URL 识别筛选栏里已选中的学历。"""
    silence()
    with agent_env([], education="硕士") as (agent, page, store, vision, _):
        check("degree 参数", agent.degree_param, "204")
        check_true("base_url 带 degree", "degree=204" in agent.base_url)
        agent._start_source("search", "大模型算法")
        check_true("搜索来源也带 degree", "degree=204" in agent.base_url)

        # URL 里已经带了学历，筛选栏显示「学历要求(1)」不算误点
        def fake_eval(expr, timeout=20.0):
            return '["学历要求"]' if '"学历要求"' in expr else "[]"

        page.evaluate = fake_eval
        page.url = agent.base_url
        check("学历筛选不算 stray", agent._stray_filters(), [])
        agent.degree_param = ""
        page.url = "https://www.zhipin.com/web/geek/jobs?city=101010100"
        check_true("没有 URL 学历时仍能发现误点的学历筛选",
                   "学历要求" in agent._stray_filters())


def test_daily_cap_stops():
    silence()
    job = {"title": "推荐算法实习生", "company": "B", "salary": "400元/天",
           "location": "北京·海淀区", "district": "海淀区", "job_type": "实习", "fit": "最匹配",
           "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95}
    # 用多个不同公司，否则打完一个之后其余都会被去重库挡掉
    jobs = [{**job, "company": f"B{i}"} for i in range(6)]
    scene = {"page_state": "job_list", "jobs": jobs}

    with agent_env([scene] * 10, daily_cap=2) as (agent, page, store, vision, _):
        stopped = None
        try:
            agent._loop()
        except StopRun as exc:
            stopped = exc
        check_true("应该因配额停机", stopped is not None)
        check("停机原因是配额", stopped.kind if stopped else None, "quota")
        check("演练计数 = 2", agent.stats["dry_run"], 2)


def test_dry_run_never_clicks():
    silence()
    scene = {"page_state": "job_list", "jobs": [
        {"title": "推荐算法实习生", "company": "B", "salary": "400元/天",
         "location": "北京·海淀区", "district": "海淀区", "job_type": "实习", "fit": "最匹配",
         "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95},
    ]}
    with agent_env([scene] * 6, daily_cap=1) as (agent, page, store, vision, _):
        try:
            agent._loop()
        except StopRun:
            pass
        check("演练模式不得产生任何点击", page.clicks, [])
        check("演练计数 = 1", agent.stats["dry_run"], 1)


def test_boss_filters_clicked():
    """筛选器必须真的被点开并选中。

    这是实测踩出来的：推荐流默认全是全职岗，卡片级过滤只能一条条滤掉，
    结果是一条都发不出去。筛选器负责把池子换对。
    """
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.base_url = agent.feed_url = page.url = "https://www.zhipin.com/web/geek/jobs?city=101010100"

        agent._enter_source()
        # 一次筛选 = 点开按钮 + 选中选项 = 2 次点击
        check("筛选操作点击次数 = 2", len(page.clicks), 2)
        check("已记录点过筛选", agent._filters_applied, True)
        check("地址未变则不认为会持久", agent._filters_persist, False)


def test_boss_filters_persist_in_url():
    """若筛选条件进了 URL，就记录新地址，之后不再重复点击。"""
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    new_url = "https://www.zhipin.com/web/geek/jobs?city=101010100&jobType=1901"
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.base_url = agent.feed_url = page.url = "https://www.zhipin.com/web/geek/jobs?city=101010100"
        # 第 2 次点击（选中选项）之后地址变化，模拟 BOSS 把筛选写进 URL
        page.url_on_click = new_url
        page.url_on_click_at = 2

        agent._enter_source()
        check_true("判定为会持久", agent._filters_persist)
        check("feed_url 已更新为新地址", agent.feed_url, new_url)

        clicks_before = len(page.clicks)
        agent._enter_source()
        check("第二次进入不再重复点筛选", len(page.clicks), clicks_before)


def test_repeated_cards_do_not_count_as_dry():
    """关键词之间重叠：翻到的全是本次已经滤过的老岗位 → 不累计「没岗位可投」。"""
    silence()
    bad = {**GOOD_JOB, "company": "X", "fit": "不可用"}
    scene = {"page_state": "job_list", "jobs": [bad]}
    with agent_env([scene] * 30) as (agent, page, store, vision, _):
        agent.source_mode = "both"
        agent.cfg._raw["search"]["queries"] = [f"词{i}" for i in range(6)]
        agent.max_dry = 3
        page.scroll = lambda dy, x=None, y=None, tries=4: 0
        stopped = None
        try:
            agent._loop()
        except StopRun as exc:
            stopped = exc
        check("同一张不可投的卡反复出现，不会触发收工", stopped, None)
        # 换了关键词计数器就清零；后面几个词翻到的都是老卡，不该再累计
        check("老卡重复出现不累计", agent._dry_streak, 0)


def test_job_type_in_url_skips_filter_clicks():
    """地址里已带 jobType=1902（[search].job_type）→ 不点筛选栏，直接扫。"""
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.cfg._raw["search"]["job_type"] = "1902"
        agent._start_source("search", "大模型算法")
        check_true("搜索地址带上了 jobType", "jobType=1902" in agent.feed_url)
        agent._enter_source()
        check("没有点筛选栏", len(page.clicks), 0)
        check_true("视为筛选已生效", agent._filters_applied and agent._filters_persist)


def test_recommend_last_puts_search_first():
    silence()
    with agent_env([]) as (agent, page, store, vision, _):
        agent.source_mode = "both"
        agent.cfg._raw["search"]["queries"] = ["大模型算法", "Agent"]
        agent.cfg._raw["source"]["expect_tabs"] = ["算法工程师(北京)"]
        agent.cfg._raw["source"]["recommend_last"] = True
        check("先搜关键词，推荐流垫后", [k for k, _ in agent._sources()],
              ["search", "search", "recommend", "expect"])
        agent.cfg._raw["source"]["recommend_last"] = False
        check("关掉则推荐流在前", [k for k, _ in agent._sources()][0], "recommend")


GOOD_JOB = {
    "title": "推荐算法实习生", "company": "B", "salary": "400-500元/天",
    "location": "北京·海淀区", "district": "海淀区", "job_type": "实习", "fit": "最匹配",
    "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95,
}


def test_only_greet_never_chats():
    """★ 核心契约：只打招呼，不对话。★

    用户明确要求过 —— 点「立即沟通」就够了，不要进聊天页再替他发一条消息。
    这个测试确保程序一次都不碰聊天页。
    """
    silence()
    scene = {"page_state": "job_list", "jobs": [GOOD_JOB]}
    with agent_env([scene] * 6, daily_cap=1, dry_run=False) as (agent, page, store, vision, _):
        try:
            agent._loop()
        except StopRun:
            pass

        # 只应该有"点立即沟通"这一次点击，不该有聊天页里的任何操作
        check("点击次数 = 1（只有打招呼那一下）", len(page.clicks), 1)
        chat_calls = [t for t in vision.prompts_seen if "chat" in t or "verify" in t]
        check("未发生任何聊天页相关调用", chat_calls, [])
        check("计为已发送 1 条", agent.stats["sent"], 1)
        check("没有生成话术", agent.store.conn.execute(
            "SELECT message FROM greeted WHERE status='sent'").fetchone()[0], "")


def test_jump_to_chat_goes_straight_back():
    """点完「立即沟通」BOSS 直接跳进聊天页 → 立刻回职位列表，不在聊天页点任何东西。"""
    silence()
    scene = {"page_state": "job_list", "jobs": [GOOD_JOB]}
    with agent_env([scene] * 4, daily_cap=1, dry_run=False) as (agent, page, store, vision, _):
        agent.feed_url = "https://www.zhipin.com/web/geek/jobs?query=x&city=101010100"
        page.url_on_click = "https://www.zhipin.com/web/geek/chat?id=1"
        page.url_on_click_at = 1
        try:
            agent._loop()
        except StopRun:
            pass
        check("只点了「立即沟通」那一下", len(page.clicks), 1)
        check_true("跳走后回到了职位列表", page.visited and page.visited[-1] == agent.feed_url)
        check("算作已打招呼", agent.stats["sent"], 1)


def test_same_job_title():
    """标题比对要能容下列表里的截断写法，同时不放过真正不同的职位。"""
    silence()
    with agent_env([]) as (agent, *_):
        same = agent._same_job_title
        check_true("完全相同", same("推荐算法实习生", "推荐算法实习生"))
        check_true("目标被截断", same("大模型应用算法实习生", "大模型应用算法实习生（..."))
        check_true("双方都截断", same("机器人建图与定位算法实习生（S...",
                                   "机器人建图与定位算法实习生（SLAM）"))
        check_true("带空格差异", same("SLAM 算法实习生", "SLAM算法实习生"))
        check("不同职位要判否", same("推荐算法实习生", "数据标注实习生"), False)
        check("完全无关要判否", same("推荐算法实习生", "前端开发工程师"), False)
        check_true("信息不足时不拦", same("", "推荐算法实习生"))
        check_true("另一边为空也不拦", same("推荐算法实习生", ""))


def test_greet_switches_detail_panel_first():
    """必须先点开目标职位的卡片，再点沟通按钮。

    推荐流是「左卡片 + 右详情」，右侧面板显示的是当前选中的卡片。
    若不先切过去，屏幕上的「立即沟通」属于别的职位 —— 会打错人。
    """
    silence()
    job = {**GOOD_JOB, "card_click": {"x": 400, "y": 300}}
    scene = {"page_state": "job_list", "jobs": [job]}
    with agent_env([scene] * 6, daily_cap=1, dry_run=False) as (agent, page, store, vision, _):
        try:
            agent._loop()
        except StopRun:
            pass
        # 一次 = 点卡片切详情 + 点立即沟通
        check("点击次数 = 2（切卡片 + 打招呼）", len(page.clicks), 2)
        check("计为已发送", agent.stats["sent"], 1)


def test_greet_skips_when_button_belongs_to_other_job():
    """按钮所属职位与目标不符时，必须拒绝点击。"""
    silence()
    scene = {"page_state": "job_list", "jobs": [GOOD_JOB]}
    with agent_env([scene] * 6, daily_cap=1, dry_run=False) as (agent, page, store, vision, _):

        # 让 locate 返回一个属于别的职位的按钮（重找卡片也拿不到东西）
        def fake_locate(page_, prompt, tag, **kw):
            if tag == "refind_card":
                return None
            return {"x": 800, "y": 300, "confidence": 0.95,
                    "raw": {"context_title": "完全无关的前端开发工程师"}}

        vision.locate = fake_locate
        try:
            agent._loop()
        except StopRun:
            pass
        check("不允许点击", len(page.clicks), 0)
        check("计为失败而不是已发", agent.stats["sent"], 0)
        check_true("有失败记录", agent.stats["failed"] >= 1)


def test_button_not_found_skips_not_aborts():
    """定位不到按钮只该跳过这个职位，不该终止整轮。"""
    silence()
    jobs = [{**GOOD_JOB, "company": f"C{i}"} for i in range(3)]
    scene = {"page_state": "job_list", "jobs": jobs}
    with agent_env([scene] * 8, daily_cap=3, dry_run=False) as (agent, page, store, vision, _):
        vision.locate = lambda *a, **k: None      # 永远定位不到按钮
        stopped = None
        try:
            agent._loop()
        except StopRun as exc:
            stopped = exc
        # 应该跑满 max_empty_scans 次翻找后才自己收工，而不是第一次就崩
        check_true("失败被记为单条失败", agent.stats["failed"] >= 1)
        check("没有误判为已发送", agent.stats["sent"], 0)
        check_true("没有因按钮定位失败而抛停机异常",
                   stopped is None or stopped.kind != "stop")


def test_filter_misfire_is_detected_and_retried():
    """点偏成隔壁下拉时要能发现并重来。

    实测踩到的：「求职类型」和「薪资待遇」两个按钮的中心只差半个按钮宽
    （归一化 336 vs 395），y 还完全一样。模型会**自信地**点到隔壁去
    （confidence 0.95），坐标上完全看不出异常。不核对"实际展开的是哪个
    下拉"的话，接下来就会跑去薪资档位里找「实习」，必然找不到，
    然后静默退回卡片级过滤 —— 整轮白跑，一条都发不出去。
    """
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.base_url = agent.feed_url = page.url = \
            "https://www.zhipin.com/web/geek/jobs?city=101010100"
        # 第一次点开的是「薪资待遇」，第二次才点对
        vision.open_menu_script = ["薪资待遇", "求职类型"]

        agent._enter_source()
        # 第 1 轮：点按钮 → 发现下拉开错 → Esc。第 2 轮：点按钮 → 点选项。
        check("发现点偏后重试并成功（3 次点击）", len(page.clicks), 3)

        # 重试必须换个问法。若第二次还是把同一排原样问一遍，就会拿到同一个框、
        # 点同一个点，注定第二次也失败 —— 那叫空重试。
        rows = [t for t in vision.prompts_text if t.startswith("【筛选栏】")]
        check("读了两次筛选栏（原始 + 重试）", len(rows), 2)
        check_true("重试时把「上次点成了什么」喂了回去", "薪资待遇" in rows[1])


def test_filter_gives_up_after_two_misfires():
    """连续点偏两次就放弃 —— 不能无限重试，卡片级过滤仍会兜底。"""
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.base_url = agent.feed_url = page.url = \
            "https://www.zhipin.com/web/geek/jobs?city=101010100"
        vision.open_menu_script = ["薪资待遇"] * 8

        agent._enter_source()
        # 两次尝试都只点了按钮，没点到选项
        check("放弃前只点了两次按钮", len(page.clicks), 2)
        check("没有误报成已持久化", agent._filters_persist, False)


def test_zoom_local_coords_are_converted():
    """复核段报了局部坐标时要能换算回全局。

    实测一例：`zoom [313,117,477,281] → click {x:70, y:500}`。
    70 根本不在 [313, 477] 里 —— 模型报的是**裁剪图内部**的相对位置，
    完全无视网格上标的全局刻度。当成全局值用就会点到页面左边去
    （那次实际点在了左侧职位卡上，于是筛选下拉一个都没弹出来，
    整步静默失效 —— 表面看只是"下拉里没找到实习"）。

    换算：(313 + 70/1000×164, 117 + 500/1000×164) = (324, 199)，正是目标。
    """
    from autoboss.vision import Vision
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = Store(Path(tmp) / "v.db", "vt")
        vision = Vision(FakeClient(), cfgmod.load(), store, Path(tmp), "vt",
                        verbose=False)
        bbox = [313, 117, 477, 281]
        check_true("框外坐标换算回全局",
                   vision._zoom_pick_to_global((70, 500), bbox) == (324, 199))
        check_true("框内坐标原样保留（本来就是全局值）",
                   vision._zoom_pick_to_global((400, 200), bbox) == (400, 200))
        check_true("没有 bbox 时不动它",
                   vision._zoom_pick_to_global((70, 500), None) == (70, 500))
        store.close()


def test_card_box_center_is_used():
    """点卡片时取「框的中心」，不取模型报的那个点。

    实测让模型直接报"卡片中心点"，它会贴着卡片**右边缘**报：卡片框是 x[127,372]，
    它报 370 —— 点下去正好落在卡片边界上，**不报错、也不选中**，右侧详情面板
    还停在空占位，于是「立即沟通」找不到，一条完全匹配的岗位被记成 failed 丢掉。
    （虾皮「AI Agent算法工程师」那条就是这么没的。）
    """
    silence()
    with agent_env([{"page_state": "job_list", "jobs": []}]) as (agent, _page, _store, _vision, _):
        job = {**GOOD_JOB,
               "card_box": {"x1": 127, "y1": 196, "x2": 372, "y2": 300},
               "card_click": {"x": 370, "y": 275}}   # 模型报的"中心"，其实是右边缘
        check("优先用框的中心", agent._card_point(job), (249.5, 248.0))
        check("没有框时退回 card_click",
              agent._card_point({"card_click": {"x": 10, "y": 20}}), (10.0, 20.0))
        check("两样都没有时返回 None", agent._card_point({"card_box": {"x1": 5}}), None)


def test_whole_screen_greeted_before_moving_on():
    """★ 一屏内的合格岗位要连续投完，不能投一个就重进一次推荐流。★

    以前每发一条就 _enter_source()，等于投一份就刷新一次池子：屏幕上剩下那些
    已经判合格、还没投的岗位会跟着这次刷新一起消失 —— 既白扔平台推荐名额，
    也白扔已经花掉的模型调用。这是"一屏只投一个"的直接原因。
    """
    silence()
    jobs = [{**GOOD_JOB, "company": f"D{i}", "title": f"推荐算法实习生{i}"}
            for i in range(3)]
    scene = {"page_state": "job_list", "jobs": jobs}
    with agent_env([scene] * 6, daily_cap=3, dry_run=False) as (agent, page, store, vision, _):
        before = len(page.visited)
        try:
            agent._loop()
        except StopRun:
            pass
        check("一屏三条全部发出", agent.stats["sent"], 3)
        # 进来源只有开头那一次。翻页改为纯滚动，不再重新导航 ——
        # 每投一条重进一次的话这里会是 4，每投完一屏重进一次会是 2。
        check("全程只进过一次推荐流（翻页靠滚动）", len(page.visited) - before, 1)


def test_greet_retries_once_when_button_not_found():
    """第一次定位不到「立即沟通」要重来一次，而不是直接放弃这个职位。"""
    silence()
    job = {**GOOD_JOB, "card_click": {"x": 400, "y": 300}}
    scene = {"page_state": "job_list", "jobs": [job]}
    with agent_env([scene] * 6, daily_cap=1, dry_run=False) as (agent, page, store, vision, _):
        seen = {"btn": 0, "refind": 0}

        def fake_locate(page_, prompt, tag, **kw):
            if tag == "refind_card":
                seen["refind"] += 1
                return {"x": 400, "y": 300, "confidence": 0.9, "raw": {}}
            seen["btn"] += 1
            if seen["btn"] == 1:
                return None          # 第一次：面板没切过去，按钮找不到
            return {"x": 800, "y": 300, "confidence": 0.95,
                    "raw": {"context_title": "推荐算法实习生"}}

        vision.locate = fake_locate
        try:
            agent._loop()
        except StopRun:
            pass
        check("按职位名重找了一次卡片", seen["refind"], 1)
        check("重试后打成了招呼", agent.stats["sent"], 1)
        check("没有记成失败", agent.stats["failed"], 0)


def test_scroll_targets_left_job_column():
    """滚轮必须打在**左栏职位列表**上。

    `BrowserSession.scroll()` 默认在视口正中派发 —— 归一化正好 (500, 500)，
    那个点落在**右栏职位详情面板里面**。右栏是独立子窗口、有自己的滚动条，
    滚轮打在那儿只会滚那篇职位描述，左边岗位列表纹丝不动。
    """
    silence()
    with agent_env([{"page_state": "job_list", "jobs": []}]) as (agent, page, store, vision, _):
        moved = agent._scroll_list(800)
        check("滚轮有位移", moved, 800)
        check("只派发了一次", len(page.scrolls), 1)
        _dy, x, _y = page.scrolls[0]
        check_true("x 落在左栏（小于视口一半 800）", x is not None and x < 800)


def test_filter_button_matched_by_text_not_by_point():
    """★ 筛选栏按钮按**文字**定位，不听模型报的那个点。★

    「求职类型」「职位类型」「薪资待遇」三个按钮的中心只差半个按钮宽。
    让模型报单点时它会**自信地报错**：confidence 0.9+，坐标却稳定落在隔壁；
    而局部放大复核是围着这个错点放大的，框里根本没有正确的按钮，救不回来。
    改成让它把整排按钮连同外框读出来、程序按文字精确匹配，歧义就消掉了 ——
    模型只需要"读得对"，不需要"点得准"。
    """
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.base_url = agent.feed_url = page.url = \
            "https://www.zhipin.com/web/geek/jobs?city=101010100"

        agent._enter_source()

        check_true("问的是整排筛选栏", "filter_row" in vision.prompts_seen)
        # 桩里「求职类型」框 = x[322,400] y[150,175] → 归一化中心 (361, 162.5)
        # → CSS (361/1000*1600, 162.5/1000*900) = (577.6, 146.25)
        check("落点是「求职类型」框的中心", page.clicks[0], (577.6, 146.25))
        check_true("没点到隔壁「薪资待遇」（其中心 x=443.5 归一化）",
                   page.clicks[0][0] < 407 / 1000 * 1600)


def test_filter_located_by_page_text_first():
    """★ 筛选栏按页面文字取外框，不靠模型估坐标。★

    实测模型能把「求职类型」读对，外框却稳定往右偏，点下去开的是「薪资待遇」。
    按文字取几何位置没这个问题，也不花模型调用；选项只认按钮正下方那个，
    卡片标签里的「实习」不算。
    """
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.base_url = agent.feed_url = page.url =             "https://www.zhipin.com/web/geek/jobs?city=101010100"
        page.text_boxes = {
            "求职类型": [(500, 170, 80, 30)],
            "实习": [(300, 700, 40, 20), (510, 230, 40, 24)],   # 前一个是卡片标签
        }

        agent._enter_source()

        check("先点按钮中心", page.clicks[0], (540, 185))
        check("再点下拉里的「实习」", tuple(round(v) for v in page.clicks[1]), (530, 242))
        check("没问模型筛选栏", "filter_row" in vision.prompts_seen, False)


def test_run_limit_independent_of_today_sent():
    """--limit 是「本次最多发几条」，不能拿今天已发的去减它。

    实测：今天已发 5 条、daily_cap=200，`--limit 5` 一条没发就报「今日配额已用尽」。
    """
    silence()
    job = {"title": "推荐算法实习生", "company": "B", "salary": "400元/天",
           "location": "北京·海淀区", "district": "海淀区", "job_type": "实习", "fit": "最匹配",
           "greet_click": {"x": 800, "y": 300}, "greeted": False, "confidence": 0.95}
    jobs = [{**job, "company": f"B{i}"} for i in range(6)]
    scene = {"page_state": "job_list", "jobs": jobs}

    with agent_env([scene] * 10, daily_cap=200) as (agent, page, store, vision, _):
        for i in range(5):
            store.record(f"旧岗位{i}", "X", "", "", "sent")
        agent.run_limit = 3
        try:
            agent._loop()
        except StopRun:
            pass
        check("本次发满 3 条", agent.stats["dry_run"], 3)


def test_success_popup_closed_by_page_text():
    """「已向BOSS发送消息」弹窗按文字点「留在此页」，并核对它真的关了。

    实测模型报的按钮坐标偏上，点空了弹窗还盖着，后面每个职位都找不到「立即沟通」。
    """
    silence()
    scene = {"page_state": "job_list", "jobs": [GOOD_JOB]}
    with agent_env([scene] * 6, daily_cap=1, dry_run=False) as (agent, page, store, vision, _):
        stay = (980, 520, 90, 36)
        # 点「立即沟通」（第 1 次点击）之后弹窗出现；点中「留在此页」中心才消失
        def find(text, prefix=False):
            if text != "留在此页" or not page.clicks:
                return []
            return [] if (1025, 538) in page.clicks else [stay]
        page.find_text_boxes = find
        try:
            agent._loop()
        except StopRun:
            pass

        check("第二下点在「留在此页」中心", page.clicks[1:2], [(1025, 538)])
        check("没再用截图识别弹窗", "popup" in vision.prompts_seen, False)
        check("计为已发送 1 条", agent.stats["sent"], 1)


def test_filter_menu_closed_after_select():
    """多选下拉勾上「实习」后不会自己收起，会挡住第一张职位卡 —— 必须收起来。"""
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 2) as (agent, page, store, vision, _):
        agent.boss_filters = [("求职类型", "实习")]
        agent.base_url = agent.feed_url = page.url =             "https://www.zhipin.com/web/geek/jobs?city=101010100"
        # Esc 和挪鼠标都收不起来，只有再点一次按钮（第 3 次点击）才收起
        def find(text, prefix=False):
            if text == "求职类型":
                return [(500, 170, 80, 30)]
            if text == "实习":
                return [] if len(page.clicks) >= 3 else [(510, 230, 40, 24)]
            return []
        page.find_text_boxes = find

        agent._enter_source()

        check("第三下点回按钮把下拉收起", page.clicks[2:3], [(540, 185)])
        check("没有多余点击", len(page.clicks), 3)


def test_scroll_overlaps_screens():
    """每次只滚大半屏：一屏只完整看得到三张卡，滚多了会把中间的卡整张跳过。"""
    silence()
    with agent_env([{"page_state": "job_list", "jobs": []}]) as (agent, page, *_):
        agent._advance_screen()
        dy = page.scrolls[0][0]
        check_true(f"滚动距离 {dy} 小于 0.65 屏", 0 < dy <= 900 * 0.65)


def test_card_clicked_by_title_and_company():
    """同名卡片有好几张时按标题下方的公司名挑，点的是标题本身，不用模型报的框。"""
    silence()
    with agent_env([{"page_state": "job_list", "jobs": []}]) as (agent, page, *_):
        page.text_boxes = {
            "大模型策略研发工程师（数据方向）": [(300, 250, 200, 24)],
            "大模型策略研发工程师（算法方向）": [(300, 450, 200, 24)],
            "百度在线": [(340, 520, 60, 20)],
            "百度": [(340, 320, 40, 20)],
        }
        job = {"title": "大模型策略研发工程师（...", "company": "百度在线",
               "card_box": {"x1": 100, "y1": 100, "x2": 200, "y2": 200}}
        agent._open_job_detail(job)
        check("点的是公司对得上的那张卡的标题", page.clicks, [(400, 462)])


def test_sources_switch_when_list_ends():
    """推荐流翻到页脚就换下一个来源（逐个搜关键词），而不是对着同一屏空转。"""
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 20) as (agent, page, store, vision, _):
        agent.source_mode = "both"
        agent.cfg._raw["search"]["queries"] = ["Agent实习", "推荐算法实习"]
        page.scroll = lambda dy, x=None, y=None, tries=4: 0     # 一滚就到底
        agent._loop()

        searched = [u for u in page.visited if "query=" in u]
        check_true("搜了 Agent实习", any("Agent" in u for u in searched))
        check_true("搜了 推荐算法实习",
                   any("%E6%8E%A8%E8%8D%90%E7%AE%97%E6%B3%95" in u for u in searched))
        # 3 个来源，每个到底前各看 2 屏
        check("没有空转", vision.list_calls, 6)


def test_dry_keyword_moves_to_next_source():
    """一个关键词翻到底都没货 → 换下一个关键词，**不结束整轮任务**。

    回归测试（2026-10-21 22:00 的日志）：搜「大模型算法」时 6 屏全被日薪门槛
    挡掉，加上「大模型应用」的 2 屏凑满 8 屏，整轮任务就在来源 2/42 收工了，
    后面 40 个关键词一个都没试到。
    """
    silence()
    with agent_env([{"page_state": "job_list", "jobs": []}]) as (agent, page, store, vision, _):
        # 每读一屏换一批「新岗位，但一个都不能投」——这就是当年的现场
        counter = {"n": 0}
        def look(page, prompt, tag="look", max_tokens=1500):
            n = counter["n"]
            counter["n"] += 1
            return {"page_state": "job_list", "jobs": [{
                **GOOD_JOB, "title": f"不对口实习{n}", "company": f"X{n}",
                "fit": "不可用"}]}
        vision.look = look
        agent.source_mode = "search"
        agent.cfg._raw["search"]["queries"] = ["大模型算法", "大模型应用", "Agent"]
        agent.max_dry = 3               # 每个词 3 屏都没货就换下一个
        stopped = None
        try:
            agent._loop()
        except StopRun as exc:
            stopped = exc
        check("一个词没货不再结束整轮", stopped, None)
        check("三个词各扫 3 屏", counter["n"], 9)
        from urllib.parse import quote
        searched = [u for u in page.visited if "query=" in u]
        for word in ("大模型算法", "大模型应用", "Agent"):
            check_true(f"搜了「{word}」", any(quote(word) in u for u in searched))


def test_dry_sources_limit_stops_run():
    """连续多少个来源都没投出去才收工（max_dry_sources），0 = 全部走完。"""
    silence()
    with agent_env([{"page_state": "job_list", "jobs": []}]) as (agent, page, store, vision, _):
        counter = {"n": 0}
        def look(page, prompt, tag="look", max_tokens=1500):
            n = counter["n"]
            counter["n"] += 1
            return {"page_state": "job_list", "jobs": [{
                **GOOD_JOB, "title": f"不对口实习{n}", "company": f"Y{n}",
                "fit": "不可用"}]}
        vision.look = look
        agent.source_mode = "search"
        agent.cfg._raw["search"]["queries"] = ["词一", "词二", "词三"]
        agent.max_dry = 3
        agent.max_dry_sources = 2
        stopped = None
        try:
            agent._loop()
        except StopRun as exc:
            stopped = exc
        check("连续 2 个来源没货就收工", stopped.kind if stopped else None, "dry")
        check("第三个词没再搜", counter["n"], 6)


def test_override_keywords_search_only():
    """--keywords / QQ 指定关键词时只搜这些词，不掺推荐流。"""
    silence()
    scene = {"page_state": "job_list", "jobs": []}
    with agent_env([scene] * 10, source_mode="both",
                   keywords=["大模型算法", "Agent"]) as (agent, page, *_):
        agent.cfg._raw["search"]["queries"] = ["配置里的词"]
        agent.cfg._raw["source"]["recommend_last"] = True
        check("来源只有指定的关键词", agent._sources(),
              [("search", "大模型算法"), ("search", "Agent")])
        page.scroll = lambda dy, x=None, y=None, tries=4: 0     # 一滚就到底
        agent._loop()
        searched = [u for u in page.visited if "query=" in u]
        check("只搜了这两个词", len(searched), 2)
        check("一次都没进推荐流", len(page.visited), 2)


def test_platform_quota_text_stops_run():
    """点完「立即沟通」页面上冒出额度用完的字样 → 立即停机，这一条不记成已发送。

    用户也会在手机上投，本地计数看不到那部分，真正的上限只能听平台的。
    点之前就有的字样（比如职位描述里）不算。
    """
    silence()
    scene = {"page_state": "job_list", "jobs": [GOOD_JOB]}
    with agent_env([scene] * 6, daily_cap=200, dry_run=False) as (agent, page, store, vision, _):
        page.page_text = "岗位要求：并发已达上限"       # 点之前就有，不算
        orig_click = page.click

        def click(x, y):
            orig_click(x, y)
            page.page_text += " 今日沟通次数已用完"
        page.click = click
        stopped = None
        try:
            agent._loop()
        except StopRun as exc:
            stopped = exc
        check("停机原因是配额", stopped.kind if stopped else None, "quota")
        check_true("命中的是新出现的字样", stopped and "已达上限" not in stopped.reason)
        check("没记成已发送", agent.stats["sent"], 0)


def test_card_title_paren_width_mismatch():
    """模型把全角括号抄成半角时，退到括号前那段 + 公司名去找卡。"""
    silence()
    with agent_env([{"page_state": "job_list", "jobs": []}]) as (agent, page, *_):
        page.text_boxes = {
            "无人机算法工程师": [(300, 250, 200, 24)],
            "算法工程师（27实习转正）-希望学（北京）": [(300, 450, 200, 24)],
            "算法工程师": [(300, 650, 120, 24)],
            "希望学": [(340, 520, 60, 20)],
            "大疆": [(340, 320, 60, 20)],
        }
        job = {"title": "算法工程师(27实习转正)-希望学(…", "company": "希望学",
               "card_box": {"x1": 100, "y1": 100, "x2": 200, "y2": 200}}
        agent._open_job_detail(job)
        check("点的是希望学那张卡", page.clicks, [(400, 462)])

        # 只有括号前那段能对上、公司名却对不上 → 不瞎点，交回模型的卡片框
        page.clicks.clear()
        job2 = {"title": "算法工程师(某某)", "company": "不存在的公司",
                "card_box": {"x1": 100, "y1": 100, "x2": 200, "y2": 200}}
        agent._open_job_detail(job2)
        check("公司对不上就不按文字点", page.clicks, [(240, 135)])


if __name__ == "__main__":
    for fn in (test_filtering_and_ordering, test_filtered_not_repeated,
               test_agent_applies_education_and_wage_overrides,
               test_degree_filter_in_url,
               test_daily_cap_stops, test_dry_run_never_clicks,
               test_card_title_paren_width_mismatch,
               test_platform_quota_text_stops_run,
               test_sources_switch_when_list_ends, test_dry_keyword_moves_to_next_source,
               test_dry_sources_limit_stops_run, test_override_keywords_search_only,
               test_job_type_in_url_skips_filter_clicks, test_recommend_last_puts_search_first,
               test_repeated_cards_do_not_count_as_dry,
               test_card_clicked_by_title_and_company,
               test_filter_menu_closed_after_select, test_scroll_overlaps_screens,
               test_success_popup_closed_by_page_text,
               test_run_limit_independent_of_today_sent,
               
               test_boss_filters_clicked, test_boss_filters_persist_in_url,
               test_filter_misfire_is_detected_and_retried,
               test_filter_button_matched_by_text_not_by_point,
               test_filter_located_by_page_text_first,
               test_filter_gives_up_after_two_misfires,
               test_zoom_local_coords_are_converted,
               test_only_greet_never_chats, test_jump_to_chat_goes_straight_back,
               test_same_job_title, test_greet_switches_detail_panel_first,
               test_greet_skips_when_button_belongs_to_other_job,
               test_button_not_found_skips_not_aborts,
               test_card_box_center_is_used,
               test_whole_screen_greeted_before_moving_on,
               test_greet_retries_once_when_button_not_found,
               test_scroll_targets_left_job_column):
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
