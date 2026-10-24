"""检索关键词：解析、来源编排、展示文字。

拆成单独一个模块，是因为三处都要用同一套规则：

* `GreetAgent` 决定**这次任务要走哪些来源**；
* `run.py greet` 在任务开始前把关键词推给 QQ；
* `run.py bot` 解析「/投递 大模型算法 Agent」这种指令。

以前这段逻辑（`_sources`）只活在 agent 里，QQ 那边想知道"这次搜什么"
只能抄一份配置解析；抄两份迟早会对不上。
"""
from __future__ import annotations

import re

# 一次任务最多搜几个关键词。QQ 上发太长的一串多半是手滑，截断比照单全收好。
MAX_KEYWORDS = 20

# 关键词之间的分隔符：空格、逗号、顿号、分号、竖线都算。
# `/投递 大模型算法、Agent,RAG` 和 `/投递 大模型算法 Agent RAG` 等价。
SPLIT_RE = re.compile(r"[|,，、;；\s]+")


def normalize(items) -> list[str]:
    """去空白、去重、截断到 MAX_KEYWORDS。接受任何可迭代对象（字符串会先拆）。"""
    if isinstance(items, str):
        items = SPLIT_RE.split(items)
    out: list[str] = []
    for item in items or []:
        word = str(item).strip()
        if word and word not in out:
            out.append(word)
            if len(out) >= MAX_KEYWORDS:
                break
    return out


def split_keywords(raw) -> list[str]:
    """把用户输入拆成关键词列表。

    `"大模型算法、Agent,RAG"` → `["大模型算法", "Agent", "RAG"]`；
    传列表（比如 CLI 已经拆好的）就直接去重。
    """
    if isinstance(raw, (list, tuple, set)):
        return normalize(raw)
    return normalize(SPLIT_RE.split(str(raw or "")))


def config_queries(cfg) -> list[str]:
    """config.toml 里的 [search].queries；为空时退回旧的单关键词写法。"""
    raw = cfg.get("search.queries", []) or []
    # 正常是 TOML 数组；写成字符串（"A,B"）也认，免得被逐字拆成一个个汉字
    queries = (split_keywords(raw) if isinstance(raw, str)
               else [str(q).strip() for q in raw if str(q).strip()])
    if not queries:
        single = str(cfg.get("search.query", "") or "").strip()
        queries = [single] if single else []
    return queries


def planned_sources(cfg, override=None, mode: str | None = None) -> list[tuple[str, str]]:
    """本轮要走的职位来源，按先后顺序：[(kind, query), ...]。

    kind 有三种：
      search    —— 用关键词搜（query 是关键词）
      recommend —— BOSS 的推荐流
      expect    —— 推荐旁边的求职期望标签（如「算法工程师(北京)」）

    `override` 非空表示用户在 QQ / 命令行上临时指定了关键词：
    这时**只搜这些词**，不掺推荐流 —— 用户要什么就搜什么，别自作主张。

    `mode` 默认取 `[source].mode`；agent 会把自己的（可能已被覆盖的）模式传进来。
    """
    words = split_keywords(override) if override else []
    forced = bool(words)
    queries = words or config_queries(cfg)

    if forced:
        return [("search", q) for q in queries]

    mode = str(mode or cfg.get("source.mode", "recommend")).lower()
    rec: list[tuple[str, str]] = []
    if mode in ("recommend", "both"):
        rec.append(("recommend", ""))
        rec.extend(("expect", str(t).strip())
                   for t in (cfg.get("source.expect_tabs", []) or []) if str(t).strip())
    search = [("search", q) for q in queries] if mode in ("search", "both") else []
    # 实测推荐流（加实习筛选后）前排多是不对口的，关键词搜索一屏四条全对口，
    # 所以 recommend_last=true 时先搜关键词、推荐流最后补漏。
    out = search + rec if cfg.get("source.recommend_last", False) else rec + search
    return out or [("recommend", "")]


def label(kind: str, query: str) -> str:
    """来源的短名字，日志和 QQ 消息共用。"""
    return {"recommend": "推荐流", "expect": f"「{query}」标签"}.get(kind, f"搜「{query}」")


def queries_of(sources) -> list[str]:
    """来源列表里的关键词（去掉推荐流/标签）。"""
    return [q for kind, q in sources if kind == "search"]


def block(words: list[str], per_line: int = 8) -> str:
    """把关键词排成每行 per_line 个，QQ 上不会挤成一大坨。"""
    return "\n".join("、".join(words[i:i + per_line])
                     for i in range(0, len(words), per_line))
