"""QQ 上设置投递偏好：学历（单个档位）、薪资（日薪下限）。

单独一个模块，因为三处共用同一套规则：

* QQ 指令 `/学历 硕士`、`/薪资 300` 的解析；
* `run.py bot` 把设定存进 `.qq_prefs.json`，下次任务沿用；
* `greet --education 硕士 --min-wage 300` 和 agent 用同一套规范化。

语义：

* **学历只设一个档位**（`/学历 硕士`），拼成 BOSS 搜索地址的 `degree` 参数
  （硕士 = `204`），由平台在 URL 层筛，不花模型调用。
  `/学历 不限` = 不带 degree 参数、不按学历筛。
  设了具体档位后 BOSS 会排除写着「学历不限」的岗位（平台原生行为）。
* **薪资 = 日薪下限**，口径和 `[job_filter].min_daily_wage` 完全一致（区间取中位数）。
  BOSS 的薪资档位只有月薪、没有日薪档，所以这项由程序按卡片薪资精确比较。
  写月薪（`月薪8000` / `8k`）会按 21.75 个工作日折成日薪。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
#  学历
# ---------------------------------------------------------------------------
# 档位由低到高。数字只用于比较学历高低，不对外展示。
EDUCATION_LEVELS = ("初中及以下", "中专/中技", "高中", "大专", "本科", "硕士", "博士")
EDUCATION_RANK = {name: i + 1 for i, name in enumerate(EDUCATION_LEVELS)}

# BOSS 搜索 URL 里 degree 参数的取值（/wapi/zpgeek/pc/all/filter/conditions.json）。
# 多个档位用逗号连接，实测 degree=203,204 只返回本科和硕士。
DEGREE_CODES = {
    "初中及以下": "209", "中专/中技": "208", "高中": "206",
    "大专": "202", "本科": "203", "硕士": "204", "博士": "205",
}

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

# 「不限 / 清空 / 恢复默认」——这一项不设限
_CLEAR_WORDS = frozenset({
    "不限", "无", "没有要求", "不限制", "不要", "清空", "清除", "重置", "默认",
    "none", "any", "auto",
})

# 前缀：允许「学历本科」「学历=本科」「edu:本科」
_KIND_PREFIX_RE = re.compile(
    r"^(?:最高学历|最低学历|学历要求|学历|edu(?:cation)?|薪资|日薪|日工资|月薪|工资|薪水|待遇"
    r"|salary|wage|daily|monthly|月|日)\s*[=:：]?\s*")
# 后缀：「本科及以上」是**岗位要求**的写法，不是学历档位
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

    `allow_above=True` 用于解析**岗位要求**（「本科及以上」→ 本科）；
    QQ 指令那边用默认值：那是在填一个学历档位，遇到「及以上」应当提示用户改写法。
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


def education_label(value: str | None) -> str:
    """人话展示：None = 未设置（按 config），"" = 不限，其他 = 档位。"""
    if value is None:
        return "未设置（按 config.toml）"
    return value or "不限"


def degree_param(education: str = "") -> str:
    """把学历换成一个 BOSS URL 的 degree 代码；「不限」返回空串。"""
    value = normalize_education(education, allow_above=True) or ""
    return DEGREE_CODES.get(value, "")


def parse_education(value) -> tuple[str | None, str]:
    """解析 `/学历` 的一个参数：返回 (规范值, 错误提示)。

    规范值 `""` = 不限；`None` = 认不出（第二项是给用户看的原因）。
    """
    got = normalize_education(value)
    if got is not None:
        return got, ""
    if looks_like_education(value):
        return None, (f"「{value}」是岗位要求的写法；写一个档位即可"
                      "（不限 / 大专 / 本科 / 硕士 / 博士）")
    return None, f"看不懂这个学历「{value}」（可写 不限 / 大专 / 本科 / 硕士 / 博士）"


def education_from_config(cfg) -> str:
    """config.toml 里的学历（单个档位）；兼容旧的 education_min/max、max_education。

    旧配置是区间时，优先取上限：新语义只有一个档位，上限最接近原来的「不超过」。
    上限写着「不限」就返回空串（不按学历筛）。
    """
    raw = cfg.get("job_filter.education", "")
    if raw is not None and str(raw).strip() != "":
        return normalize_education(raw, allow_above=True) or ""
    for key in ("education_max", "education_min", "max_education"):
        raw = cfg.get(f"job_filter.{key}", "")
        if raw is not None and str(raw).strip() != "":
            return normalize_education(raw, allow_above=True) or ""
    return ""


def is_clear(text) -> bool:
    """是不是「不限 / 清空 / 恢复默认」这类写法。"""
    return _clean(text) in _CLEAR_WORDS


# ---------------------------------------------------------------------------
#  薪资
# ---------------------------------------------------------------------------
MIN_DAILY_WAGE = 10.0
MAX_DAILY_WAGE = 5000.0
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
_BARE_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


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
#  持久化：QQ 上设定的值存在项目根目录的 .qq_prefs.json
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Prefs:
    """QQ 上设定的投递偏好。

    `education`：`None` = 没设过（按 config.toml），`""` = 不限，其他 = 单个档位。
    `min_wage`：`None` = 没设过，`0` = 明确不限。
    """
    education: str | None = None
    min_wage: float | None = None

    def with_education(self, value: str) -> "Prefs":
        return Prefs(education=value, min_wage=self.min_wage)

    def with_wage(self, min_wage: float) -> "Prefs":
        return Prefs(education=self.education, min_wage=min_wage)

    def is_empty(self) -> bool:
        return self.education is None and self.min_wage is None


def _migrate_education(raw) -> str | None:
    """兼容旧版存盘格式：字符串 / (最低, 最高) 数组。

    旧版是「区间」；新版只保留一个档位，优先取上限（最接近原来的「不超过」）。
    空串是「明确不限」，不能当成「没设置」丢掉。
    """
    if isinstance(raw, str):
        if raw.strip() == "":
            return ""
        return normalize_education(raw)
    if isinstance(raw, (list, tuple)):
        values: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip() == "":
                values.append("")
                continue
            got = normalize_education(item)
            if got is not None:
                values.append(got)
        if not values:
            return None
        return max(values, key=rank_of)
    return None


def load_prefs(path) -> Prefs:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return Prefs()
    if not isinstance(data, dict):
        return Prefs()

    education = _migrate_education(data.get("education"))

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
    payload = json.dumps({
        "education": prefs.education,
        "min_wage": prefs.min_wage,
    }, ensure_ascii=False, indent=2)
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
    else:
        education = f"{prefs.education or '不限'}（QQ 设定）"

    if prefs.min_wage is None:
        wage = "未设置（按 config.toml）"
    elif not prefs.min_wage:
        wage = "不限"
    else:
        wage = f"≥{prefs.min_wage:g} 元/天（区间取中位数）"

    return f"🎓 学历要求：{education}\n💰 日薪：{wage}"


def status_line(prefs: Prefs) -> str:
    """一行版，塞进「/状态」里。"""
    if prefs.is_empty():
        return "🎓 学历/薪资：按 config.toml"
    if prefs.education is None:
        education = "config"
    else:
        education = prefs.education or "不限"
    if prefs.min_wage is None:
        wage = "config"
    elif not prefs.min_wage:
        wage = "不限"
    else:
        wage = f"≥{prefs.min_wage:g}"
    return f"🎓 学历 {education} · 💰 日薪 {wage}"
