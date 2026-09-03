"""embedding.py —— canonical 主题桶加权词面嵌入（semantic Phase1 Task5）

把 semantic.py 的 EMBED_DIM/_hash_bucket/_add 与 embed_text 算法原样搬入，但主题桶改为 canonical：
    parse(text) 给出 subject_id + subject_text；
    主题桶优先用 subject_id（别名共享同一桶 → "红黑树"/"rbtree" 归一为 red_black_tree）；
    subject_id 缺失时才回退 subject_text。

其余权重逐字保留语义.py 方案：INTENT(2.0) + 普通词(1.0，模板词 0) + bigram(0.4) + L2 归一（256 维，FNV）。

纯逻辑模块：不 import nuxt，py3.8 兼容。
注意：import parse 时会执行 jieba.add_word 原子化若干约束描述词（线程安全/持久化等），
这是 parse.py（Task2）已知的全局行为，embedding 无需规避，也未改 parse。
"""
import math
import jieba

from parse import parse  # noqa: E402 —— 解析/主题归一化权交给 parse（Task2）

# 无信息模板词：嵌入权重 0。注意"实现/简单"等是意图/主题词，不在此列（结构化保存）。
# semantic.py 停用表未导出，故在此保留等价副本（与 semantic 服务保持一致）。
STOP_WORDS = {
    "一个", "一下", "请问", "帮我", "可以", "简单地", "请", "能否", "让我", "帮我",
    # 通用疑问/模板词：无内容信息，去掉避免"怎么减肥 vs 怎么学好英语"这类跨主题误命中
    "怎么", "如何", "为什么", "为何", "推荐", "哪些", "多少", "怎样", "为啥",
    "怎么办", "是不是", "有没有", "是否", "什么", "是", "吗", "呢", "吧", "啊",
    "应该", "需要", "想要", "请问一下", "一下",
}

EMBED_DIM = 256


def _hash_bucket(s, dim):
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    return h % dim


def _add(vec, s, w):
    vec[_hash_bucket(s, EMBED_DIM)] += w


def embed_text(text):
    """加权词面嵌入（256 维，L2 归一）。

    SUBJECT:canonical(3.0) + INTENT(2.0) + 普通词(1.0，模板词 0) + bigram(0.4)。
    主题经 parse(text) 取 subject_id/subject_text：
        sub = subject_id or subject_text
    即 id 命中时别名共享同一 canonical 桶（红黑树/rbtree 都落 SUBJECT:red_black_tree）→ 高余弦。
    """
    vec = [0.0] * EMBED_DIM
    q = parse(text)
    sub_id, sub_txt = q.subject_id, q.subject_text
    sub = sub_id or sub_txt
    intent = q.intent
    if sub:
        _add(vec, "SUBJECT:" + sub, 3.0)
    if intent != "unknown":
        _add(vec, "INTENT:" + intent, 2.0)
    for w in jieba.cut(text):
        w = w.strip()
        if w and w not in STOP_WORDS:
            _add(vec, w, 1.0)
    chars = [c for c in text if not c.isspace()]
    for i in range(len(chars) - 1):
        _add(vec, chars[i] + chars[i + 1], 0.4)
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec
