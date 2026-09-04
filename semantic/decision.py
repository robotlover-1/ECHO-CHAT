"""决策：subject/language/operation/intent/residual 保守对称硬拒 + 可选 semantic_soft fallback。

纯规则，**不含模型分**。主入口 `hard_decide(qp, cp) -> (shared, reason, soft)`：
    - shared=True 表示二者可共享同一缓存条目（或至少是允许命中的 soft 语义匹配）；
    - reason 取值：ok / language_conflict / operation_conflict / intent_conflict /
      constraint_conflict / subject_conflict / unknown_subject / unknown_intent /
      semantic_soft_match；
    - soft 是受约束的兜底通道标志：subject 硬门因"缺 id"拟拒(unknown_subject)时才有机会。

Task4：可选 **family_compat**（受控跨实体共享，env `SEMANTIC_FAMILY_COMPAT` 默认 "0" 关闭）——
仅当开启时，在 subject 硬门之后、language 判定之前短接尝试：两侧不同实体但属于**同一
implementation family**（库/内建实体 vs 其抽象 family 概念，或两个同 family 实体，均无模板
type_args、非 multi_subject、意图均 ∈ implementation/definition、语言相等）→ 视同 subject 兼容，
直接返回 (shared=True, reason="ok", soft=False)，且决策响应 source="family_compat"。
关闭时（默认）不做任何放行，完全保持原有的保守 subject 判定（不同实体 → 拒）。

`source` 语义（HTTP /v1/decision 与 /batch 透传补充字段）：默认 "hard"；仅 family_compat 命中时
为 "family_compat"。家庭开关默认关 => family_compat 分支从不触发 => source 恒默认值，旧字段全不变。

历史 note：decision 层曾返回 canonical 嵌入余弦作为 score（Phase1）。e5 绝对余弦跨主题饱和
(~0.84)，模型分天然不适合做 decision 内信号 → Phase2/3 已把 scoring 移交给 VSEARCH/Go，
此处不再产出任何分；拒绝集/类别语义与 Phase1 完全一致（reject reason 逐一保留），
唯一行为变化是把打分入口 `decide`(含 score) 改为纯规则 `hard_decide`(shared/reason/soft)。

import 方向无环：decision → (parse)；decision 不 import embedding/models。
"""
import os
from parse import ParsedQuery
from parse import critical_constraints

LANGSENS = {"implementation", "troubleshooting", "code_modification", "execution"}

# Task4 的最小数据缺口补丁：lang_terms 库/内建实体侧已带 family（linked_list/dynamic_array/…
# Task3 parse），但**抽象 family 概念**（alias_of 概念：linked_list/array/hash_table…）未标 family
# （concept 命中路径 implementation_family 为 None）。为让 family_compat 的"实体↔同 family 抽象"
# 正向对在纯 decision 内落地（受控、默认关），记 minimal 概念 id→family 提升表承载语言受限实体
# family 的既有规范 id。仅当开关开启且该侧为 alias_of 概念（subject_kind 为空）才参与提升。
_FAMILY_CONCEPT_LIFT = {
    "linked_list": "linked_list",
    "array": "dynamic_array",
    "hash_table": "hash_table",
}


def _soft_fallback_enabled() -> bool:
    """soft 通道开关：SEMANTIC_SOFT_FALLBACK，默认 '0'（关闭，走保守未知拒绝）。"""
    return os.environ.get("SEMANTIC_SOFT_FALLBACK", "0") == "1"


def _family_compat_enabled() -> bool:
    """family 兼容开关：SEMANTIC_FAMILY_COMPAT，默认 '0'（关闭，保守拒 cross-entity）。"""
    return os.environ.get("SEMANTIC_FAMILY_COMPAT", "0") == "1"


def _impl_family(qp):
    """读取侧 implementation_family；alias_of 抽象概念无 family 时经 minimal 概念提升表补。
    仅当 qp.subject_id 是 alias_of 概念(kind 空)且落在提升表才补；库/内建/类实体本身已带 family。"""
    fam = getattr(qp, "implementation_family", None)
    if fam:
        return fam
    if (getattr(qp, "subject_id", None) and getattr(qp, "subject_kind", None) is None):
        return _FAMILY_CONCEPT_LIFT.get(qp.subject_id)
    return None


def _family_compat(qp, cp) -> bool:
    """family 兼容判定（仅调用方已确认开关开启才进入）。纯条件，不抛错。
    满足时视同 subject 兼容（调用方直接以 reason=ok/source=family_compat/soft=False 返回）。"""
    if getattr(qp, "multi_subject", False) or getattr(cp, "multi_subject", False):
        return False
    fam_q = _impl_family(qp)
    fam_c = _impl_family(cp)
    if not fam_q or not fam_c or fam_q != fam_c:
        return False
    if getattr(qp, "type_args", ()) or getattr(cp, "type_args", ()):
        return False
    return (getattr(qp, "subject_id", None) != getattr(cp, "subject_id", None)
            and getattr(qp, "intent", None) in ("implementation", "definition")
            and getattr(cp, "intent", None) in ("implementation", "definition")
            and getattr(qp, "language", None) == getattr(cp, "language", None))


def _subject_conflict(qp, cp):
    """主题硬门。返回 (conflict:bool, reason_or_None)。reason 在缺 id 侧为 unknown_subject。"""
    if qp.subject_id and cp.subject_id:
        if qp.subject_id != cp.subject_id:
            return True, "subject_conflict"
        return False, None
    # 至少一侧无 subject_id：仅 normalize subject_text 完全相等才算不冲突；任一为空按冲突保守
    if not qp.subject_text or not cp.subject_text:
        return True, "unknown_subject"
    if qp.subject_text != cp.subject_text:
        return True, "unknown_subject"
    return False, None


def _language_sensitive(qp, cp) -> bool:
    return (qp.intent in LANGSENS or cp.intent in LANGSENS
            or qp.output_type == "code" or cp.output_type == "code")


def _soft_path(qp, cp) -> bool:
    """soft 兜底判定（subject 硬门已因'缺 id'拟拒时调用）。

    Phase2 保守条件（任一不满足 → 不 soft）：
      - 任一侧有 subject_id 而另一侧无 → False（禁止半边缺失时跨主题混淆）；
      - intent 均已知且相等；
      - 语言敏感意图下语言同值/双空（禁止 python 溢出命中 go）；
      - operation 相等或双空（同等较)；
      - not (critical_constraints(qp) 与 critical_constraints(cp) 交集差非空)——
        一侧宣称的硬约束另一侧完全未满足则不放行。全部满足 → True（soft_flag）。
    """
    if (bool(qp.subject_id) != bool(cp.subject_id)):
        return False
    if qp.intent in ("unknown",) or cp.intent in ("unknown",):
        return False
    if qp.intent != cp.intent:
        return False
    # 语言：敏感意图下须同值或双空
    if _language_sensitive(qp, cp):
        if qp.language != cp.language:
            return False
    # operation：相等或双空
    if qp.operation != cp.operation:
        return False
    # critical 约束：任一侧存在对方未满足的约束 → 不放行
    qcc = critical_constraints(qp)
    ccc = critical_constraints(cp)
    if (qcc - ccc) or (ccc - qcc):
        return False
    return True


def _decide_verbose(qp: ParsedQuery, cp: ParsedQuery):
    """纯规则对称硬拒 + soft 兜底 + source；返回 (shared, reason, soft, source)。
    相对 hard_decide 仅多出末位 source；HTTP /v1/decision 透传用（source 旧默认 "hard"）。"""
    # 0 family_compat：subject/概念实体化解析已发生（subject_id/implementation_family 已知）。
    #    语义上属"subject 判定之后、语言判定之前"的受控短接：当不同实体但同 family/无模板/
    #    非多主题/意图{implementation,definition}/语言相等 → 用该家族一致**覆盖**两实体不等之
    #    拟拒，视同 subject 兼容直接 ok（soft False，source=family_compat），不再进语言/操作/意图。
    #    关闭（默认）此分支不触发 → 原保守 subject 判定（不同实体 → 拒）保持。
    if _family_compat_enabled() and _family_compat(qp, cp):
        return True, "ok", False, "family_compat"
    # 1 subject（缺 id 侧先拟拒，交由 soft 通道再判）
    conf, subj_reason = _subject_conflict(qp, cp)
    if conf:
        if subj_reason == "unknown_subject" and _soft_fallback_enabled():
            if _soft_path(qp, cp):
                return True, "semantic_soft_match", True, "hard"
        return False, subj_reason, False, "hard"
    # 2 language（对称，None 非通配；相等或双方 None 放行）
    if _language_sensitive(qp, cp) and qp.language != cp.language:
        return False, "language_conflict", False, "hard"
    # 3 operation 保守（任一侧不同即拒；同值非 None 放行；双方 None 进意图）
    if qp.operation != cp.operation:
        return False, "operation_conflict", False, "hard"
    # 4 intent
    if qp.operation is not None and qp.operation == cp.operation:
        pass  # 同操作放行（跨意图），跳过意图检查
    elif qp.intent in ("unknown",) or cp.intent in ("unknown",):
        return False, "unknown_intent", False, "hard"
    elif qp.intent != cp.intent:
        return False, "intent_conflict", False, "hard"
    # 5 residual 复核（沿用 Phase-1 残差相等判 constraint_conflict，reject 不变）
    if qp.residual_words != cp.residual_words:
        return False, "constraint_conflict", False, "hard"
    # ok：纯规则命中，无 score。
    return True, "ok", False, "hard"


def hard_decide_verbose(qp: ParsedQuery, cp: ParsedQuery):
    """带 source 的决策：返回 (shared, reason, soft, source)。供 /v1/decision 透传。"""
    return _decide_verbose(qp, cp)


def hard_decide(qp: ParsedQuery, cp: ParsedQuery):
    """纯规则对称硬拒 + soft 兜底；返回 (shared, reason, soft)。不含任何模型分。
    （源完全来自 _decide_verbose，本函数剥去末位 source 保持既有 3 元契约不变。）"""
    return _decide_verbose(qp, cp)[:3]
