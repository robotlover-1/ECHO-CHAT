import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from parse import parse
from decision import hard_decide

# Task3：decision 已纯规则化 hard_decide(qp,cp)->(shared, reason, soft)；不再产模型分。
# shared/reason 语义与 Phase1 decide 一致（ok/各 conflict reason 保留）；soft 默认为 soft 通道标志。
def D(q, c):
    return hard_decide(parse(q), parse(c))

def SR(q, c):
    """取 decision 的 (shared, reason)——对比旧评分三元组没 soft 混淆。"""
    s, r, _ = D(q, c)
    return s, r

def test_match_rows():
    assert D("生成一个红黑树", "生成一个 rbtree")[0] is True
    assert D("用 C 语言生成红黑树", "使用 C 编写 rbtree")[0] is True
    assert D("红黑树是什么", "what is a red-black tree")[0] is True
    assert D("红黑树的插入", "写一个红黑树的插入")[0] is True

# Go accept 迁移：decision 不再给分 → 检索级"Go 接受谓词"代理移到余弦无关的纯规则 shared+reason=="ok"。
# 需余弦的分量（cos>=ACCEPTANCE）在检索级/eval 余弦测试里用 models.encode_query 执行（见 test_eval 与
# test_embedding）；此处 decision 单元只断言纯规则 shared/soft/reason。
# ACCEPTANCE（余弦阈值）Task6 用 e5 同主题内正负样本标定，当前未能校准前以宽松占位 0.0 并在 eval 余弦
# 测试注释声明"Task6 填"（见下 test_ok_deprecated_cosine_handed_retrieval）。
def _shared_ok(q, c):
    shared, reason, soft = D(q, c)
    assert (shared, reason) == (True, "ok"), (q, c, reason)
    assert soft is False, (q, c, soft)   # ok 命中非软通道
    return shared

def test_shared_ok_pure_rule():
    assert _shared_ok("红黑树是什么", "what is a red-black tree")
    assert _shared_ok("用 C 语言生成红黑树", "使用 C 编写 rbtree")
    assert _shared_ok("用 Python 实现红黑树插入", "implement RB-tree insertion in Python")
    assert _shared_ok("生成一个红黑树", "生成一个 rbtree")

def test_ok_deprecated_cosine_handed_retrieval():
    """旧决策分(s>0.25 canonical 余弦)已删：decision 只产 shared/reason/soft。
    纯规则返回三元组，长度恒 3（不虚构 score 占位）。
    ，即不再有任何 cosine 分量在 decision 层输出。
    """
    for q, c in [("红黑树是什么", "what is a red-black tree"),
                 ("用 C 语言生成红黑树", "使用 C 编写 rbtree"),
                 ("用 Python 实现红黑树插入", "implement RB-tree insertion in Python"),
                 ("生成一个红黑树", "生成一个 rbtree")]:
        out = D(q, c)
        assert len(out) == 3           # (shared, reason, soft)，无 score

def test_language_rejections():
    assert SR("C 实现红黑树", "C++ 实现 rbtree") == (False, "language_conflict")
    assert SR("生成红黑树", "用 C++ 生成红黑树") == (False, "language_conflict")
    assert SR("用 Python 实现单例", "实现单例") == (False, "language_conflict")  # 查询有语言→候选无语言
    assert D("什么是红黑树", "what is a red-black tree")[0] is True  # 定义不卡语言

def test_intent_rejections():
    # 隔离 intent 轴：subject 同、语言皆 None、operation 皆 None，仅 intent 不同 → intent_conflict。
    # R3 修复（test_parse 回归：中文句式内嵌英文宾语"实现一个 red-black tree"经保空格重跑命中本体）
    # 已让 spec 原 probe 能到达 intent_conflict；此处另保留等价的纯净中/英 probe 继续守 intent 轴。
    assert SR("红黑树是什么", "实现一个红黑树") == (False, "intent_conflict")
    assert SR("what is a red-black tree", "implement a red-black tree") == (False, "intent_conflict")
    assert SR("红黑树是什么", "实现一个 red-black tree") == (False, "intent_conflict")

def test_operation_conservative():
    assert SR("Python 实现红黑树插入", "Python 实现 rbtree 删除") == (False, "operation_conflict")
    assert SR("完整实现红黑树", "只实现红黑树插入") == (False, "operation_conflict")

def test_constraint_rejections():
    assert SR("实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树") == (False, "constraint_conflict")
    assert SR("实现带父指针的二叉树", "实现支持重复键的二叉树") == (False, "constraint_conflict")
    assert SR("生成一个红黑树", "实现线程安全的红黑树") == (False, "constraint_conflict")

def test_subject_boundary():
    assert D("实现一个二叉搜索树", "实现一棵二叉树")[0] is False
    assert D("quick sort", "sort")[0] is False

def test_constant_tuple_arity_no_score():
    # reject/unknown 也恒 3 元（旧 had score field）——确保不会在返元里混入模型分。
    sample = [("C 实现红黑树", "C++ 实现 rbtree"),
              ("红黑树是什么", "实现一个红黑树"),
              ("生成一个红黑树", "实现线程安全的红黑树"),
              ("实现一个二叉搜索树", "实现一棵二叉树"),
              ("实现布隆过滤器", "写一个 bloom filter")]  # unknown_subject(软关)
    for q, c in sample:
        assert len(D(q, c)) == 3
