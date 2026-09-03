import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from ontology import load, lookup_subject_id, ONTOLOGY_VERSION

def test_loader_loads_version():
    d = load()
    assert d["concepts"] and ONTOLOGY_VERSION == "2026-09-03.1"

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
