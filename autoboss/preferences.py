"""QQ 上设置投递偏好：学历、薪资（日薪下限）。

单独一个模块，因为三处共用同一套规则：

* QQ 指令 `/投递 大模型算法 本科 300`、`/学历`、`/薪资` 的解析；
* `run.py bot` 把设定存进 `.qq_prefs.json`，下次任务沿用；
* `greet --education/--min-wage` 和 agent 用同一套规范化。

语义：

* **学历 = 你自己的最高学历**，不是 BOSS 筛选栏里的「学历要求」档位。
  卡片上写的岗位要求高于它就不投；卡片没写学历的照旧放行 ——
  和城市读不到不拦是同一个原则。
* **薪资 = 日薪下限**，口径和 `[job_filter].min_daily_wage` 完全一致（区间取中位数）。
  写月薪（`月薪8000` / `8k`）会按 21.75 个工作日折成日薪。

数字的歧义这样消：裸数字默认当**条数**（`/投递 关键词 10`），保持老习惯；
只有这条指令里同时写了学历、又没指定薪资时，≥40 的数字才按**日薪**算
（`/投递 关键词 本科 300`）。想单独改薪资就写 `薪资300` / `300元/天` / `≥300`；
条数写成 `10条` 永远不会有歧义。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import keywords as kw

# ---------------------------------------------------------------------------
#  学历
# ---------------------------------------------------------------------------
# 档位由低到高。数字只用于比较「岗位要求是否高于我的学历」，不对外展示。
EDUCATION_LEVELS = ("初中及以下", "中专/中技", "高中", "大专", "本科", "硕士", "博士")
EDUCATION_RANK = {name: i + 1 for i, name in enumerate(EDUCATION_LEVELS)}

# 用户和 BOSS 卡片上的各种写法 → 规范名
_EDU_ALIASES = {
    "初中": "初中及以下", "初中及以下": "初中及以下",
    "中专": "中专/中技", "中技": "中专/中技", "中专/中技": "中专/中技",
    "高中": "高中",
    "大专": "大专", "专科": "大专", "高职": "大专",
    "本科": "本科", "学士": "本科", "大学本科": "本科", "bachelor": "本科",
    "硕士": "硕士", "研究生": "硕士", "硕研": "硕士", "master": "硕士",
    "博士": "博士", "博研": "博士", "phd": "博士", "doctor": "博士",
}

# 「不限 / 清空 / 恢复默认」——不按这一项筛
_CLEAR_WORDS = frozenset({
    "不限", "无", "没有要求", "不限制", "不要", "清空", "清除", "重置", "默认",
    "none", "any", "auto",
})

# 前缀：允许「学历本科」「学历=本科」「edu:本科」
_KIND_PREFIX_RE = re.compile(
    r"^(?:最高学历|学历要求|学历|edu(?:cation)?|薪资|日薪|日工资|月薪|工资|薪水|待遇"
    r"|salary|wage|daily|monthly|月|日)\s*[=:：]?\s*")
# 后缀：「本科及以上」是**岗位要求**的写法，不是「我的学历」
_ABOVE_RE = re.compile(r"(?:及以上|以上|起)$")

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．－～／", "0123456789.-~/")


def _clean(text) -> str:
    """规整一段文字：去前缀、去空白和括号、全角转半角。"""
    s = str(text or "").strip().lower().translate(_FULLWIDTH_DIGITS)
    s = s.replace("（", "(").replace("）", ")")
    s = _KIND_PREFIX_RE.sub("", s)
    s = re.sub(r"[\s()（）\[\]【】]", "", s)
    s = re.sub(r"[，,。.;；、]+$", "", s)
    return s


def normalize_education(value, *, allow_above: bool = False) -> str | None:
    """把学历写法规范化成 `EDUCATION_LEVELS` 里的名字。

    返回：
      * 规范名（"本科" / "硕士" …）；
      * `""`  —— 明确「不限 / 清空」；
      * `None` —— 认不出（调用方决定是报错还是当关键词）。

    `allow_above=True` 用于解析**卡片上的岗位要求**（「本科及以上」→ 本科）；
    QQ 指令那边用默认值：那是在填「我的学历」，遇到「及以上」应当提示用户改写法。
    """
    s = _clean(value)
    if not s:
        return None
    if s in _CLEAR_WORDS:
        return ""
    if _ABOVE_RE.search(s):
        if not allow_above:
            return None
        s = _ABOVE_RE.sub("", s)
        if s in _CLEAR_WORDS:
            return ""
    return _EDU_ALIASES.get(s)


def looks_like_education(value) -> bool:
    """像学历、但 normalize 不认（比如「本科及以上」）—— 用来给一句人话提示。"""
    s = _clean(value)
    if not s:
        return False
    if _ABOVE_RE.search(s):
        s = _ABOVE_RE.sub("", s)
    return s in _EDU_ALIASES


def rank_of(value) -> int:
    """学历档位序号，0 = 不限 / 读不出。卡片里写「本科及以上」按本科算。"""
    got = normalize_education(value, allow_above=True) if value else ""
    return EDUCATION_RANK.get(got or "", 0)


# ---------------------------------------------------------------------------
#  薪资
# ---------------------------------------------------------------------------
MIN_DAILY_WAGE = 10.0
MAX_DAILY_WAGE = 5000.0
# 裸数字默认是条数；只有「指令里带了学历、还没定薪资」时，≥ 这个数才算日薪。
BARE_NUMBER_AS_WAGE = 40
MAX_LIMIT = 500
WORK_DAYS_PER_MONTH = 21.75

_NUM = r"\d+(?:\.\d+)?"
_UNIT = r"(k|千|万|w)?"

# 「月薪 8000」「8000/月」「8k」——先折成月薪，再摊到工作日
_MONTH_PATTERNS = (
    re.compile(r"^(?:月薪|月工资|月收入|月|month|monthly|salary)[:=]?(" + _NUM + r")"
               + _UNIT + r"(?:元|块)?$"),
    re.compile(r"^(" + _NUM + r")" + _UNIT + r"(?:元|块)?(?:/|每)?月$"),
    # 只认带单位（8k / 1.5万）的：裸数字「300」是日薪，不能当成年月薪去折
    re.compile(r"^(" + _NUM + r")(k|千|万|w)$"),
)
# 「日薪300」「300元/天」「300/天」「≥300」「300以上」「300-500元/天」
_DAILY_PATTERNS = (
    re.compile(r"^(?:日薪|日工资|日结|薪资|工资|薪水|待遇|daily|wage|day)[:=]?(" + _NUM + r")$"),
    re.compile(r"^(" + _NUM + r")[-~](" + _NUM + r")(?:元)?(?:/|每)?(?:天|日)?$"),
    re.compile(r"^(" + _NUM + r")(?:元|块)?(?:/|每)?(?:天|日)$"),
    re.compile(r"^(" + _NUM + r")(?:元|块)$"),
)


def _to_monthly(number: float, unit: str | None) -> float:
    """带 K/千/万 的月薪换算成元/月。"""
    unit = (unit or "").lower()
    if unit in ("k", "千"):
        return number * 1000
    if unit in ("万", "w"):
        return number * 10000
    return number


def _explicit_wage(text) -> float | None:
    """解析**明确写了单位/前缀**的薪资，返回日薪下限。裸数字不在这里处理。"""
    s = str(text or "").strip().lower().translate(_FULLWIDTH_DIGITS)
    s = re.sub(r"\s+", "", s)
    if not s:
        return None
    flagged = False
    stripped = re.sub(r"^(?:≥|>=|不低于|至少|最低|高于)", "", s)
    if stripped != s:
        flagged = True
    s = stripped
    stripped = re.sub(r"(?:以上|\+)$", "", s)
    if stripped != s:
        flagged = True
    s = stripped

    for pattern in _MONTH_PATTERNS:
        m = pattern.fullmatch(s)
        if m:
            monthly = _to_monthly(float(m.group(1)), m.group(2))
            return round(monthly / WORK_DAYS_PER_MONTH, 1)

    for pattern in _DAILY_PATTERNS:
        m = pattern.fullmatch(s)
        if m:
            return float(m.group(1))

    # 「≥300」「300以上」「300+」：标记过是薪资，但后面只剩一个裸数字
    if flagged and _BARE_NUM_RE.fullmatch(s):
        return float(s)
    return None


def parse_wage(text) -> float | None:
    """解析 `/薪资` 的参数：明确写法 + 裸数字都认。认不出返回 None。"""
    value = _explicit_wage(text)
    if value is not None:
        return value
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(text or ""))
    return float(m.group(1)) if m else None


def wage_problem(value: float | None) -> str:
    """日薪是否离谱；没问题（含 0 = 不限）返回空串。"""
    if value is None:
        return "看不懂这个薪资，可以写 300 / 日薪300 / 月薪8000 / 不限"
    if value == 0:
        return ""                     # 0 = 明确不限
    if value < MIN_DAILY_WAGE:
        return f"日薪 {value:g} 元/天太小了（最低按 {MIN_DAILY_WAGE:g} 算）"
    if value > MAX_DAILY_WAGE:
        return (f"日薪 {value:g} 元/天太大了，想写月薪就写「月薪{value:g}」"
                f"（上限按 {MAX_DAILY_WAGE:g} 元/天算）")
    return ""


# ---------------------------------------------------------------------------
#  一条指令的解析结果
# ---------------------------------------------------------------------------
@dataclass
class TaskArgs:
    """`/投递 ...` / `/演练 ...` 解析出来的东西。

    `education` / `min_wage` 用 None 表示「这条指令没提」，空串 / 0 表示「明确不限」。
    """
    keywords: list[str] = None            # type: ignore[assignment]
    limit: int | None = None
    education: str | None = None
    min_wage: float | None = None
    errors: list[str] = None              # type: ignore[assignment]

    def __post_init__(self):
        self.keywords = list(self.keywords or [])
        self.errors = list(self.errors or [])

    @property
    def touched(self) -> bool:
        """这条指令有没有动学历 / 薪资设置。"""
        return self.education is not None or self.min_wage is not None


def _clear_target(piece: str) -> str:
    """这一节是不是「不限 / 清空」？返回 both / education / wage / ""。"""
    s = str(piece).strip().lower().translate(_FULLWIDTH_DIGITS)
    if not s:
        return ""
    core = _KIND_PREFIX_RE.sub("", s)
    core = re.sub(r"[\s()（）]", "", core)
    if core not in _CLEAR_WORDS:
        return ""
    has_edu = bool(re.match(r"^(?:最高学历|学历要求|学历|edu(?:cation)?)", s))
    has_wage = bool(re.match(r"^(?:薪资|日薪|日工资|月薪|工资|薪水|待遇|salary|wage|daily|monthly)",
                             s))
    if has_edu and not has_wage:
        return "education"
    if has_wage and not has_edu:
        return "wage"
    return "both"


def is_clear(text) -> bool:
    """是不是「不限 / 清空 / 恢复默认」这类写法。"""
    return bool(_clear_target(text))


_COUNT_RE = re.compile(r"^(\d+)(条|个|份|次)?$")
_BARE_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")

# 可以单独写、把下一个词当值的标记：「/投递 岗位 学历 本科 薪资 300」
_MARK_EDUCATION = frozenset({"学历", "学历要求", "edu", "education"})
_MARK_WAGE = frozenset({"薪资", "日薪", "工资", "薪水", "待遇", "salary", "wage", "daily"})
_MARK_MONTH = frozenset({"月薪", "月工资", "monthly"})

_MONTH_INPUT_RE = re.compile(r"^(" + _NUM + r")" + _UNIT + r"(?:元|块)?$")


def _monthly_to_daily(text) -> float | None:
    """「8000」「8k」「1.5万」→ 日薪。"""
    m = _MONTH_INPUT_RE.fullmatch(str(text or "").strip().lower().translate(_FULLWIDTH_DIGITS))
    if not m:
        return None
    return round(_to_monthly(float(m.group(1)), m.group(2)) / WORK_DAYS_PER_MONTH, 1)


def parse_task_args(tokens) -> TaskArgs:
    """把 `/投递` 后面的参数拆成 关键词 / 条数 / 学历 / 日薪。

    例：
        「大模型算法 本科 300」 → 关键词[大模型算法]，学历本科，日薪 300
        「大模型算法 10」       → 关键词[大模型算法]，最多 10 条
        「10条 本科 薪资300」   → 10 条，学历本科，日薪 300
        「岗位 学历 本科 薪资 300」→ 标记和值分开写也认
    """
    args = TaskArgs()
    if isinstance(tokens, str):
        tokens = [tokens]
    pending: str | None = None          # education / wage / month
    numbers: list[tuple[int, bool]] = []   # 裸数字推迟到最后再定性（要看指令里有没有学历）
    for raw in (tokens or []):
        for piece in kw.SPLIT_RE.split(str(raw)):
            piece = piece.strip()
            if not piece:
                continue
            lowered = piece.lower()

            # 上一个是「学历 / 薪资 / 月薪」这种光秃秃的标记：这一节就是它的值
            if pending == "education":
                value = normalize_education(piece)
                if value is None:
                    if looks_like_education(piece):
                        args.errors.append(
                            f"「{piece}」是岗位要求的写法；学历填你自己的最高学历，"
                            "比如 本科 / 硕士")
                    else:
                        args.errors.append(
                            f"「{piece}」不是一个学历，可写 不限 / 大专 / 本科 / 硕士 / 博士")
                else:
                    args.education = value
                pending = None
                continue
            if pending in ("wage", "month"):
                if _clear_target(piece):
                    args.min_wage = 0.0
                else:
                    wage = _monthly_to_daily(piece) if pending == "month" else parse_wage(piece)
                    problem = wage_problem(wage)
                    if problem:
                        args.errors.append(problem)
                    else:
                        args.min_wage = wage
                pending = None
                continue
            if lowered in _MARK_EDUCATION:
                pending = "education"
                continue
            if lowered in _MARK_MONTH:
                pending = "month"
                continue
            if lowered in _MARK_WAGE:
                pending = "wage"
                continue

            target = _clear_target(piece)
            if target:
                if target in ("both", "education"):
                    args.education = ""
                if target in ("both", "wage"):
                    args.min_wage = 0.0
                continue

            wage = _explicit_wage(piece)
            if wage is not None:
                problem = wage_problem(wage)
                if problem:
                    args.errors.append(problem)
                else:
                    args.min_wage = wage
                continue

            education = normalize_education(piece)
            if education is not None:
                args.education = education
                continue
            if looks_like_education(piece):
                args.errors.append(
                    f"「{piece}」是岗位要求的写法；学历填你自己的最高学历，"
                    "比如 本科 / 硕士")
                continue

            m = _COUNT_RE.fullmatch(piece)
            if m:
                numbers.append((int(m.group(1)), m.group(2) is not None))
                continue

            if _BARE_NUM_RE.fullmatch(piece):
                # 只可能是条数（>=40 的已在上面处理）
                args.errors.append(f"「{piece}」不像条数也不像日薪")
                continue

            args.keywords.append(piece)

    if pending is not None:
        name = {"education": "学历", "wage": "薪资", "month": "月薪"}[pending]
        args.errors.append(f"「{name}」后面没写值")

    # 裸数字的归属：默认是条数（老习惯）；只有这条指令里写了学历、又还没定薪资时，
    # ≥40 的那个数才当日薪 —— 这样「/投递 岗位 本科 300」和「/投递 岗位 300」都对。
    for number, explicit_count in numbers:
        if number <= 0:
            args.errors.append("条数要大于 0")
        elif (number < BARE_NUMBER_AS_WAGE or explicit_count
              or args.education is None or args.min_wage is not None):
            if number > MAX_LIMIT:
                if explicit_count:
                    args.errors.append(f"一次最多投 {MAX_LIMIT} 条")
                else:
                    args.errors.append(
                        f"「{number}」既可能是条数也可能是日薪：条数最多 {MAX_LIMIT}，"
                        f"写「{number}条」或「薪资{number:g}」说清楚")
            else:
                args.limit = number
        elif number > MAX_DAILY_WAGE:
            args.errors.append(
                f"日薪 {number:g} 元/天太大了；想写月薪就写「月薪{number:g}」，"
                f"想写条数就写「{number}条」")
        else:
            args.min_wage = float(number)

    args.keywords = kw.normalize(args.keywords)
    return args


# ---------------------------------------------------------------------------
#  持久化：QQ 上设定的值存在项目根目录的 .qq_prefs.json
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Prefs:
    """QQ 上设定的投递偏好。None = 没设过（按 config.toml），"" / 0 = 明确不限。"""
    education: str | None = None
    min_wage: float | None = None

    def merged(self, args: TaskArgs) -> "Prefs":
        education = self.education if args.education is None else args.education
        min_wage = self.min_wage if args.min_wage is None else args.min_wage
        return Prefs(education=education, min_wage=min_wage)

    def is_empty(self) -> bool:
        return self.education is None and self.min_wage is None


def load_prefs(path) -> Prefs:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return Prefs()
    if not isinstance(data, dict):
        return Prefs()

    education: str | None = None
    raw_education = data.get("education")
    if raw_education is not None:
        # 空串是「明确不限」，不能当成「没设置」丢掉
        if str(raw_education).strip() == "":
            education = ""
        else:
            education = normalize_education(raw_education)
    min_wage: float | None = None
    raw_wage = data.get("min_wage")
    if raw_wage is not None:
        try:
            min_wage = float(raw_wage)
        except (TypeError, ValueError):
            min_wage = None
        if min_wage is not None and min_wage < 0:
            min_wage = None
    return Prefs(education=education, min_wage=min_wage)


def save_prefs(path, prefs: Prefs) -> None:
    """落盘。两项都没设就把文件删掉，项目目录保持干净。"""
    path = Path(path)
    if prefs.is_empty():
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"education": prefs.education, "min_wage": prefs.min_wage},
                         ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
#  给 QQ 回复用的文字
# ---------------------------------------------------------------------------
def describe(prefs: Prefs) -> str:
    """两行：学历、日薪。没设过的项说明按 config.toml。"""
    if prefs.education is None:
        education = "未设置（按 config.toml）"
    elif prefs.education == "":
        education = "不限"
    else:
        education = f"{prefs.education}（岗位要求高于它的不投）"

    if prefs.min_wage is None:
        wage = "未设置（按 config.toml）"
    elif not prefs.min_wage:
        wage = "不限"
    else:
        wage = f"≥{prefs.min_wage:g} 元/天（区间取中位数）"

    return f"🎓 学历：{education}\n💰 日薪：{wage}"


def status_line(prefs: Prefs) -> str:
    """一行版，塞进「/状态」里。"""
    if prefs.is_empty():
        return "🎓 学历/薪资：按 config.toml"
    education = ("不限" if prefs.education == "" else prefs.education) \
        if prefs.education is not None else "config"
    wage = ("不限" if not prefs.min_wage else f"≥{prefs.min_wage:g}") \
        if prefs.min_wage is not None else "config"
    return f"🎓 学历 {education} · 💰 日薪 {wage}"
