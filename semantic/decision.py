"""决策：subject_id/language/operation/intent/residual 保守对称硬拒，然后给分。

score 语义（Go 消费）：
    - ok 分支：canonical 嵌入余弦 = 二者"等价格等价度"（别名经 subject_id 归一共享桶 →
      跨语言/跨措辞"应命中"对 cosine 高）。Go accept 谓词 shared && reason=="ok"
      && score >= rerank_threshold（0.25，来自 config）。
    - reject/unknown 分支：score 一律 0.0。
注意 import 方向无环：decision → (parse, embedding)；embedding → parse；embedding 不 import decision。
"""
from parse import ParsedQuery
from embedding import embed_text

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

def _cos(a, b):
    """两向量点积（embed 已 L2 归一 ⇒ 点积=余弦）。"""
    return sum(x * y for x, y in zip(a, b))

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
    # ok：canonical 嵌入余弦（等价格等价度）。别名经 subject_id 归一共享桶（红黑树/rbtree 同桶），
    # 故跨语言/跨措辞"应命中"对余弦高；阈值 0.25 来自 Go config rerank_threshold。
    return _cos(embed_text(qp.raw_text), embed_text(cp.raw_text)), True, "ok"
