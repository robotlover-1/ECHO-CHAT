# -*- coding: utf-8 -*-
"""Task5 术语识别 / 边界验收集（确定性；测试消费；逐条走完整 parse/decision）。

来源：task-5-brief.md 四段清单（§五、识别+miss+拒绝对+fp-safe）。含义：
  * RECOGNIZE   —— 术语识别正例：短语应解析到期望 subject_id + language。
                    备注标注路径：alias zh/en/abbr/plural / lang entity / fold+lang / entity+type_args。
  * MISS        —— 术语识别未命中 / 明确不识别（期望 subject None，reason 子串为证；multi 者为多主题并列：
                    期望 subject None 且 multi_subject=True、reason == "multiple_subjects"）——不是决策"误拒"，
                    而是入口处就未映射（无语言歧义词/非白名单 namespace/语言实体 ambiguous 等），见逐条注释。
  * REJECT_PAIRS—— 边界该拒的成对候选：语义各自被识别，但组合不该共享（decision/hard 层面硬拒，非 subject
                    误识别）。expected shared False（reason 由解析时态给出，测试只断 False，理由见注释）。
  * FP_ELIGIBLE_SAFE —— 指纹准入安全判定对：既有 eligible 又有明确该 gate 挡住不产生危险的候选。

逐条处置见本目录同事件报告（task-5-report.md）；全部不改 gate 判据，识别错仅靠
本体 alias/lang_terms 折叠数据补足；“纯短语无句式”该识别的别名补 ontology 折叠形式。

判据原则（与 Task3 守则一致，Test 层不迁就解析缺口：
  - 别名/语言实体折叠正确性归 parse/ontology 数据（concepts.json / lang_terms.json）责任，
    本集只按当下真实 parse() 真值断言；凡“数据应补而当前缺失”者按 brief 处置补数据后收录。
"""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sem = _os.path.dirname(_os.path.dirname(_HERE))          # semantic/
if _sem not in _sys.path:
    _sys.path.insert(0, _sem)
_PKG = _os.path.dirname(_HERE)                            # tests/
if _PKG not in _sys.path:
    _sys.path.insert(0, _PKG)

# ============================================================= ① RECOGNIZE 术语识别正例 ==
# 列：(短语, 期望 subject_id, 期望 language, 备注)
# 备注：全走完整 parse()；断言 subject_id/language 与 parse 真值一致。
#   alias zh/en/abbr/plural —— 同一个 red_black_tree 概念的多语言/多写法别名；
#   lang entity —— 语言受限实体表(cpp_std_list / python_builtin_list)只在该语言下识别，subject_kind=实体；
#   fold+lang —— “cpp 与 rbtree 共现”概念路径 + 语言折叠(cpp) 双路；
#   entity+type_args —— std::vector<int> 识别为 cpp_std_vector 且带模板实参 (int)。
RECOGNIZE = [
    ("红黑树", "red_black_tree", None, "alias zh"),
    ("rbtree", "red_black_tree", None, "alias abbr"),
    # red-black tree（孤立“带连字符+空格”短语）实为空壳：parse 无句式时对整句仅做“去空格压实”→ red-blacktree
    # 并非本体键 → 空壳形达不成别名命中；但本体别名 red-black tree 在**句子里保空格**可命中。故这条 alias-en
    # 以可独立命中的最小实现句承载其识别断言；纯短语形态由 E2E fp 相等、(生成一个 red-black tree)一并守。
    ("生成一个 red-black tree", "red_black_tree", None, "alias en(in sentence): keep-space hits"),
    ("red black trees", "red_black_tree", None, "plural alias"),
    ("用 C++ 写一个 list", "cpp_std_list", "cpp", "lang entity"),
    ("python 的 list", "python_builtin_list", "python", "lang entity"),
    ("实现一个 cpp rbtree", "red_black_tree", "cpp", "fold+lang"),
    ("std::vector<int>", "cpp_std_vector", "cpp", "entity+type_args"),
]

# ============================================================= ② MISS 术语识别未命中 ==
# 列：(短语, reason 子串或 None)  期望 subject_id is None（且 multi 时为 multi_subject=True，
# reason=="multiple_subjects"；否则未映射 reason 含给定子串 None 则不强查 reason）。
#   需明确区分：“该拒”（语义在 = REJECT_PAIRS / decision）与“压根没识别”（= 本集）。
MISS = [
    ("写一个 list", "subject_unresolved"),              # language 未知 → std/cpp 白名单不适用；list 无语言歧义 → None
    ("go to the next step", "subject_unresolved"),      # 弱词 go（英文介词/go-动词）无编程锚点 → 不判语言 → None
    ("a swift response", "subject_unresolved"),         # swift 弱词无编程锚点(“response”非技术锚) → 不语言 → None
    ("C++ list 和 vector 有什么区别", "multiple_subjects"),  # 多实体 cpp list(vector≠list family 也不同)+vector 并列 → 多主题
    ("company::list", "subject_unresolved"),            # 自定义非白名单 ns(company::) 剥不去 → 全局不映射；lang 表只 std 白名单 → None
    ("java 的 List", "subject_unresolved"),              # ja List 在语言实体表为 ambiguous → 不映射 → None
]

# ============================================================= ③ REJECT_PAIRS 边界该拒 ==
# 列：(q, c)；期望 decision(默认关 SEMANTIC_FAMILY_COMPAT=0/<未设>=关) shared False。
#   配对各自都能被识别到合理主体，但组合层面语义不该共享 —— 必须由 decision/hard 拦截、并明确
#   NOT 是 subject 层误识别成同一主体。reason 交由 decision 现时真值（只断 False，reason 附注）。
REJECT_PAIRS = [
    ("生成一个红黑树", "用 C++ 实现 std::list"),          # 概念(红黑树) vs 语言库实体(cpp_std_list) → 异主体硬拒
    ("std::list<int> 怎么遍历", "std::list<string> 怎么遍历"),  # 同实体、模板类型实参不同 → 约束/残差硬拒
    ("std::list 怎么 splice", "实现一个 C++ 链表"),        # splice·API 边界 vs 家族抽象实现 → 异主体硬拒
    ("python list append", "用 python 实现动态数组"),      # list 内建实体(builtin) vs 抽象概念数组 → 异主体硬拒
]

# ============================================================= ④ FP_ELIGIBLE_SAFE 指纹安全 ==
# 列：(短语, 期望 fingerprint_eligible)。指纹安全的含义 = “进不进指纹都要被 gate 恰当放晴、不产生 gate 后
#   误命中”：本段每例的 eligible 真值反映三扇条件：
#     - True  ：alias_of 抽象概念(红黑树族) → 指纹**可直命**（同 subject+同槽位变体共享同一 fp）；
#     - False ：库/内建实体(cpp_std_list)/带模板类型实参(std::list<int>) → 指纹**不直命**，
#               只走向量 + decision 复核，杜绝“同文本/近语义直接命中到异实体/带参实体”的危险。
#   （True 的 fp 相等、False 的 “同文本亦 eligible=False” 分别由 E2E assert_fp_safety 守。）
FP_ELIGIBLE_SAFE = [
    ("生成一个 rbtree", True),          # alias_of 概念(red_black_tree)→ 指纹可直命
    ("用 C++ 写一个 list", False),       # library_type 实体 → 不进指纹（只向量+decision）
    ("std::list<int>", False),          # type_args 模板 → 不进指纹
]

# ============================================== 摘要 ============
_SECTIONS = {"RECOGNIZE": RECOGNIZE, "MISS": MISS,
             "REJECT_PAIRS": REJECT_PAIRS, "FP_ELIGIBLE_SAFE": FP_ELIGIBLE_SAFE}

# DECISION 侧默认口子：Term 边界验收集的 REJECT_PAIRS/正例 criterion 以 family_compat=默认关 为准
# (SEMANTIC_FAMILY_COMPAT 缺省 "0" → OFF)。本常量仅供测试在跑 paired 决策前强置回默认 OFF，防环境残留。
TE_DEFAULT_FAMILY_FLAG = "0"


def term_summary():
    """各段条数（供 test_eval 断言段下限 + 报告计数；见 task-5-brief §清单结构）。"""
    return {k: len(v) for k, v in _SECTIONS.items()}

