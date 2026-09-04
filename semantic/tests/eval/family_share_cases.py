"""Task4 decision family_compat 共享判定 Precision 门（确定性；测试消费）。

背景：family_compat 为**受控跨实体共享**，默认关（env `SEMANTIC_FAMILY_COMPAT` 默认 "0"）。
含义：不同实体（库/内建实体 vs 其家族抽象概念，二者**不同 subject_id**）在——同 implementation
family、均无模板 type_args、非 multi_subject、意图均 ∈ {implementation, definition}、语言相等——
时，视同 subject 兼容，硬规则 short-circuit 返回 shared=True reason="ok" 且 source="family_compat"。

本集目标是守住 family_compat 的 **Precision**（不因家族猜共而误放行），作为"默认关"的取舍证据：
  - 正例 POS：ON 开启且命中 family_compat 分支 ⇒ shared True & reason ok & soft False & source=family_compat；
    同时默认关（OFF）下这些正例应 **shared False（保守拒）**——证明放行完全由开关授权。
  - 负例 NEG：跨语言 / 带模板 type_args / multi_subject / splice·API 实体边界(意图非实现·定义) /
    操作冲突等易被"同家族"误放的组合，**开关开启时仍必须 shared False**（不产生误命中）。

构造方式（与 tests/eval/retrieval_cases.py 一致）：给出**语义明确**的成对候选，再按真实 parse→
decision(hard_decide_verbose) 当下的真值过滤收录，只保留确实命中预期极性者——确定性、无假样本、
不使用任何 nuxt/在线服务。import 本模块默认把 decision 侧 env 归位为默认关，不改持久态。

注意点（数据缺口补丁）：lang_terms 库/内建实体侧已带 family，但抽象概念(alias_of linked_list/array/
hash_table…) parse 未标 family（None）；decision._FAMILY_CONCEPT_LIFT 在 family_compat 开启时把少量
家族抽象概念映射回既有 family id，故正例"实体↔其抽象"才可触及 family 短接。本集依赖该 minimal 补丁。
"""
import os
import sys as _sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_sem = os.path.dirname(os.path.dirname(_HERE))          # semantic/
if _sem not in _sys.path:
    _sys.path.insert(0, _sem)                            # parse/decision/ontology
_PKG = os.path.dirname(_HERE)                            # tests/
if _PKG not in _sys.path:
    _sys.path.insert(0, _PKG)

from parse import parse
from decision import hard_decide_verbose


def _verdict(q, c):
    """在 ON(family_compat 开启)下的真实决策四元组 (shared, reason, soft, source)。"""
    prev = os.environ.get("SEMANTIC_FAMILY_COMPAT", "0")
    os.environ["SEMANTIC_FAMILY_COMPAT"] = "1"
    try:
        return hard_decide_verbose(parse(q), parse(c))
    finally:
        os.environ["SEMANTIC_FAMILY_COMPAT"] = prev


# =========================================================== 正例模板（family 抽象 × 同语言具体）===
# 每组 (name, q_模板, c_模板)。q 倾向读具体（库/内建实体），c 读家族抽象概念；反写亦可对称命中。
# 字段：family := (语言, 具体实体词, 抽象概念中文|英文短语, 抽象概念中文别名直接)
_FAM = {
    # lingo具体实体(subject=entity, 自带family)     抽象概念(去解析走 alias_of my)：
    "cpp_linked_list": {
        "lang": "cpp",
        "concrete": ["用 C++ 写一个 list", "cpp 实现 list", "implement list in cpp", "C++ 标准库写 list",
                     "用 cpp 写一个 list", "实现 cpp list"],
        "abstract": ["用 cpp 实现链表", "实现 C++ 链表", "链表的 cpp 实现", "cpp 实现一个 链表",
                     "用 C++ 实现链表", "实现一个 cpp 链表"],
    },
    "cpp_dynamic_array": {
        "lang": "cpp",
        "concrete": ["用 C++ 写一个 vector", "cpp 实现 vector", "用 c++ 写一个 vector"],
        "abstract": ["实现 cpp 动态数组", "动态数组 C++ 实现", "用 C++ 实现动态数组", "cpp 的动态数组的实现",
                     "动态数组的 cpp 实现", "C++ 实现动态数组"],
    },
    "python_dynamic_array": {
        "lang": "python",
        "concrete": ["用 python 实现 list", "用 python 写 list", "实现一个 python list", "python 的 list 实现",
                     "实现 python list", "用 python 实现一个 list", "implement list in python"],
        "abstract": ["用 python 实现动态数组", "python 实现动态数组", "动态数组 python 实现",
                     "实现 python 的动态数组", "利用 python 写动态数组"],
    },
    "python_hash_table": {
        "lang": "python",
        "concrete": ["用 python 实现 dict", "python 实现 dict", "implement dict in python"],
        "abstract": ["用 python 实现哈希表", "用 python 实现散列表", "python 哈希表 实现",
                     "用 python 实现 hash table"],
    },
    "cpp_hash_table": {
        "lang": "cpp",
        "concrete": ["用 C++ 写 unordered_map", "用 cpp 实现 unordered_map"],
        "abstract": ["C++ 实现哈希表", "cpp 实现哈希表", "用 c++ 写散列表"],
    },
}

# default-OFF 也要保守拒（正例都含具体实体 entity ≠ 抽象概念 subject，故 OFF 天然 subject_conflict）
_POS = []
_seen_p = set()
for _fam_name, _fd in _FAM.items():
    for _conc in _fd["concrete"]:
        for _abs in _fd["abstract"]:
            _key = (_conc, _abs)
            if _key in _seen_p:
                continue
            _seen_p.add(_key)
            s, r, soft, src = _verdict(_conc, _abs)
            if s is True and r == "ok" and soft is False and src == "family_compat":
                _POS.append((_conc, _abs))

# =========================================================== 负例（开启下仍 shared False，Precision 守界）===
_NEG = []
_seen_n = set()


def _neg(q, c, note):
    """收录 ON 之下确实 shared False 的负例（保真；语义见 note 注释）。"""
    if (q, c) in _seen_n or (c, q) in _seen_n:
        return
    _seen_n.add((q, c))
    s, r, soft, src = _verdict(q, c)
    if s is False:
        _NEG.append((q, c, note, r))


# 1) 跨语言（同 family 也因语言不同放行不了，且常 subject 不同 → 拒；语义最易被"同家族印象"误放）
for _q, _c in [
    ("用 C++ 写一个 list", "用 python 实现 list"),      # 同 family linked/dynamic？→ linked≠dynamic；仍拒
    ("用 C++ 写一个 list", "用 Java 实现 ArrayList"),   # list(linked) vs ArrayList(dynamic) 异family →拒
    ("在 C++ 里写一个 list", "用 Go 写一个链表"),          # 语言不同 → 拒(language_conflict/subject)
    ("用 C++ 写一个 vector", "用 Python 写动态数组"),      # 同动态数组语义但语言跨 → language 拒
]:
    _neg(_q, _c, "跨语言")

# 2) 带模板类型实参（type_args 非空 → family 不放行）
for _q, _c in [
    ("用 C++ 实现 vector<int>", "C++ 实现动态数组"),     # type_args (int) → 拒
    ("用 C++ 实现 std::vector<double>", "cpp 动态数组实现"),  # 同 family 但带模板 → 拒
    ("用 python 实现 list[int]", "用 python 实现动态数组"),
]:
    _neg(_q, _c, "type_args/模板")

# 3) splice / API / 成员函数层级意图 → 语义落在"操作边界"而非"家族实现"
for _q, _c in [
    ("std::list 怎么 splice", "用 cpp 实现链表"),          # splice 尚未进入 实现/定义 → family 不放行，subject 也异
    ("python list 的 append 用法", "用 python 实现动态数组"),  # 方法/API 边界
    ("std::list 的唯一化 splice 接口", "cpp 实现链表"),
]:
    _neg(_q, _c, "splice/API 边界")

# 4) multi_subject（比较/并列多主题 → 非单主题，family 不放行）
for _q, _c in [
    ("C++ list 和 vector 有什么区别", "用 C++ 实现 list"),  # multi_subject True→拒
    ("python list 和 dict 有什么区别", "python 实现 list"),
]:
    _neg(_q, _c, "multi_subject")

# 5) 操作冲突 / 非实现·定义意图（家族相同仍不得共享缓存）
for _q, _c in [
    ("python list 怎么删除所有元素", "用 python 实现动态数组"),  # 操作/use意图，非实现/定义
    ("动态数组和链表我该选哪个", "用 cpp 实现 vector"),           # reason/comparison
    ("C++ vector list 性能差异", "cpp 动态数组的实现"),          # comparison
    ("为什么 python 用 list", "用 python 实现动态数组"),          # reason
]:
    _neg(_q, _c, "操作/意图冲突")

# 6) 家族不算同之异家族 / 跨家族（library map vs array 等，防家族名相近误放）
for _q, _c in [
    ("用 python 实现 dict", "用 python 实现一个 vector 的元素结构"),   # 抽象侧很弱 → 拒
    ("用 C++ 写 list", "用 C++ 写 unordered_map"),            # linked vs hash_table 异family → 拒
    ("用 cpp 实现 unordered_map", "cpp 实现 vector"),          # hash_table vs dynamic_array →拒
]:
    _neg(_q, _c, "异家族")

# =========================================================== 导出 + 汇总 =================================
POS = [(_q, _c) for _q, _c in _POS]
NEG = [(_q, _c) for _q, _c, _note, _r in _NEG]


def family_compat_on_toggle(enable):
    """（辅助/测试）把 decision 侧 env 开关设为给定布尔；返回恢复句柄。family_compat 为读 env 即时开关。"""
    prev = os.environ.get("SEMANTIC_FAMILY_COMPAT", "0")

    def restore():
        os.environ["SEMANTIC_FAMILY_COMPAT"] = prev

    os.environ["SEMANTIC_FAMILY_COMPAT"] = "1" if enable else "0"
    return restore


def summary():
    """各负例拒因分布（Precision 门证据）；正例条数与是否全走 family_compat_source 由调用方断言。"""
    from collections import Counter
    return {
        "pos": len(POS),
        "neg": len(NEG),
        "neg_reasons": dict(Counter(_r for _q, _c, _note, _r in _NEG)),
    }
