import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from parse import parse
from decision import decide

def D(q, c):
    return decide(parse(q), parse(c))

def test_match_rows():
    assert D("生成一个红黑树", "生成一个 rbtree")[1] is True
    assert D("用 C 语言生成红黑树", "使用 C 编写 rbtree")[1] is True
    assert D("红黑树是什么", "what is a red-black tree")[1] is True
    assert D("红黑树的插入", "写一个红黑树的插入")[1] is True

# Go accept 谓词（ITEM-1：score=canonical 嵌入余弦，非关键词 Jaccard）：
# shared && reason=="ok" && score>=0.25（rerank_threshold 见 Go config）。
# cross-language / 跨措辞"应命中"对在旧 Jaccard 下 ≈0，正是本谓词要守住的回退。
def _ok(q, c):
    score, shared, reason = D(q, c)
    assert (shared, reason) == (True, "ok"), (q, c, reason)
    assert score >= 0.25, (q, c, score)
    return score

def test_ok_score_is_canonical_cosine_cross_language():
    assert _ok("红黑树是什么", "what is a red-black tree") > 0.25
    assert _ok("用 C 语言生成红黑树", "使用 C 编写 rbtree") > 0.25
    assert _ok("用 Python 实现红黑树插入", "implement RB-tree insertion in Python") > 0.25
    assert _ok("生成一个红黑树", "生成一个 rbtree") > 0.25

def test_reject_always_zero_score():
    """reject/unknown 分支 score 恒 0.0（只有 ok 携带等价格等价度）。"""
    assert D("生成红黑树", "用 C++ 生成红黑树")[0] == 0.0  # language_conflict
    assert D("红黑树是什么", "实现一个红黑树")[0] == 0.0      # intent_conflict
    assert D("实现一个二叉搜索树", "实现一棵二叉树")[0] == 0.0  # subject_conflict


def test_language_rejections():
    assert D("C 实现红黑树", "C++ 实现 rbtree")[1:] == (False, "language_conflict")
    assert D("生成红黑树", "用 C++ 生成红黑树")[1:] == (False, "language_conflict")
    assert D("用 Python 实现单例", "实现单例")[1:] == (False, "language_conflict")  # 查询有语言→候选无语言
    assert D("什么是红黑树", "what is a red-black tree")[1] is True  # 定义不卡语言

def test_intent_rejections():
    # 隔离 intent 轴：subject 同、语言皆 None、operation 皆 None，仅 intent 不同 → intent_conflict。
    # R3 修复（test_parse 回归：中文句式内嵌英文宾语"实现一个 red-black tree"经保空格重跑命中本体）
    # 已让 spec 原 probe 能到达 intent_conflict；此处另保留等价的纯净中文/英文 probe 继续守 intent 轴。
    assert D("红黑树是什么", "实现一个红黑树")[1:] == (False, "intent_conflict")
    assert D("what is a red-black tree", "implement a red-black tree")[1:] == (False, "intent_conflict")
    assert D("红黑树是什么", "实现一个 red-black tree")[1:] == (False, "intent_conflict")

def test_operation_conservative():
    assert D("Python 实现红黑树插入", "Python 实现 rbtree 删除")[1:] == (False, "operation_conflict")
    assert D("完整实现红黑树", "只实现红黑树插入")[1:] == (False, "operation_conflict")

def test_constraint_rejections():
    assert D("实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树")[1:] == (False, "constraint_conflict")
    assert D("实现带父指针的二叉树", "实现支持重复键的二叉树")[1:] == (False, "constraint_conflict")
    assert D("生成一个红黑树", "实现线程安全的红黑树")[1:] == (False, "constraint_conflict")

def test_subject_boundary():
    assert D("实现一个二叉搜索树", "实现一棵二叉树")[1] is False
    assert D("quick sort", "sort")[1] is False
