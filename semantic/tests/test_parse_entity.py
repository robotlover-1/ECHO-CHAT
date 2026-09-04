import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parse import parse

# Task3: parse 术语实体化 + 语言门控 + 指纹安全(entity/type_args/ns/multi→eligible False)
def test_cpp_list_entity_not_fp():
    q = parse("用 C++ 实现一个 list")
    assert q.subject_id == "cpp_std_list"
    assert q.subject_kind == "library_type"
    assert q.language == "cpp"
    assert q.fingerprint_eligible is False      # 库实体 → 只向量/decision

def test_type_args_block_fp():
    q = parse("std::list<int> 怎么遍历")
    assert q.type_args == ("int",)
    assert q.fingerprint_eligible is False

def test_alias_concept_still_fp():
    q = parse("生成一个 rbtree")
    assert q.subject_id == "red_black_tree"
    assert q.fingerprint_eligible is True

def test_multi_subject():
    q = parse("C++ list 和 vector 有什么区别")
    assert q.multi_subject is True and q.fingerprint_eligible is False

def test_no_lang_ambiguous_word_miss():
    q = parse("写一个 list")          # 无语言歧义 → subject None
    assert q.subject_id is None and q.fingerprint_eligible is False

def test_lang_gating_weak_words():
    assert parse("go to the next step").language is None
    assert parse("go 语言的 map").language == "golang"
