"""学历（单个档位）/ 薪资指令解析与持久化的测试。不依赖 pytest。

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
                      ("初中", "初中及以下"), ("学历 本科", "本科"),
                      ("学历=本科", "本科"), ("edu:本科", "本科")):
        check(f"「{raw}」", p.normalize_education(raw), want)

    check("「本科及以上」默认不认（那是岗位要求写法）",
          p.normalize_education("本科及以上"), None)
    check("「本科及以上」允许时按本科",
          p.normalize_education("本科及以上", allow_above=True), "本科")
    check("不限 → 空串", p.normalize_education("不限"), "")
    check("清空 → 空串", p.normalize_education("清空"), "")
    check("空 → None", p.normalize_education(""), None)
    check("乱写 → None", p.normalize_education("火星"), None)
    check_true("「本科及以上」被识别成学历写法", p.looks_like_education("本科及以上"))
    check("rank 本科", p.rank_of("本科"), 5)
    check("rank 硕士", p.rank_of("硕士"), 6)
    check("rank 不限", p.rank_of("不限"), 0)
    check("rank 读不出", p.rank_of(None), 0)


def test_parse_education():
    check("硕士", p.parse_education("硕士"), ("硕士", ""))
    check("不限", p.parse_education("不限"), ("", ""))
    check("清空 → 不限", p.parse_education("清空"), ("", ""))
    check("学历=本科", p.parse_education("学历=本科"), ("本科", ""))

    value, error = p.parse_education("本科及以上")
    check("岗位要求写法被拒绝", value, None)
    check_true("给出人话提示", "岗位要求" in error)

    value, error = p.parse_education("火星")
    check("认不出被拒绝", value, None)
    check_true("提示可选档位", "可写" in error)


def test_degree_param():
    check("硕士", p.degree_param("硕士"), "204")
    check("本科", p.degree_param("本科"), "203")
    check("大专", p.degree_param("大专"), "202")
    check("不限 = 不带参数", p.degree_param("不限"), "")
    check("空 = 不带参数", p.degree_param(""), "")


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
#  持久化
# ===========================================================================
def test_prefs_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".qq_prefs.json"

        check("没有文件时是空的", p.load_prefs(path).is_empty(), True)

        saved = p.Prefs(education="硕士", min_wage=300.0)
        p.save_prefs(path, saved)
        check("保存后能读回来", p.load_prefs(path), saved)
        data = json.loads(path.read_text(encoding="utf-8"))
        check("落盘内容是 JSON", data, {"education": "硕士", "min_wage": 300.0})

        # 明确「不限」要能存住（空串不能读成「没设置」）
        p.save_prefs(path, p.Prefs(education="", min_wage=0.0))
        loaded = p.load_prefs(path)
        check("学历不限存得住", loaded.education, "")
        check("薪资不限存得住", loaded.min_wage, 0.0)

        # 上一版存的是「最低, 最高」数组，迁移成上限那一档
        path.write_text('{"education": ["", "硕士"], "min_wage": 300}',
                        encoding="utf-8")
        loaded = p.load_prefs(path)
        check("旧区间迁成上限", loaded.education, "硕士")
        check("旧格式的薪资照读", loaded.min_wage, 300.0)

        path.write_text('{"education": ["本科", "硕士"]}', encoding="utf-8")
        check("旧区间取最高档", p.load_prefs(path).education, "硕士")

        # 更早的格式：只存一个字符串
        path.write_text('{"education": "本科"}', encoding="utf-8")
        check("旧字符串照读", p.load_prefs(path).education, "本科")

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


def test_with_and_describe():
    prefs = p.Prefs(education="硕士", min_wage=300.0)

    changed = prefs.with_education("")
    check("改成不限", changed.education, "")
    check("改学历不动薪资", changed.min_wage, 300.0)

    changed = prefs.with_wage(0.0)
    check("改薪资", changed.min_wage, 0.0)
    check("改薪资不动学历", changed.education, "硕士")

    text = p.describe(prefs)
    check_true("描述里有学历", "硕士" in text)
    check_true("描述里有日薪", "300" in text)
    check_true("状态行有学历", "硕士" in p.status_line(prefs))
    check_true("空设置说明按 config", "config" in p.status_line(p.Prefs()))
    check_true("不限能显示出来",
               "不限" in p.status_line(p.Prefs(education="", min_wage=0.0)))
    check("education_label 未设置", p.education_label(None), "未设置（按 config.toml）")
    check("education_label 不限", p.education_label(""), "不限")
    check("education_label 档位", p.education_label("本科"), "本科")


if __name__ == "__main__":
    for fn in (test_normalize_education, test_parse_education, test_degree_param,
               test_wage_parsing, test_prefs_roundtrip, test_with_and_describe):
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
