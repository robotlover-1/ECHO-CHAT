import pytest
from ontology.loader import lookup_lang_entity, LANG_TERMS_VERSION, load_lang_terms


def test_version_constant():
    assert LANG_TERMS_VERSION == "2026-09-04.1"


def test_cpp_list_entity():
    ms = lookup_lang_entity("cpp", "实现一个 list")
    assert [m.entity_id for m in ms] == ["cpp_std_list"]
    assert ms[0].family == "linked_list" and ms[0].kind == "library_type"


def test_python_list_entity():
    ms = lookup_lang_entity("python", "python 的 list 复杂度")
    assert ms[0].entity_id == "python_builtin_list"
    assert ms[0].family == "dynamic_array"


def test_java_list_ambiguous():
    assert lookup_lang_entity("java", "List") == []        # ambiguous → 不映射


def test_type_args_kept():
    ms = lookup_lang_entity("cpp", "std::list<int>")
    assert ms[0].entity_id == "cpp_std_list" and ms[0].type_args == ["int"]


def test_data_loaded_and_has_langs():
    # load_lang_terms 返回 languages: dict[lang, dict[term, entity]]；校验非法值即抛异常
    langs = load_lang_terms()
    assert set(langs) >= {"cpp", "python", "java"}


def test_two_terms_same_sentence_both_hit():
    # 同一句两个不同词分别命中→返回全部(供多主题判定)
    ms = lookup_lang_entity("cpp", "对比 list 和 vector 的复杂度")
    assert len(ms) == 2
    assert {m.entity_id for m in ms} == {"cpp_std_list", "cpp_std_vector"}


@pytest.mark.parametrize("lang,text", [
    ("csharp", "list"),      # 未知语言
    ("cpp", ""),             # 空文本
    ("cpp", "毫无命中内容"),   # 无命中词
    ("java", "ArrayList"),   # mapped 应命中(单个) —— 单独验证
])
def test_unsupported_or_no_match(lang, text):
    ms = lookup_lang_entity(lang, text)
    if lang == "java" and text == "ArrayList":
        assert [m.entity_id for m in ms] == ["java_array_list"]
    else:
        assert ms == []
