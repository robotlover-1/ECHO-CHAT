import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from ontology import load, lookup_subject_id, ONTOLOGY_VERSION

def test_loader_loads_version():
    d = load()
    assert d["concepts"] and ONTOLOGY_VERSION == "2026-09-06.1"

@pytest.mark.parametrize("text,expect", [
    ("红黑树", "red_black_tree"),
    ("rbtree", "red_black_tree"),
    ("red-black tree", "red_black_tree"),
    ("RBAC", None),                       # 词边界，不命中 rb
    ("二叉搜索树", "binary_search_tree"),
    ("bst", "binary_search_tree"),
    ("tree", "tree"),                     # 基础概念独立建档
    ("binary tree", "binary_tree"),
    ("RB tree", "red_black_tree"),
])
def test_lookup(text, expect):
    assert lookup_subject_id(text) == expect

def test_alias_global_unique():
    seen = {}
    for c in load()["concepts"]:
        for a in c["aliases"]:
            key = a.strip().lower()
            assert key not in seen or seen[key] == c["id"], f"alias dup: {key}"
            seen[key] = c["id"]
    assert len(seen) >= 40


def test_lookup_handles_glued_and_ns_variants():
    # red black tree 折叠变体应命中
    assert lookup_subject_id("redblacktree") == "red_black_tree"
    assert lookup_subject_id("std::rbtree") == "red_black_tree"  # 白名单 std:: 剥离


def test_plural_variant_english():
    # 有限复数走显式复数别名条目（数据驱动，可溯源）
    assert lookup_subject_id("linked lists") == "linked_list"
    assert lookup_subject_id("red black trees") == "red_black_tree"
    assert lookup_subject_id("binary search trees") == "binary_search_tree"


def test_validator_uniqueness_includes_generated():
    # 生成变体(folding)全集自身必须无重复；跨概念碰撞由 validate() 在启动时抛 AssertionError
    from ontology.validator import _generated_keys
    keys = _generated_keys(load())
    assert len(keys) == len(set(keys))


from ontology.validator import validate_groups

def _g(cid, group):
    return {"id": cid, "canonical_zh": "x", "canonical_en": "x",
            "aliases": ["x"], "group": group}

def test_group_valid():
    validate_groups([_g("elec_x", "electrical"),
                     _g("mech_x", "mechanical"),
                     _g("emb_x", "embedded"),
                     {"id": "legacy", "canonical_zh": "y", "canonical_en": "y", "aliases": ["y"]}])  # 旧概念无 group 放行

def test_group_illegal_value():
    with pytest.raises(AssertionError):
        validate_groups([_g("elec_x", "civil")])

def test_group_prefix_mismatch():
    with pytest.raises(AssertionError):
        validate_groups([_g("elec_x", "mechanical")])          # 前缀 vs group 不一致
    with pytest.raises(AssertionError):
        validate_groups([{"id": "elec_y", "canonical_zh": "z", "canonical_en": "z", "aliases": ["z"]}])  # 前缀漏 group
    with pytest.raises(AssertionError):
        validate_groups([_g("emb_x", "electrical")])           # 前缀 vs group 不一致
    with pytest.raises(AssertionError):
        validate_groups([{"id": "mech_y", "canonical_zh": "z", "canonical_en": "z", "aliases": ["z"]}])  # 前缀漏 group

def test_group_unknown_prefix_legacy_ok():
    # 既有 CS id 无前缀无 group → 放行；带前缀但非领域前缀且无 group → 放行
    validate_groups([{"id": "red_black_tree", "canonical_zh": "r", "canonical_en": "r", "aliases": ["r"]}])
