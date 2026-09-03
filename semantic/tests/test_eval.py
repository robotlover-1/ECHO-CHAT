import sys, os

# 运行路径：semantic/ 下跑 pytest；parse/decision 在前缀(其父目录=semantic)，eval 数据包在本 tests/ 目录内。
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # semantic（parse/decision/ontology）
sys.path.insert(0, _HERE)                    # tests -> eval 包

import pytest
from decision import decide
from parse import parse
from eval.cases import CASES, MATRIX
from eval import cases as _cases_mod

REASONS = {r for _, c, exp in CASES if isinstance(exp, tuple) for r in [exp[1]]}


def _rows():
    """每条三元组 (q,c,exp) 必须依次落在 parse→decide 的真实结果。"""
    bad = []
    for q, c, exp in CASES:
        shared, reason = decide(parse(q), parse(c))[1:]
        if exp == "match":
            if not shared:
                bad.append((q, c, reason))
        else:
            if shared or reason != exp[1]:
                bad.append((q, c, reason, exp))
    return bad


def test_total_at_least_120():
    assert len(CASES) >= 120, len(CASES)


def test_matrix_rows_all_pass():
    for q, c, exp in MATRIX:
        shared, reason = decide(parse(q), parse(c))[1:]
        if exp == "match":
            assert shared, (q, c, reason)
        else:
            assert shared is False and reason == exp[1], (q, c, reason)


def test_all_cases():
    bad = _rows()
    assert not bad, f"{len(bad)} failures: {bad[:5]}"


def test_category_floors():
    neg = [x for x in CASES if isinstance(x[2], tuple)]
    pos = [x for x in CASES if x[2] == "match"]
    assert len(CASES) == len(neg) + len(pos), "每 case 恰 match 或 reject"
    assert len(neg) >= 50, len(neg)
    assert len(pos) >= 40, len(pos)
    lang = sum(1 for x in neg if x[2][1] == "language_conflict")
    con = sum(1 for x in neg if x[2][1] == "constraint_conflict")
    op = sum(1 for x in neg if x[2][1] == "operation_conflict")
    intn = sum(1 for x in neg if x[2][1] == "intent_conflict")
    subj = sum(1 for x in neg if x[2][1] == "subject_conflict")
    assert lang >= 10, f"lang {lang}"
    assert con >= 10, f"con {con}"
    assert op >= 5, f"op {op}"
    assert intn >= 10, f"intent {intn}"
    assert subj >= 6, f"subj {subj}"
    # 每种 reason 都确有 fixture（reason 集与类别一一对应；不引入浮空 reason 掩盖解析缺口）
    assert REASONS == {"language_conflict", "constraint_conflict", "operation_conflict",
                       "intent_conflict", "subject_conflict"}, sorted(REASONS)
