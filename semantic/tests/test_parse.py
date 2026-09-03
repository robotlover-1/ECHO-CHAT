import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from parse import parse
from decision import hard_decide

CASES = [
    # (text, subject_id, intent, language)
    ("生成一个红黑树", "red_black_tree", "implementation", None),
    ("什么是红黑树", "red_black_tree", "definition", None),
    ("what is a red-black tree", "red_black_tree", "definition", None),
    ("implement a red-black tree", "red_black_tree", "implementation", None),
    ("使用 C 编写 rbtree", "red_black_tree", "implementation", "c"),
    ("用 C 语言生成红黑树", "red_black_tree", "implementation", "c"),
    ("用 Python 实现红黑树插入", "red_black_tree", "implementation", "python"),
    ("红黑树是什么", "red_black_tree", "definition", None),
    ("RBAC 权限模型是什么", None, "definition", None),   # RBAC 不命中 rbtree
    ("JavaScript 实现深拷贝", "deep_copy", "implementation", "javascript"),
    ("用Java写线程池", "thread_pool", "implementation", "java"),
    ("用 C++ 写红黑树", "red_black_tree", "implementation", "cpp"),
]
@pytest.mark.parametrize("text,sid,intent,lang", CASES)
def test_parse_slots(text, sid, intent, lang):
    q = parse(text)
    assert q.subject_id == sid, text
    assert q.intent == intent, text
    assert q.language == lang, text

def test_parse_operation_maps_to_id():
    assert parse("用 Python 实现红黑树插入").operation == "insert"
    assert parse("rbtree 删除节点").operation == "delete"

def test_parse_operation_ignores_separate_predicate_and_keeps_real_ops():
    # eval 权威集校准：概念名内嵌的操作字（二叉搜索树→'搜索'、插入排序→'插入'、红黑树→无）不得
    # 被误当成对数据结构的具体数据操作（否则"二叉搜索树是什么×实现一个二叉搜索树"竟会 false-match、
    # "实现 BST"也被降格，→ 全部误拒/误配）。
    assert parse("实现一个二叉搜索树").operation is None
    assert parse("二叉搜索树是什么").operation is None
    assert parse("实现一个插入排序").operation is None
    # 真·独立操作仍须保留：词尾/独立谓词的插删改查照旧抽取。
    assert parse("红黑树的插入").operation == "insert"
    assert parse("在二叉搜索树中查找一个值").operation == "find"
    assert parse("用 Python 实现红黑树插入").operation == "insert"

def test_parse_cn_head_english_subject_space_retry():
    # carry-over(R3)：中文句式内嵌英文宾语"实现一个 red-black tree"，压空格后变 'red-blacktree'
    # 本体无此形态 → R3 修复前 subject_id=None → decision 只得 unknown_subject。
    # 修复后用「保空格」normalized 文本重跑中文句式，英文保留 token 间隙命中 red_black_tree，
    # decision 走到 intent_conflict（红黑树是什么 × 实现）。无空格中文用例不受影响。
    q = parse("实现一个 red-black tree")
    assert q.subject_id == "red_black_tree", q.subject_text
    assert q.subject_text == "red-black tree", q.subject_text
    # 回归：携词不因空格吞并误判为 rbtree 同义宾语（decision 纯规则 hard_decide；reason 在 idx1）
    s, r, _ = hard_decide(parse("红黑树是什么"), q)
    assert (s, r) == (False, "intent_conflict")

def test_parse_bypass():
    assert parse("继续修改上面的红黑树").bypass_cache is True
    assert parse("记住我叫小明").bypass_cache is True
    assert parse("生成一个红黑树").bypass_cache is False
