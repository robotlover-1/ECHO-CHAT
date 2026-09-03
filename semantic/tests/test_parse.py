import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from parse import parse

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

def test_parse_bypass():
    assert parse("继续修改上面的红黑树").bypass_cache is True
    assert parse("记住我叫小明").bypass_cache is True
    assert parse("生成一个红黑树").bypass_cache is False
