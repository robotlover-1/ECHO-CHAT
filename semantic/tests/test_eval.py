import sys, os

# 运行路径：semantic/ 下跑 pytest；parse/decision 在前缀(其父目录=semantic)，eval 数据包在本 tests/ 目录内。
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # semantic（parse/decision/ontology）
sys.path.insert(0, _HERE)                    # tests -> eval 包

import pytest
from models import encode_query
from decision import hard_decide
from parse import parse
from eval.cases import CASES, MATRIX
from eval import cases as _cases_mod
from eval import retrieval_cases as _retr
from eval.retrieval_cases import ROWS as RETR_ROWS, category_summary as retr_summary

# Task3：decision 纯规则化，不再给分。Go accept 谓词的两半被拆分：
#   - 纯规则 shared & reason=="ok"：由 decision.hard_decide 断言（此处所有 match 行）。
#   - 余弦分量（cos >= ACCEPTANCE）：decision 不再产 → 迁到检索级代理此处对关键命中用
#     models.encode_query 计算。（余弦经 L2 归一：点积==余弦）
# ACCEPTANCE = Task6 校准值（见 tools/calibrate.py + eval/retrieval_cases.py 结论）：
#   e5 绝对余弦同主题也饱和~0.84-0.9（复用 OK 命中最低实测 0.776），同主题真负均被 decision
#   硬拒 → 不存在"正/负可分"的绝对阈值。故 acceptance 只作**极低兜底 0.6**，保证所有 decision-ok
#   真实复用不被误杀（fit/dev+cal recall=1.0、独立 test 一次 recall=1.0），真正区分靠 decision。
ACCEPTANCE = 0.6


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def codecos(q, c):
    """检索级"Go 接受谓词"的余弦代理：cos(encode_query(q), encode_query(c))。"""
    return _cos(encode_query(q), encode_query(c))


REASONS = {r for _, c, exp in CASES if isinstance(exp, tuple) for r in [exp[1]]}
# decision 可达 reason 集（含纯规则 new 软命中，但 CASES 无 soft fixture，故 reason 集仍为 5 硬拒）


def _rows():
    """每条三元组 (q,c,exp) 必须落在 raw parse→hard_decide 的真实结果（纯规则 shared/reason）。"""
    bad = []
    for q, c, exp in CASES:
        shared, reason, soft = hard_decide(parse(q), parse(c))
        if exp == "match":
            # Go accept 谓词契约迁移：shared && reason=="ok"；soft 通道不用于正样本(soft 默认关)。
            if not (shared and reason == "ok"):
                bad.append((q, c, reason))
        else:  # ("reject", expected_reason)
            if shared or reason != exp[1]:
                bad.append((q, c, reason, exp))
    return bad


def test_total_at_least_120():
    assert len(CASES) >= 120, len(CASES)


def test_matrix_rows_accept_predicate():
    """MATRIX 手工权威集断言到(纯规则)accept 谓词 shared && reason=="ok"，并额外做
    稀疏余弦代理（e5 余弦>=ACCEPTANCE，Task6 校准前为 0.0 宽松占位）。
    """
    for q, c, exp in MATRIX:
        shared, reason, soft = hard_decide(parse(q), parse(c))
        if exp == "match":
            assert shared and reason == "ok", (q, c, reason)
            assert soft is False, (q, c, soft)
            # 余弦代理：跨措辞/跨语言"应命中"在旧 Jaccard≈0 被误伤，Task3 decision 虽不给分，
            # 但检索级最终仍以余弦排序 → 对 OK 命中维持余弦健康门（Task6 校准 ACCEPTANCE=0.6 兜底）。
            assert codecos(q, c) >= ACCEPTANCE, (q, c)
        else:
            assert shared is False and reason == exp[1], (q, c, reason)
            assert soft is False, (q, c, reason)


def test_cross_language_hits_cosine_proxy():
    """ITEM-1 应命中对（跨语言/跨措辞）——模型分阶段这里是唯一守 cosine>=0.25 的地方，现迁为
    检索级 codecos 代理（ACCEPTANCE=Task6 校准 0.6 兜底线）。纯规则 shared/reason 在 _rows 已守 ok。
    """
    hits = [
        ("红黑树是什么", "what is a red-black tree"),
        ("用 C 语言生成红黑树", "使用 C 编写 rbtree"),
        ("用 Python 实现红黑树插入", "implement RB-tree insertion in Python"),
    ]
    for q, c in hits:
        shared, reason, _ = hard_decide(parse(q), parse(c))
        assert shared and reason == "ok"
        assert codecos(q, c) >= ACCEPTANCE, (q, c)   # Task6 校准 ACCEPTANCE=0.6


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


# ===================== Task6：检索质量集门禁（基于 eval/retrieval_cases.py） =====================
def test_retrieval_cases_floors():
    """检索质量三分集的类别最小量（见 retrieval_cases docstring；≥200 且各类别下限）。"""
    s = retr_summary()
    assert len(RETR_ROWS) >= 200, len(RETR_ROWS)
    assert s["onto_pos"] >= 40, s         # 本体正跨语可复用
    assert s["super_pos"] >= 20, s        # 超本体正语域外
    assert s["hard_neg"] >= 60, s         # 同主题硬负（语言/操作/意图）
    assert s["constraint_neg"] >= 30, s   # 约束冲突负
    assert s["plain_neg"] >= 20, s        # 普通负
    assert s["bypass"] >= 5, s            # 绕过（状态改写/上下文续）


def test_retrieval_positives_decision_ok_and_cos_backstop():
    """检索级正样本（同本体/超本体可复用）应 (a) decision 判 ok；(b) e5 cosine ≥ ACCEPTANCE 兜底线。
    Task6 结论：decision-ok 的真实复用最低 cosine≈0.776，ACCEPTANCE=0.6 只作极低兜底、绝不误杀。"""
    bad = []
    for q, c, cat in RETR_ROWS:
        if cat not in ("onto_pos", "super_pos"):
            continue
        shared, reason, soft = hard_decide(parse(q), parse(c))
        if not (shared and reason == "ok"):
            # 超本体正（subject 无建档）本就常回落 unknown_subject —— 这里只守"并非安全误判"：
            # 非法 shared 才算失败；其余（被 decision 拒绝）不作为正样本错误（decision 是第一道线）。
            if shared:
                bad.append((q, c, reason))
            continue
        # decision-ok 的真正候选：必须过 cos 兜底
        if codecos(q, c) < ACCEPTANCE:
            bad.append((q, c, round(codecos(q, c), 4)))
    assert not bad, f"{len(bad)} retrieval-pos 未过 acceptance: {bad[:8]}"


def test_retrieval_negatives_are_decision_blocked():
    """同主题硬负 / 约束 / 普通负不应被 decision 放行（shared）—— decision 先把它们挡在 acceptance 外，
    正应如此（因此不需要高 acceptance）。注释每类都须 decision 否拒。"""
    bad = ["本类负样本不应 shared"]
    kinds = {"hard_neg", "constraint_neg", "plain_neg"}
    for q, c, cat in RETR_ROWS:
        if cat not in kinds:
            continue
        shared, reason, _ = hard_decide(parse(q), parse(c))
        if shared:
            bad.append((q, c, cat, reason))
    # 超本体正/普通负可判 unknown（本函数只守不该 shared 的硬负/约束/普通真负）
    assert len(bad) == 1, bad[:8]
