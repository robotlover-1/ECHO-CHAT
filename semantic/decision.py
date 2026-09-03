"""决策：subject_id/language/operation/intent/residual 保守对称硬拒，然后给分。"""
import jieba
from jieba import analyse
from parse import ParsedQuery

LANGSENS = {"implementation", "troubleshooting", "code_modification", "execution"}

def _subject_conflict(qp, cp) -> bool:
    if qp.subject_id and cp.subject_id:
        return qp.subject_id != cp.subject_id
    # 无 subject_id 一侧：仅 normalize subject_text 完全相等才不算冲突；任一为空按冲突保守处理
    if not qp.subject_text or not cp.subject_text:
        return True
    return qp.subject_text != cp.subject_text

def _language_sensitive(qp, cp) -> bool:
    return (qp.intent in LANGSENS or cp.intent in LANGSENS
            or qp.output_type == "code" or cp.output_type == "code")

def keyword_score(q, c) -> float:
    kw1 = set(analyse.extract_tags(q, topK=6))
    kw2 = set(analyse.extract_tags(c, topK=6))
    if not (kw1 | kw2):
        return 1.0
    return len(kw1 & kw2) / len(kw1 | kw2)

def decide(qp: ParsedQuery, cp: ParsedQuery):
    # 1 subject
    if _subject_conflict(qp, cp):
        return 0.0, False, ("unknown_subject" if (qp.subject_id is None or cp.subject_id is None) else "subject_conflict")
    # 2 language（对称，None 非通配；相等或双方 None 放行）
    if _language_sensitive(qp, cp) and qp.language != cp.language:
        return 0.0, False, "language_conflict"
    # 3 operation 保守（任一侧不同即拒；同值非 None 放行；双方 None 进意图）
    if qp.operation != cp.operation:
        return 0.0, False, "operation_conflict"
    # 4 intent
    if qp.operation is not None and qp.operation == cp.operation:
        pass  # 同操作放行（跨意图），跳过意图检查
    elif qp.intent in ("unknown",) or cp.intent in ("unknown",):
        return 0.0, False, "unknown_intent"
    elif qp.intent != cp.intent:
        return 0.0, False, "intent_conflict"
    # 5 residual 复核
    if qp.residual_words != cp.residual_words:
        return 0.0, False, "constraint_conflict"
    return keyword_score(qp.raw_text, cp.raw_text), True, "ok"
