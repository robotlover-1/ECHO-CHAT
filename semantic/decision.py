"""决策：subject/language/operation/intent/residual 保守对称硬拒 + 可选 semantic_soft fallback。

纯规则，**不含模型分**。主入口 `hard_decide(qp, cp) -> (shared, reason, soft)`：
    - shared=True 表示二者可共享同一缓存条目（或至少是允许命中的 soft 语义匹配）；
    - reason 取值：ok / language_conflict / operation_conflict / intent_conflict /
      constraint_conflict / subject_conflict / unknown_subject / unknown_intent /
      semantic_soft_match；
    - soft 是受约束的兜底通道标志：subject 硬门因"缺 id"拟拒(unknown_subject)时才有机会。

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


def _soft_fallback_enabled() -> bool:
    """soft 通道开关：SEMANTIC_SOFT_FALLBACK，默认 '0'（关闭，走保守未知拒绝）。"""
    return os.environ.get("SEMANTIC_SOFT_FALLBACK", "0") == "1"


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


def hard_decide(qp: ParsedQuery, cp: ParsedQuery):
    """纯规则对称硬拒 + soft 兜底；返回 (shared, reason, soft)。不含任何模型分。"""
    # 1 subject（缺 id 侧先拟拒，交由 soft 通道再判）
    conf, subj_reason = _subject_conflict(qp, cp)
    if conf:
        if subj_reason == "unknown_subject" and _soft_fallback_enabled():
            if _soft_path(qp, cp):
                return True, "semantic_soft_match", True
        return False, subj_reason, False
    # 2 language（对称，None 非通配；相等或双方 None 放行）
    if _language_sensitive(qp, cp) and qp.language != cp.language:
        return False, "language_conflict", False
    # 3 operation 保守（任一侧不同即拒；同值非 None 放行；双方 None 进意图）
    if qp.operation != cp.operation:
        return False, "operation_conflict", False
    # 4 intent
    if qp.operation is not None and qp.operation == cp.operation:
        pass  # 同操作放行（跨意图），跳过意图检查
    elif qp.intent in ("unknown",) or cp.intent in ("unknown",):
        return False, "unknown_intent", False
    elif qp.intent != cp.intent:
        return False, "intent_conflict", False
    # 5 residual 复核（沿用 Phase-1 残差相等判 constraint_conflict，reject 不变）
    if qp.residual_words != cp.residual_words:
        return False, "constraint_conflict", False
    # ok：纯规则命中，无 score。
    return True, "ok", False
