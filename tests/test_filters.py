"""筛选规则的测试。不依赖 pytest，直接 `python tests/test_filters.py` 跑。

    python tests/test_filters.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoboss.filters import (  # noqa: E402
    Rules, evaluate, looks_like_internship, parse_daily_wage,
)

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want, tol: float = 1.0):
    global PASSED
    ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) else (got == want)
    if ok:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 得到 {got!r}，期望 {want!r}")


def check_true(name: str, cond: bool):
    check(name, bool(cond), True)


def check_none(name: str, value):
    check(name, value, None)


# ===========================================================================
# 薪资解析
# ===========================================================================
def test_wage():
    w = parse_daily_wage("300-500元/天")
    check("日薪区间 low", w.low, 300)
    check("日薪区间 high", w.high, 500)
    check("日薪区间中位", w.median, 400)

    w = parse_daily_wage("200元/天")
    check("日薪单价", w.median, 200)
    check_true("日薪单价字段名", "日薪" == w.basis)

    w = parse_daily_wage("700-800元/天")
    check("日薪高区间", w.median, 750)

    # 全角字符
    w = parse_daily_wage("４００元／天")
    check("全角数字", w.median, 400)

    # 月薪 → 日薪：6000/21.75 = 275.86, 8000/21.75 = 367.82
    w = parse_daily_wage("6-8K")
    check("月薪低", w.low, 6000 / 21.75)
    check("月薪高中位", w.median, (6000 / 21.75 + 8000 / 21.75) / 2)

    # 13K×14薪 年化 → 13000*14/12/21.75
    w = parse_daily_wage("13-26K·14薪")
    check("14薪低", w.low, 13000 * 14 / 12 / 21.75)
    check("14薪高", w.high, 26000 * 14 / 12 / 21.75)
    check_true("14薪不把14当薪资", w.low > 600)   # 若误把 14 当年终月数会算出离谱值

    # 年薪
    w = parse_daily_wage("20-30万/年")
    check("年薪低", w.low, 200000 / 12 / 21.75)

    # 解析不了
    check_none("面议", parse_daily_wage("面议"))
    check_none("空字符串", parse_daily_wage(""))
    check_none("None", parse_daily_wage(None))
    check_none("纯文字", parse_daily_wage("薪资优厚"))


# ===========================================================================
# 实习判定
# ===========================================================================
def test_internship():
    check_true("job_type=实习", looks_like_internship({"job_type": "实习"}))
    check_true("标题含实习生", looks_like_internship({"title": "算法实习生"}))
    check_true("标题含暑期", looks_like_internship({"title": "暑期算法工程师"}))
    check_true("job_type 缺失靠标题", looks_like_internship({"title": "推荐算法 Intern"}))
    check("全职被否", looks_like_internship({"job_type": "全职", "title": "算法工程师"}), False)
    check("无关岗位被否", looks_like_internship({"title": "前端开发工程师"}), False)
    check_true("类型未知但有到岗天数 + 按天计薪算实习", looks_like_internship(
        {"title": "全栈工程师", "job_type": "未知", "salary": "300-400元/天",
         "tags": ["4天/周", "3个月", "本科"]}))
    check("类型未知、毫无实习迹象仍否", looks_like_internship(
        {"title": "大模型算法工程师", "job_type": "未知", "salary": "20-30K",
         "tags": ["3-5年", "硕士"]}), False)
    # 校招全职被模型误标成实习（实测：华为「算法工程师 20-40K」，标签 在校/应届·硕士）
    check("校招被模型误标实习也否", looks_like_internship(
        {"job_type": "实习", "title": "算法工程师", "salary": "20-40K",
         "tags": ["在校/应届", "硕士"]}), False)
    check_true("标签有到岗天数算实习", looks_like_internship(
        {"job_type": "实习", "title": "算法工程师", "salary": "6-8K",
         "tags": ["4天/周", "6个月", "硕士"]}))
    check_true("按天计薪算实习", looks_like_internship(
        {"job_type": "实习", "title": "算法工程师", "salary": "500-1000元/天", "tags": []}))


# ===========================================================================
# 端到端判定
# ===========================================================================
RULES = Rules(
    require_internship=True,
    min_daily_wage=300,
    require_city="北京",
    fit_scores={"最匹配": 3.0, "次匹配": 2.0, "可转投": 1.0},
    prefer_districts=["海淀区", "海淀"],
    prefer_district_bonus=1.0,
)


# 一个"硬条件都过"的底子。各用例只改自己关心的那一项。
# 默认放在朝阳区 —— 不为海淀，这样档位分的断言是干净的整数。
def _job(**kw) -> dict:
    base = {
        "title": "推荐算法实习生", "company": "某某科技",
        "salary": "400-500元/天", "location": "北京·朝阳区", "district": "朝阳区",
        "job_type": "实习", "fit": "最匹配",
    }
    base.update(kw)
    return base


def test_evaluate():
    # ---- 档位：模型拿岗位跟知识库对比的结果 ----
    # 它既决定排序分，也决定发不发（判不出可投的档位就不发，宁可漏掉不错发）。

    v = evaluate(_job(fit="最匹配"), RULES)
    check_true("最匹配放行", v.ok)
    check("最匹配得分 3", v.score, 3.0)
    check_true("理由里带档位", any("最匹配" in r for r in v.reasons))

    v = evaluate(_job(fit="次匹配"), RULES)
    check_true("次匹配放行", v.ok)
    check("次匹配得分 2", v.score, 2.0)

    v = evaluate(_job(fit="可转投"), RULES)
    check_true("可转投放行", v.ok)
    check("可转投得分 1", v.score, 1.0)

    # 产品 / 测试：日薪区间**最低值** ≥800 按本档（同最匹配）投；
    # 不够时测试降成可转投照投，产品不投
    gated = Rules(require_city="北京",
                  fit_scores={"最匹配": 3.0, "可转投": 1.0, "产品": 3.0, "测试": 3.0},
                  fit_min_low_wage={"产品": 800, "测试": 800},
                  fit_fallback={"测试": "可转投"})
    v = evaluate(_job(title="AI产品经理实习生", fit="产品", salary="800-1000元/天"), gated)
    check_true("产品 800-1000 放行", v.ok)
    check("产品高薪得分同最匹配", v.score, 3.0)
    v = evaluate(_job(title="产品实习生", fit="产品", salary="600-900元/天"), gated)
    check("产品 600-900 最低值不够被拒", v.ok, False)
    check_true("拒绝理由写明门槛", any("最低 600 < 800" in r for r in v.reasons))
    v = evaluate(_job(title="测试开发实习生", fit="测试", salary="800-900元/天"), gated)
    check("测试高薪得分同最匹配", (v.ok, v.score), (True, 3.0))
    v = evaluate(_job(title="测试开发实习生", fit="测试", salary="400-500元/天"), gated)
    check("测试 800 以下降成可转投", (v.ok, v.score), (True, 1.0))
    check_true("降档写进理由", any("按「可转投」投" in r for r in v.reasons))
    v = evaluate(_job(title="测试实习生", fit="测试", salary="200-300元/天"), gated)
    check("降档后日薪中位下限照样生效", v.ok, False)
    v = evaluate(_job(title="产品实习生", fit="产品", salary="面议"),
                 Rules(require_city="北京", allow_unparsable=True,
                       fit_scores={"产品": 3.0}, fit_min_low_wage={"产品": 800}))
    check("读不出薪资的产品岗不投", v.ok, False)
    check_true("门槛不影响其他档位",
               evaluate(_job(fit="最匹配", salary="400-500元/天"), gated).ok)

    # 模型判定不可用 → 不发
    v = evaluate(_job(fit="不可用"), RULES)
    check("不可用被拒", v.ok, False)
    check_true("拒绝理由提到档位", any("没有可投的档位" in r for r in v.reasons))

    # 模型压根没给 fit → 同样不发（跟"读不全职位名就跳过"一个原则）
    v = evaluate(_job(fit=None), RULES)
    check("缺 fit 被拒", v.ok, False)
    check_true("缺 fit 的理由可辨认", any("没给 fit" in r for r in v.reasons))

    # 模型给了个没见过的档位 → 不发，但理由里能看到它给了什么
    v = evaluate(_job(fit="也许可以"), RULES)
    check("未知档位被拒", v.ok, False)
    check_true("未知档位的理由里带上原值", any("也许可以" in r for r in v.reasons))

    # 排序：最匹配 > 次匹配 > 可转投
    hi = evaluate(_job(fit="最匹配"), RULES)
    mid = evaluate(_job(fit="次匹配"), RULES)
    lo = evaluate(_job(fit="可转投"), RULES)
    check_true("档位排序正确", hi.score > mid.score > lo.score)

    # ---- 硬条件（不看知识库，只看卡面）----

    # 薪资不达标
    v = evaluate(_job(salary="150-200元/天"), RULES)
    check("日薪不足被拒", v.ok, False)
    check_true("拒绝理由提到日薪", any("日薪中位" in r for r in v.reasons))

    # 区间中位数刚好等于 300 → 应该放行（"不低于300"）
    v = evaluate(_job(salary="250-350元/天", location="北京"), RULES)
    check_true("中位数=300 应放行", v.ok)

    # 中位数 299 应拒
    v = evaluate(_job(salary="250-348元/天", location="北京"), RULES)
    check("中位数<300 应拒", v.ok, False)

    # 非实习
    v = evaluate(_job(title="算法工程师", salary="30-50K·16薪", job_type="全职"), RULES)
    check("全职被拒", v.ok, False)
    check_true("理由提到实习", any("实习" in r for r in v.reasons))

    # 外地
    v = evaluate(_job(salary="400元/天", location="上海·浦东新区"), RULES)
    check("外地被拒", v.ok, False)

    # 薪资解析不出 → 默认拒
    v = evaluate(_job(salary="面议", location="北京"), RULES)
    check("面议默认被拒", v.ok, False)

    # 但配置容忍时放行
    lenient = Rules(**{**RULES.__dict__, "allow_unparsable": True})
    v = evaluate(_job(salary="面议", location="北京"), lenient)
    check_true("容忍模式下面议放行", v.ok)

    # ---- 地点软排序：只加分，不排除 ----

    # 非海淀的北京岗位照样放行（地点从来不是硬条件）
    v = evaluate(_job(location="北京·昌平区", district="昌平区"), RULES)
    check_true("非海淀的北京岗位一样放行", v.ok)

    # 同档位下，海淀的排在前面
    haidian = evaluate(_job(district="海淀区", location="北京·海淀区"), RULES)
    chaoyang = evaluate(_job(district="朝阳区", location="北京·朝阳区"), RULES)
    check_true("海淀命中优先区域", any("优先区域" in r for r in haidian.reasons))
    check_true("海淀分数更高", haidian.score > chaoyang.score)
    check("海淀加分幅度", haidian.score - chaoyang.score, RULES.prefer_district_bonus)

    # 只写「北京」、没写具体区的岗位：不该被误判成命中，也不该被排除
    vague = evaluate(_job(location="北京", district=None), RULES)
    check_true("只写北京的岗位放行", vague.ok)
    check_true("只写北京不算命中海淀",
               not any("优先区域" in r for r in vague.reasons))


if __name__ == "__main__":
    for fn in (test_wage, test_internship, test_evaluate):
        fn()
    total = PASSED + len(FAILED)
    print(f"通过 {PASSED}/{total}")
    for f in FAILED:
        print(f"  ✗ {f}")
    sys.exit(1 if FAILED else 0)
