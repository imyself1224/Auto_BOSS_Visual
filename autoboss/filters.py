"""职位筛选规则。

刻意和 agent 分开：这部分是**纯函数、无副作用**，可以脱离浏览器直接单元测试。
薪资解析尤其容易出错（区间、单位、月薪折算日薪、全角字符），必须能测。

用视觉方案拿到的一个额外好处：BOSS 列表页的薪资金额在 DOM 里是**字体加密**的
（私有区字符），而截图里是正常可读的数字，所以这里的解析才有意义。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import preferences

# 全角数字与常见全角符号归一化
_FULLWIDTH = str.maketrans("０１２３４５６７８９．－～／", "0123456789.-~/")

_NUM = r"\d+(?:\.\d+)?"
_MONTHS_RE = re.compile(r"[·×xX*・]?\s*(\d{1,2})\s*薪")

# 判断"这是实习"的兜底关键词（视觉模型给的 job_type 之外再用标题兜一层）
_INTERN_MARKERS = ("实习", "实习生", "intern", "Intern", "INTERN", "暑期", "日常实习")
# 实习卡片标签行特有的写法：到岗天数「4天/周」、实习时长「6个月」
_INTERN_TAG_RE = re.compile(r"\d\s*天\s*/\s*周|\d+\s*个月")

# 爸妈版用：明确不要夜班。两班倒 / 三班倒通常包含夜班，也一并排除。
_NIGHT_SHIFT_MARKERS = ("夜班", "晚班", "夜勤", "两班倒", "三班倒", "倒班", "轮班")


def _norm(text: str | None) -> str:
    return (text or "").translate(_FULLWIDTH).strip()


@dataclass
class Wage:
    """统一折算成**日薪**后的薪资区间。"""
    low: float
    high: float
    basis: str          # 折算依据，写进日志方便核对

    @property
    def median(self) -> float:
        """区间取中位数 —— 这正是用户要求的判定口径。"""
        return (self.low + self.high) / 2.0

    def __str__(self) -> str:
        if abs(self.low - self.high) < 0.5:
            return f"{self.low:.0f}元/天（{self.basis}）"
        return f"{self.low:.0f}–{self.high:.0f}元/天，中位 {self.median:.0f}（{self.basis}）"


def parse_daily_wage(text: str | None, work_days: float = 21.75) -> Wage | None:
    """把 BOSS 上的薪资文本折算成日薪区间。解析不出来返回 None。

    能处理的常见写法：
        300-500元/天        日薪区间
        200元/天            日薪单价
        13-26K·14薪         月薪 + 年终月数，按年化后折算
        6-8K                月薪
        20-30万/年          年薪
    """
    s = _norm(text).replace(" ", "")
    if not s:
        return None

    # 年终月数，如 "·14薪"。先取出来，再从待解析数字里剔除，免得 14 被当成薪资。
    months = 12.0
    m = _MONTHS_RE.search(s)
    if m:
        months = float(m.group(1))
    s_for_nums = _MONTHS_RE.sub("", s)

    nums = [float(x) for x in re.findall(_NUM, s_for_nums)]
    if not nums:
        return None
    nums = nums[:2]                      # 最多取两个，忽略后面的噪声数字
    low, high = (nums[0], nums[0]) if len(nums) == 1 else (nums[0], nums[1])

    lowered = s.lower()

    # 日薪：最直接
    if "天" in s or "/日" in s or "日薪" in s:
        return Wage(low, high, "日薪")

    # 年薪（万）
    if "年" in s or ("万" in s and "月" not in s):
        return Wage(low * 10000 / 12 / work_days,
                    high * 10000 / 12 / work_days,
                    "年薪折算")

    # 月薪（K / 千 / 万）
    if "k" in lowered or "千" in s or "月" in s:
        unit = 10000.0 if "万" in s else 1000.0
        # 年化 = 月薪 × 年终月数；再摊到每月、每个工作日
        return Wage(low * unit * months / 12 / work_days,
                    high * unit * months / 12 / work_days,
                    f"月薪折算（{months:.0f}薪）" if months != 12 else "月薪折算")

    # 没写单位：按量级猜。实习薪资通常写成三位数，月薪通常写成两位数。
    if max(nums) < 1000:
        return Wage(low * 1000 * months / 12 / work_days,
                    high * 1000 * months / 12 / work_days, "按K推断")
    return Wage(low, high, "按日薪推断")


def _intern_evidence(job: dict) -> bool:
    """卡片上有没有看得见的实习证据：标题字样、到岗天数/实习时长标签、按天计薪。"""
    title = str(job.get("title") or "")
    if any(marker in title for marker in _INTERN_MARKERS):
        return True
    tags = " ".join(str(t) for t in (job.get("tags") or []))
    if any(marker in tags for marker in _INTERN_MARKERS) or _INTERN_TAG_RE.search(tags):
        return True
    salary = _norm(job.get("salary"))
    return "天" in salary or "/日" in salary


def looks_like_internship(job: dict) -> bool:
    """是否实习岗。

    优先信视觉模型读到的 job_type，读不到再用标题兜底 ——
    推荐流里"实习生"三个字经常只出现在标题里，没有独立标签。

    但模型说「实习」也要有证据：实测它把华为「算法工程师 20-40K」
    （标签「在校/应届 · 硕士」，其实是校招全职）判成了实习。
    所以模型给了 `tags` 时，标题、标签、薪资单位里至少要有一处实习迹象，
    否则不认。没给 `tags`（旧返回格式）就照旧信 job_type。
    """
    jtype = str(job.get("job_type") or "")
    if "实习" in jtype:
        if "tags" in job and not _intern_evidence(job):
            return False
        return True
    if jtype and ("全职" in jtype or "兼职" in jtype):
        return False
    # 模型说「未知」/ 没给：看卡片上的证据。实测「全栈工程师 300-400元/天 · 4天/周 · 3个月」
    # 这种标题没写实习、但标签和薪资单位一看就是实习的卡，以前全被当成全职滤掉了
    # （一次运行里十几条）。
    return _intern_evidence(job)


# 标题里可能出现「本科及以上」「硕士优先」这类字样；长词优先，避免「本科」吃掉「本科及以上」。
_EDU_WORDS_RE = re.compile(
    "|".join(sorted(("初中及以下", "中专/中技", "本科", "硕士", "博士",
                     "大专", "专科", "高中", "中专", "学士", "研究生"), key=len,
                    reverse=True)))


def job_education(job: dict) -> str:
    """卡片上写的**岗位学历要求**，规范名；读不到返回空串。

    主要看标签行（视觉模型本来就要读标签判断实习），其次职位标题。
    旧版提示词可能给过单独的 `education` 字段，这里仍兼容；现在学历主要由
    BOSS 搜索地址的 degree 参数筛选，这一层只是兜底复核，不额外花模型调用。
    明确写着「学历不限」时返回「不限」（按最低档算），读不到返回空串（不拦）。
    """
    raw = job.get("education")
    if raw:
        got = preferences.normalize_education(raw, allow_above=True)
        if got == "":
            return "不限"
        if got is not None:
            return got
    tags = job.get("tags") or []
    for tag in tags:
        got = preferences.normalize_education(tag, allow_above=True)
        if got == "":
            return "不限"
        if got is not None:
            return got
    m = _EDU_WORDS_RE.search(str(job.get("title") or ""))
    if m:
        return preferences.normalize_education(m.group(0), allow_above=True) or ""
    return ""


@dataclass
class Rules:
    require_internship: bool = True
    # 爸妈版用：明确排除实习岗（和 require_internship 互斥，两个都开时 require 优先）
    exclude_internship: bool = False
    # 爸妈版用：不要夜班（标题 / 标签 / 工作时长里出现夜班、倒班等就跳过）
    exclude_night_shift: bool = False
    min_daily_wage: float = 300.0
    # 月薪下限（元/月）。比较折算后月薪区间的**最低值**；
    # 和 min_daily_wage 可以只开一个，都开时两个都要满足。
    min_monthly_wage: float = 0.0
    work_days_per_month: float = 21.75
    allow_unparsable: bool = False
    require_city: str = "北京"
    # 学历（单个档位，空串 = 不筛）。主筛由 BOSS URL 的 degree 参数完成；
    # 这里只是拿卡片标签复核一遍，读不出学历的放行 —— 和城市读不到不拦一致。
    education: str = ""
    # 无简历模式关掉：不做 fit 判定，硬条件通过就投。
    require_fit: bool = True
    # 档位 → 排序分。模型把岗位跟求职者画像（resume/kb.md）对比后判一个档位
    # （最匹配 / 次匹配 / 可转投 / 不可用）。
    # **表里没有的档位一律不打招呼** —— 所以「不可用」不写进来即可。
    fit_scores: dict[str, float] = field(default_factory=dict)
    # 有门槛的档位：档位 → 日薪区间**最低值**下限。
    # 达到了按本档计分；达不到就降到 fit_fallback 里写的档，没写降档 = 不打招呼。
    # 比如测试：最低 ≥800 按「测试」档（同最匹配）投，800 以下降成「可转投」照投；
    # 产品没写降档，800 以下就不投。
    fit_min_low_wage: dict[str, float] = field(default_factory=dict)
    fit_fallback: dict[str, str] = field(default_factory=dict)
    # 地点软排序：命中这些区就加分，**只影响先后，不排除**。
    # 做成软条件是因为很多岗位只写「北京」不写具体区，硬卡会漏掉一批好机会。
    prefer_districts: list[str] = field(default_factory=list)
    prefer_district_bonus: float = 1.0


@dataclass
class Verdict:
    ok: bool
    score: float
    reasons: list[str]

    def __str__(self) -> str:
        return f"{'✓' if self.ok else '✗'} 分数 {self.score:.1f} —— {'；'.join(self.reasons)}"


def evaluate(job: dict, rules: Rules) -> Verdict:
    """对单个职位卡做出「发不发」的判定，以及排序用的分数。

    两层都**能排除**：
    - 硬条件（实习 / 薪资下限 / 城市）来自 config，不看简历；
    - 档位（`fit`）来自模型拿岗位跟知识库的对比结果 ——
      判不出可投的档位就不发，宁可漏掉也不错发。
    排序分 = 命中档位的权重，只影响先后。
    """
    reasons: list[str] = []
    hard_fail: str | None = None
    score = 0.0

    location = str(job.get("location") or "")
    salary_text = job.get("salary")

    # ---- 硬条件 1：实习 / 非实习 ----
    is_intern = looks_like_internship(job)
    # 排除实习时宁可严一点：模型/标题/标签只要有一处说是实习就不投。
    # （looks_like_internship 为了让「实习」判定有证据，模型说实习但标签没证据时会返回
    #   False；那套宽松规则适合「只投实习」，不适合「不要实习」。）
    intern_claim = (any(marker in str(job.get("job_type") or "") for marker in _INTERN_MARKERS)
                    or any(marker in str(job.get("title") or "") for marker in _INTERN_MARKERS)
                    or any(marker in str(tag) for tag in (job.get("tags") or [])
                           for marker in _INTERN_MARKERS))
    if rules.require_internship and not is_intern:
        hard_fail = f"不是实习岗（类型={job.get('job_type') or '未知'}）"
    elif rules.exclude_internship and (is_intern or intern_claim):
        hard_fail = "是实习岗（无简历模式只投正式岗）"
    else:
        reasons.append("实习岗" if is_intern else "非实习岗")

    # ---- 硬条件 1.5：夜班 / 倒班（爸妈版）----
    if hard_fail is None and rules.exclude_night_shift:
        shift_text = " ".join(str(part) for part in (
            job.get("title") or "",
            job.get("job_type") or "",
            job.get("work_hours") or "",
            " ".join(str(t) for t in (job.get("tags") or [])),
        ))
        hit = next((m for m in _NIGHT_SHIFT_MARKERS if m in shift_text), "")
        if hit:
            hard_fail = f"夜班/倒班岗（出现「{hit}」），设置不要夜班"
        else:
            reasons.append("非夜班")
    # ---- 硬条件 2：学历（兜底复核）----
    # 主筛是 BOSS 搜索地址的 degree 参数；这里拿卡片标签复核，读不出就放行。
    # 「学历不限」按最低档（rank 0）算，和 BOSS 的 URL 行为一致。
    if hard_fail is None and rules.education:
        required = job_education(job)
        if required:
            if preferences.rank_of(required) != preferences.rank_of(rules.education):
                hard_fail = f"要求{required}，不是设置的{rules.education}"
            else:
                reasons.append(f"学历要求{required}")

    # ---- 硬条件 3：薪资下限 ----
    # 日薪、月薪可以只开一个；都开时两个都要满足。
    # allow_unparsable 只负责「读不出薪资」这一种情况，不放过读出来但不够的。
    wage = None
    need_wage = (rules.min_daily_wage > 0
                 or rules.min_monthly_wage > 0
                 or bool(rules.fit_min_low_wage))
    if hard_fail is None:
        if need_wage:
            wage = parse_daily_wage(salary_text, rules.work_days_per_month)

        if rules.min_daily_wage > 0:
            if wage is None:
                if rules.allow_unparsable:
                    reasons.append(f"薪资无法解析，按容忍放行（{salary_text!r}）")
                else:
                    hard_fail = f"薪资无法解析：{salary_text!r}"
            elif wage.median < rules.min_daily_wage:
                hard_fail = (f"日薪中位 {wage.median:.0f} < "
                             f"{rules.min_daily_wage:.0f}（{salary_text}）")
            else:
                reasons.append(f"日薪 {wage}")

        if hard_fail is None and rules.min_monthly_wage > 0:
            if wage is None:
                if rules.allow_unparsable:
                    reasons.append(f"薪资无法解析，按容忍放行（{salary_text!r}）")
                else:
                    hard_fail = f"薪资无法解析：{salary_text!r}"
            else:
                monthly_low = wage.low * rules.work_days_per_month
                if monthly_low < rules.min_monthly_wage:
                    hard_fail = (f"月薪最低 {monthly_low:.0f} < "
                                 f"{rules.min_monthly_wage:.0f}（{salary_text}）")
                else:
                    reasons.append(f"月薪最低 {monthly_low:.0f}（{salary_text}）")

    # ---- 硬条件 4：城市（读不到就不拦，推荐流本身就限定在城市）----
    if hard_fail is None and rules.require_city and location:
        if rules.require_city not in location:
            hard_fail = f"地点不是{rules.require_city}：{location}"

    if hard_fail is not None:
        return Verdict(False, 0.0, [hard_fail])

    # ---- 档位：模型把岗位跟知识库对比后给出的判定 ----
    #
    # 这是"打不打"和"先打谁"的依据。判定不了就不打 —— 跟读不全职位名的
    # 处理一致：宁可漏掉，也不能拿一个没核过的岗位去骚扰 HR。
    # 无简历模式（require_fit=false）跳过这一层，硬条件通过就投。
    if not rules.require_fit:
        reasons.append("无简历模式：硬条件通过即可")
    else:
        fit = str(job.get("fit") or "").strip()
        reason = str(job.get("fit_reason") or "").strip()
        suffix = f"：{reason}" if reason else ""

        if fit in rules.fit_scores:
            tier = fit
            floor = rules.fit_min_low_wage.get(fit)
            if floor is not None and (wage is None or wage.low < float(floor)):
                got = f"最低 {wage.low:.0f}" if wage is not None else "读不出薪资"
                gate = f"「{fit}」日薪最低 {got} < {float(floor):.0f}"
                tier = rules.fit_fallback.get(fit, "")
                if tier not in rules.fit_scores:
                    return Verdict(False, 0.0, [f"{gate}（{salary_text}）{suffix}"])
                reasons.append(f"{gate}，按「{tier}」投")
            score = float(rules.fit_scores[tier])
            reasons.append(f"{fit}{suffix}")
        else:
            return Verdict(False, 0.0, [
                f"没有可投的档位{('（' + fit + '）') if fit else '（模型没给 fit 字段）'}{suffix}"
            ])

    # ---- 地点软排序：只加分，不排除 ----
    district = str(job.get("district") or "") or location
    hit = [k for k in rules.prefer_districts if k and k in district]
    if hit:
        score += rules.prefer_district_bonus
        reasons.append(f"优先区域 {hit[0]}")

    return Verdict(True, score, reasons)
