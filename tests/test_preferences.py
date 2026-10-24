"""学历 / 薪资指令解析与持久化的测试。不依赖 pytest。

    python tests/test_preferences.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoboss import preferences as p  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want, tol: float = 1e-6):
    global PASSED
    if isinstance(want, float) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 得到 {got!r}，期望 {want!r}")


def check_true(name: str, cond):
    check(name, bool(cond), True)


# ===========================================================================
#  学历
# ===========================================================================
def test_normalize_education():
    for raw, want in (("本科", "本科"), ("硕士", "硕士"), ("博士", "博士"),
                      ("大专", "大专"), ("专科", "大专"), ("研究生", "硕士"),
                      ("学士", "本科"), ("高中", "高中"), ("中专", "中专/中技"),
                      ("初中", "初中及以下"), ("本科", "本科"),
                      ("学历 本科", "本科"), ("学历=本科", "本科"), ("edu:本科", "本科"),
                      ("本科及以上", "本科")):
        if raw == "本科及以上":
            check(f"「{raw}」默认不认（那是岗位要求写法）",
                  p.normalize_education(raw), None)
            check(f"「{raw}」允许时按本科",
                  p.normalize_education(raw, allow_above=True), "本科")
            continue
        check(f"「{raw}」", p.normalize_education(raw), want)

    check("不限 → 空串", p.normalize_education("不限"), "")
    check("薪资不限也能认（去掉前缀再判断）", p.normalize_education("薪资不限"), "")
    check("空 → None", p.normalize_education(""), None)
    check("乱写 → None", p.normalize_education("火星"), None)
    check_true("「本科及以上」被识别成学历写法", p.looks_like_education("本科及以上"))
    check_true("「本科」也是学历写法", p.looks_like_education("本科"))
    check("rank 本科", p.rank_of("本科"), 5)
    check("rank 硕士", p.rank_of("硕士"), 6)
    check("rank 不限", p.rank_of("不限"), 0)
    check("rank 读不出", p.rank_of(None), 0)


# ===========================================================================
#  薪资
# ===========================================================================
def test_wage_parsing():
    for raw, want in (("300", 300.0), ("薪资300", 300.0), ("日薪300", 300.0),
                      ("300元/天", 300.0), ("300/天", 300.0), ("300元", 300.0),
                      ("≥300", 300.0), ("不低于300", 300.0), ("300以上", 300.0),
                      ("300+", 300.0), ("300-500元/天", 300.0),
                      ("月薪8000", 367.8), ("8000元/月", 367.8), ("8k", 367.8),
                      ("1.5万", 689.7)):
        check(f"「{raw}」", p.parse_wage(raw), want, tol=0.05)
    check("认不出的返回 None", p.parse_wage("看心情"), None)
    check_true("10 元/天算太小", "太小" in p.wage_problem(9))
    check_true("8000 元/天算太大", "太大" in p.wage_problem(8000))
    check("正常值没问题", p.wage_problem(300), "")
    check("0 是合法的不限", p.wage_problem(0.0), "")


# ===========================================================================
#  整条指令
# ===========================================================================
def test_task_args():
    a = p.parse_task_args(["大模型算法", "本科", "300"])
    check("关键词", a.keywords, ["大模型算法"])
    check("学历", a.education, "本科")
    check("日薪", a.min_wage, 300.0)
    check("没写条数", a.limit, None)
    check_true("动了设置", a.touched)

    a = p.parse_task_args(["大模型算法", "10"])
    check("裸数字 10 是条数", (a.limit, a.min_wage), (10, None))
    check("没动设置", a.touched, False)

    a = p.parse_task_args(["大模型算法", "300"])
    check("没写学历时裸数字 300 仍是条数（老习惯）",
          (a.limit, a.min_wage), (300, None))

    a = p.parse_task_args(["大模型算法", "本科", "300"])
    check("写了学历后，后面的 300 当日薪",
          (a.education, a.min_wage, a.limit), ("本科", 300.0, None))

    a = p.parse_task_args(["大模型算法", "本科", "300", "10"])
    check("学历 + 日薪 + 条数",
          (a.education, a.min_wage, a.limit), ("本科", 300.0, 10))

    a = p.parse_task_args(["大模型算法", "300", "本科"])
    check("学历写在数字后面也认",
          (a.education, a.min_wage, a.limit), ("本科", 300.0, None))

    a = p.parse_task_args(["10条", "本科"])
    check("「10条」永远是条数", a.limit, 10)

    a = p.parse_task_args(["本科", "硕士"])
    check("后写的学历覆盖前面的", a.education, "硕士")

    a = p.parse_task_args(["学历不限", "薪资不限"])
    check("学历清空", a.education, "")
    check("薪资清空", a.min_wage, 0.0)

    a = p.parse_task_args(["本科及以上", "300"])
    check_true("岗位要求写法给出人话提示",
               a.errors and "岗位要求" in a.errors[0])

    a = p.parse_task_args(["薪资99999"])
    check("离谱日薪报错", len(a.errors), 1)

    a = p.parse_task_args(["600条"])
    check("条数上限报错", len(a.errors), 1)

    a = p.parse_task_args("大模型算法,本科,300")
    check("整串也能解析（逗号分隔）",
          (a.keywords, a.education, a.min_wage), (["大模型算法"], "本科", 300.0))

    # 标记和值分开写也认：「/投递 岗位 学历 本科 薪资 300」
    a = p.parse_task_args(["大模型算法", "学历", "本科", "薪资", "300"])
    check("学历/薪资标记",
          (a.keywords, a.education, a.min_wage), (["大模型算法"], "本科", 300.0))

    a = p.parse_task_args(["岗位", "月薪", "8000"])
    check("月薪标记", a.min_wage, 367.8)

    a = p.parse_task_args(["岗位", "薪资", "不限"])
    check("薪资标记后面写不限", a.min_wage, 0.0)

    a = p.parse_task_args(["岗位", "学历"])
    check_true("标记后面没值要报错", a.errors)

    # 关键词里的数字不能被误认成薪资
    a = p.parse_task_args(["Agent", "RAG", "5"])
    check("5 是条数", (a.keywords, a.limit), (["Agent", "RAG"], 5))


# ===========================================================================
#  持久化
# ===========================================================================
def test_prefs_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".qq_prefs.json"

        check("没有文件时是空的", p.load_prefs(path).is_empty(), True)

        saved = p.Prefs(education="本科", min_wage=300.0)
        p.save_prefs(path, saved)
        check("保存后能读回来", p.load_prefs(path), saved)
        data = json.loads(path.read_text(encoding="utf-8"))
        check("落盘内容是 JSON", data, {"education": "本科", "min_wage": 300.0})

        # 明确「不限」要能存住（空串不能读成「没设置」）
        p.save_prefs(path, p.Prefs(education="", min_wage=0.0))
        loaded = p.load_prefs(path)
        check("学历不限存得住", loaded.education, "")
        check("薪资不限存得住", loaded.min_wage, 0.0)

        # 两项都回到「没设置」时删文件
        p.save_prefs(path, p.Prefs())
        check("空设置删文件", path.exists(), False)

        # 坏文件不能把 bot 带崩
        path.write_text("{不是 json", encoding="utf-8")
        check("坏文件按空设置处理", p.load_prefs(path).is_empty(), True)

        path.write_text('{"education": "硕士", "min_wage": "abc"}',
                        encoding="utf-8")
        loaded = p.load_prefs(path)
        check("坏的日薪被忽略", loaded.min_wage, None)
        check("好的学历照常读", loaded.education, "硕士")


def test_apply_and_describe():
    prefs = p.Prefs(education="本科", min_wage=300.0)
    a = p.parse_task_args(["硕士", "500"])
    merged = prefs.merged(a)
    check("覆盖学历", merged.education, "硕士")
    check("覆盖日薪", merged.min_wage, 500.0)

    a = p.parse_task_args(["大模型算法"])
    check("没提的项保持不变", prefs.merged(a), prefs)

    text = p.describe(prefs)
    check_true("描述里有学历", "本科" in text)
    check_true("描述里有日薪", "300" in text)
    check_true("状态行有学历", "硕士" in p.status_line(p.Prefs(education="硕士")))
    check_true("空设置说明按 config",
               "config" in p.status_line(p.Prefs()))
    check_true("不限能显示出来", "不限" in p.status_line(p.Prefs(education="", min_wage=0.0)))


if __name__ == "__main__":
    for fn in (test_normalize_education, test_wage_parsing, test_task_args,
               test_prefs_roundtrip, test_apply_and_describe):
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
