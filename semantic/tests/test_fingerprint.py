import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parse import parse, build_fingerprint


def fp(s):
    return parse(s).fingerprint


def test_alias_and_wording_same_fp():
    # 主体别名 + 措辞不同 → (subject,intent,language,operation,output,residual=[]) 全同 → 同指纹
    assert fp("用 C 语言生成红黑树") == fp("使用 C 编写 rbtree")
    assert fp("生成一个红黑树") == fp("生成一个 rbtree")
    assert fp("什么是红黑树") == fp("what is a red-black tree")


def test_fp_differs_on_language_operation():
    assert fp("用 C 语言生成红黑树") != fp("用 C++ 生成红黑树")
    assert fp("用 Python 实现红黑树插入") != fp("用 Python 实现 rbtree 删除")


def test_constraint_queries_not_eligible():
    for t in ["实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树",
              "实现支持重复键的 C++ 红黑树", "实现无递归删除的红黑树",
              "实现带父指针的二叉树"]:
        q = parse(t)
        assert q.fingerprint_eligible is False, t
        assert q.fingerprint is None, t


def test_residual_captures_constraint():
    assert "线程安全" in parse("实现线程安全的 C++ 红黑树").residual_words
    assert parse("生成一个红黑树").residual_words == frozenset()


def test_fp_deterministic_and_versioned():
    a = build_fingerprint(parse("实现红黑树"))
    b = build_fingerprint(parse("实现红黑树"))
    assert a == b and a
    # 版本随 parser_version / ontology_version change 而漂移
    assert fp("实现红黑树") == fp("生成红黑树")


# ---- carry-over（Task3评审）：英文尾式语言，压缩后 in+lang 粘连，须带空格补判 ----
def test_english_trailing_language():
    assert parse("implement RB-tree insertion in Python").language == "python"
    assert parse("implement RB-tree insertion in Java").language == "java"
