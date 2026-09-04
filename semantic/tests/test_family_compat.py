"""Task4 决策 family_compat（受控跨实体共享，默认关）单测 + Precision 共享判定集门禁。

覆盖：
  1. 默认关（SEMANTIC_FAMILY_COMPAT 缺省即 "0"）：不同实体跨 family 概念 → 保守 subject_conflict，不起放行。
  2. 开启后，正例（不同 subject_id + 同 family + 无 type_args + 非 multi + 意图实现/定义 + 语言同）
     → shared=True reason="ok" soft=False 且 verbose source="family_compat"。
  3. 负例（跨语言 / 带模板 / multi_subject / splice·API / 操作·非实现意图 / 异家族）在开启下仍 shared False。
  4. Precision 门：消费 eval/family_share_cases（确定性真值过滤 ≥30 对）。
  依赖 Task3 交付的 parse 实体化字段（implementation_family/type_args/multi_subject）。

decision 每次调用即时读 env，无模块级缓存 → 可直接切换 env 测开关。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))   # semantic/（parse/decision/ontology）
sys.path.insert(0, _HERE)                    # tests -> eval/

from parse import parse                       # noqa: E402
from decision import hard_decide, hard_decide_verbose  # noqa: E402
from eval.family_share_cases import POS, NEG  # noqa: E402, F401  —— POS/NEG 为 Precision 判定集


def _with_env(val):
    """把 SEMANTIC_FAMILY_COMPAT 置为 val；返回恢复为旧值的句柄。"""
    prev = os.environ.get("SEMANTIC_FAMILY_COMPAT", "0")

    def restore():
        os.environ["SEMANTIC_FAMILY_COMPAT"] = prev

    os.environ["SEMANTIC_FAMILY_COMPAT"] = val
    return restore


def DV(q, c):
    """family_compat 开启下的 verbose 决策 (shared, reason, soft, source)。"""
    restore = _with_env("1")
    try:
        return hard_decide_verbose(parse(q), parse(c))
    finally:
        restore()


def DOFF(q, c):
    """默认关下的三元决策 (shared, reason, soft)。"""
    restore = _with_env("0")
    try:
        return hard_decide(parse(q), parse(c))
    finally:
        restore()


# ============================================================= Step1 主单测 =====
def test_family_compat_off_by_default_rejects_cross_entity():
    # cpp_std_list(库实体, family=linked_list) vs linked_list(抽象概念) 默认不授权 → 保守拒
    s, r, soft = DOFF("用 C++ 写一个 list", "用 cpp 实现链表")
    assert s is False
    assert r in ("unknown_subject", "subject_conflict", "intent_conflict")
    assert soft is False


def test_family_compat_on_allows_clean_implementation_pair():
    # 不同实体但同 family + 意图实现 + 语言同 + 无模板 + 非 multi → shared True reason ok source=family_compat
    s, r, soft, source = DV("用 C++ 写一个 list", "用 cpp 实现链表")
    assert (s, r, soft) == (True, "ok", False)
    assert source == "family_compat"


def test_source_defaults_to_hard_when_switch_off():
    # family_compat 关闭 → source 恒 "hard"；旧三元组长度 3 契约不变
    assert DOFF("用 C++ 写一个 list", "用 cpp 实现链表")[0] is False
    assert len(DOFF("用 C++ 写一个 list", "用 cpp 实现链表")) == 3


# ============================================================= 单轴条件收紧 =====
def test_on_keeps_requiring_family_equal_same_lang():
    # 语言同但 family 不同(linked vs dynamic_array) → subject_conflict，不起 family_compat
    s, r, soft, source = DV("用 C++ 写一个 list", "用 C++ 写一个 vector")
    assert s is False and source == "hard"


def test_on_rejects_cross_language():
    # C++ vector(dynamic_array) vs python list(dynamic_array)：语言不同 → family_compat 不放行
    s, *_ = DV("用 C++ 写一个 vector", "用 python 实现 list")
    assert s is False


def test_on_rejects_templated_entity():
    # 带模板 type_args(double) → family 不放行（避免危险同指纹/模板混用）
    s, r, soft, source = DV("用 C++ 实现 std::vector<double>", "cpp 实现动态数组")
    assert s is False and source == "hard"


def test_on_rejects_multi_subject():
    s, *_ = DV("C++ list 和 vector 的区别", "用 C++ 实现 list")
    assert s is False


def test_on_rejects_non_impl_def_splice_intent():
    # std::list splice 属 API/操作意图（非 实现·定义）→ family 不放行共享
    s, r, soft, source = DV("std::list 怎么 splice", "用 cpp 实现链表")
    assert s is False and source == "hard"


# ========================================================= Precision 门（判定集）====
def test_family_set_total_at_least_30():
    assert len(POS) + len(NEG) >= 30, (len(POS), len(NEG))


def test_family_set_positives_shared_true_source_family_compat_when_on():
    """正例开启时必须走到 family_compat 分支：shared&ok&source=family_compat。缺则不构成正样本。"""
    bad = []
    for q, c in POS:
        s, r, soft, source = DV(q, c)
        if not (s is True and r == "ok" and soft is False and source == "family_compat"):
            bad.append((q, c, (s, r, soft, source)))
    assert not bad, f"{len(bad)} POS 未走 family_compat: {bad[:5]}"


def test_family_set_positives_conservative_when_off():
    """正例默认关（不授权 family_compat）必须保守 shared False —— 放行完全由 env 开关授权。"""
    bad = []
    for q, c in POS:
        s, r, soft = DOFF(q, c)
        if s is True:
            bad.append((q, c, (s, r, soft)))
    assert not bad, f"{len(bad)} POS 默认关被误共享: {bad[:5]}"


def test_family_set_negatives_stay_blocked_when_on():
    """负例（跨语言/模板/multi/splice·API/操作/异家族）开启下仍须 shared False —— 防家族误放行。"""
    bad = []
    for q, c in NEG:
        s, r, soft, source = DV(q, c)
        if s is True:
            bad.append((q, c, (s, r, soft, source)))
    assert not bad, f"{len(bad)} NEG 被 family 误放行: {bad[:5]}"
