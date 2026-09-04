import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parse import parse

# c++/cpp/cxx + 空格/无空格 等写法变体：同一 subject/intent/language/op → 同一指纹 → 跨变体命中缓存
VARS = [
    "实现一个 c++ 的红黑树",
    "生成一个 cpp 的 rbtree",
    "用cpp写红黑树",
    "用 c++ 写红黑树",
    "生成一个 cxx 的红黑树",
    "实现 cpp rbtree",
    "用c++实现红黑树",
    "写一个 c++ 红黑树",
]


def test_lang_variants_all_parse_same():
    for t in VARS:
        q = parse(t)
        assert q.subject_id == "red_black_tree", t
        assert q.language == "cpp", (t, q.language)
        assert q.intent == "implementation", (t, q.intent)
        assert q.fingerprint_eligible, (t, sorted(q.residual_words))


def test_lang_variants_share_fingerprint():
    fps = {parse(t).fingerprint for t in VARS}
    assert len(fps) == 1, fps


def test_spaced_english_language_not_glued():
    # 修复回归: 去空格会把 "cpp rbtree" 粘成 cpprbtree 导致词界挡掉语言
    assert parse("实现 cpp rbtree").language == "cpp"
    assert parse("用 python 写红黑树").language == "python"
    assert parse("java 写红黑树").language == "java"
