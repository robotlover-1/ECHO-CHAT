from nuxt import route, logger, Request
from nuxt.repositorys.validation import fields, use_args
import traceback
import re
import tiktoken
import math
import jieba
from jieba import analyse

encoding_cache = {}

support_models = set(["gpt-3.5-turbo",
                      "gpt-3.5-turbo-16k",
                      "gpt-4",
                      "gpt-4-32k",
                      "deepseek-chat",
                      "deepseek-v4-flash",
                      "deepseek-v4-pro"])


@route("/tokenizer/<str:model_name>", methods=["POST"])
@use_args({
    "role": fields.Str(required=True),
    "content": fields.Str(required=True),
    "name": fields.Str(required=False)
}, location="json")
def get_num_tokens(req: Request, message: dict, model_name: str):
    try:
        return {
            "code": 200,
            "num_tokens": num_tokens_from_messages([message], model=model_name)
        }
    except Exception as e:
        logger.error(traceback.format_exc())
        return {
            "code": 500,
            "msg": "{}".format(e)
        }


def num_tokens_from_messages(messages, model="gpt-3.5-turbo"):
    """Returns the number of tokens used by a list of messages."""
    encoding = None
    if model in encoding_cache:
        encoding = encoding_cache.get(model)
    else:
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        encoding_cache[model] = encoding
    if model in support_models:  # note: future models may deviate from this
        num_tokens = 0
        for message in messages:
            num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
            for key, value in message.items():
                num_tokens += len(encoding.encode(value))
                if key == "name":  # if there's a name, the role is omitted
                    num_tokens += -1  # role is always required and always 1 token
        num_tokens += 3  # every reply is primed with <im_start>assistant
        return num_tokens
    else:
        raise NotImplementedError(f"""num_tokens_from_messages() is not presently implemented for model {model}.
See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens.""")


EMBED_DIM = 256

def _hash_bucket(s, dim):
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    return h % dim

# ============ 问句结构化抽取：主题 / 意图 / 上下文依赖 / 语言 ============
# 不依赖 jieba 词性，用句式规则可靠提取，供嵌入加权与 rerank 硬拒使用。

SUBJECT_PATTERNS = [
    r"^什么是(.+?)[？?]?$",
    r"^(.+?)是什么[？?]?$",
    r"^(.+)指的是什么[？?]?$",
    r"^(.+)是什么意思[？?]?$",
    r"^请介绍一下(.+?)[？?]?$",
    r"^实现(?:一个)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^写(?:一个|出)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^生成(?:一个)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^构建(?:一个)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^之前实现过(.+?)吗[？?]?$",
    r"^以前写过(.+?)吗[？?]?$",
    r"^(.+?)怎么(写|实现)[？?]?$",              # "冒泡排序怎么写/怎么实现" → 冒泡排序
    r"^怎么(?:写|实现)(.+?)[。！!?？]?$",          # "怎么写冒泡排序" → 冒泡排序
    r"^(.+?)的(插入|删除|遍历|查询|添加|更新)[。！!?？]?$",  # "红黑树的插入" → 红黑树
    r"^用.+?(?:写|实现|生成|构建)(.+?)[。！!?？]?$",  # "用Go实现红黑树" → 红黑树（语言另由 extract_language 处理）
]


def normalize_subject(subject):
    subject = re.sub(r"^(一个|简单的|简易的|完整的|基础的|版本的)+", "", subject)
    return subject.strip()


def extract_subject(text):
    """抽问句主题：链表是什么/实现一个简易版本的链表 → 链表；抽不到返回 None。"""
    t = re.sub(r"\s+", "", text.strip().lower())
    for p in SUBJECT_PATTERNS:
        m = re.match(p, t)
        if m:
            return normalize_subject(m.group(1).strip())
    return None


INTENT_RULES = [
    ("state_update", [r"记住", r"以后.*回答", r"后面.*会问", r"从现在开始", r"你是一个", r"你是一名", r"扮演", r"我的名字是"]),
    ("history_query", [r"之前.*过.*吗", r"以前.*过.*吗", r"刚才.*过.*吗", r"是否.*过", r"还记得", r"上次", r"前面"]),
    ("implementation", [r"实现", r"写一个", r"写出", r"给.*代码", r"代码示例", r"完整代码", r"生成", r"构建", r"做一个"]),
    ("definition", [r"是什么", r"什么是", r"什么意思", r"介绍一下", r"概念", r"定义"]),
    ("comparison", [r"区别", r"差异", r"对比", r"哪个好"]),
    ("reason", [r"为什么", r"原因", r"为何"]),
    ("operation", [r"删除", r"插入", r"添加", r"遍历", r"查询", r"怎么(增|删|改|查|插)"]),
    ("troubleshooting", [r"报错", r"错误", r"异常", r"失败", r"怎么解决", r"无法"]),
]


def extract_intent(text):
    """规则型意图分类：history_query/implementation/definition/comparison/reason/operation/troubleshooting/unknown。"""
    n = re.sub(r"\s+", "", text.lower())
    for intent, pats in INTENT_RULES:
        for p in pats:
            if re.search(p, n):
                return intent
    return "unknown"


CONTEXT_PATTERNS = [
    r"之前", r"刚才", r"前面", r"上次", r"上一", r"继续", r"还是",
    r"再改", r"刚刚", r"还记得", r"基于前", r"按照前", r"接着",
]


def is_context_dependent(text):
    """依赖当前会话上下文的问题（之前/刚才/继续…）→ 不适合全局语义缓存。"""
    n = re.sub(r"\s+", "", text.lower())
    return any(re.search(p, n) for p in CONTEXT_PATTERNS)


# 状态修改指令：会改变会话状态（身份/偏好/记忆/输出规则）→ 不可缓存、不可命中缓存。
# 这类内容即使 Embedding 再强也会与"无状态问答"混淆（你是一个工程师 vs 你是一个艺术家），
# 必须在准入层直接绕过。
STATEFUL_PATTERNS = [
    # 记忆与偏好
    r"记住", r"请记得", r"不要忘记", r"以后.*回答", r"后面.*会问", r"接下来.*会问",
    # 角色与身份设置
    r"你是一个", r"你是一名", r"你现在是", r"从现在开始", r"扮演", r"作为.*回答", r"假设你是",
    # 用户信息设置
    r"我是一个", r"我是一名", r"我的名字是", r"我的职业是", r"我的偏好是",
    # 输出规则设置
    r"以后都", r"接下来都", r"后续都", r"回答时要", r"回答不要", r"统一使用",
]


def is_stateful_instruction(text):
    n = re.sub(r"\s+", "", text.strip().lower())
    return any(re.search(p, n) for p in STATEFUL_PATTERNS)


def should_bypass_semantic_cache(text):
    """缓存准入：上下文依赖 或 状态修改指令 → 不查不写全局语义缓存。"""
    return is_context_dependent(text) or is_stateful_instruction(text)


LANG_PATTERNS = {
    "go": [r"(?<![a-z])go(?![a-z])", r"golang"],
    "python": [r"python", r"\bpy\b"],
    "java": [r"java"],
    "cpp": [r"c\+\+", r"cpp"],
    "c": [r"c语言"],
    "javascript": [r"javascript", r"\bjs\b"],
}


def extract_language(text):
    n = re.sub(r"\s+", "", text.lower())
    for lang, pats in LANG_PATTERNS.items():
        for p in pats:
            if re.search(p, n):
                return lang
    return None


# 无信息模板词：嵌入权重 0。注意"实现/简单"等是意图/主题词，不在此列（结构化保存）。
STOP_WORDS = {
    "一个", "一下", "请问", "帮我", "可以", "简单地", "请", "能否", "让我", "帮我",
    # 通用疑问/模板词：无内容信息，去掉避免"怎么减肥 vs 怎么学好英语"这类跨主题误命中
    "怎么", "如何", "为什么", "为何", "推荐", "哪些", "多少", "怎样", "为啥",
    "怎么办", "是不是", "有没有", "是否", "什么", "是", "吗", "呢", "吧", "啊",
    "应该", "需要", "想要", "请问一下", "一下",
}


def _add(vec, s, w):
    vec[_hash_bucket(s, EMBED_DIM)] += w


def embed_text(text):
    """加权词面嵌入：SUBJECT:主题(3.0) + INTENT:意图(2.0) + 普通词(1.0，模板词0) + bigram(0.4)。"""
    vec = [0.0] * EMBED_DIM
    sub = extract_subject(text)
    intent = extract_intent(text)
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


def intent_compatible(query_intent, candidate_intent):
    # 意图冲突硬拒：只有"双方都可判定且不同"才拒绝（如 定义 vs 实现），
    # 以及 history_query（依赖会话）一律不放行。
    # unknown 侧不做意图判断——交给向量余弦 + 主题 + 关键词决定，
    # 否则"生成一个红黑树"这类未收录意图的问题连完全相同重问都会被误拒。
    if query_intent == "history_query" or candidate_intent == "history_query":
        return False
    if query_intent != "unknown" and candidate_intent != "unknown":
        return query_intent == candidate_intent
    return True


OPERATION_WORDS = ["插入", "删除", "遍历", "查询", "添加", "更新", "替换", "修改", "查找"]


def extract_operation(text):
    """抽取具体操作（插入/删除/遍历…）：同主题不同操作（红黑树的插入 vs 红黑树的删除）答案不可复用。"""
    t = re.sub(r"\s+", "", text.lower())
    for op in OPERATION_WORDS:
        if op in t:
            return op
    return None


def _subject_conflict(qs, cs):
    """主题冲突：双方都有主题且无"包含/被包含"关系才冲突。
    数组 vs 数组的实现（写法的实现 剥不掉）→ 兼容；数组 vs 红黑树 → 冲突。"""
    if not qs or not cs or qs == cs:
        return False
    if qs in cs or cs in qs:
        return False
    return True


def rerank_score(query, cached_query):
    qs, cs = extract_subject(query), extract_subject(cached_query)
    if _subject_conflict(qs, cs):
        return 0.0, False  # subject_conflict
    qi, ci = extract_intent(query), extract_intent(cached_query)
    if not intent_compatible(qi, ci):
        return 0.0, False  # intent_conflict（含 history/unknown）
    ql, cl = extract_language(query), extract_language(cached_query)
    if ql and cl and ql != cl:
        return 0.0, False  # language_conflict
    # 操作冲突：同为操作句但操作不同（插入 vs 删除）→ 拒
    qop, cop = extract_operation(query), extract_operation(cached_query)
    if qop and cop and qop != cop:
        return 0.0, False  # operation_conflict
    # 普通词 Jaccard（兜底）
    kw1 = set(analyse.extract_tags(query, topK=6))
    kw2 = set(analyse.extract_tags(cached_query, topK=6))
    if not (kw1 | kw2):
        return 1.0, True
    return len(kw1 & kw2) / len(kw1 | kw2), True

@route("/embed", methods=["POST"])
@use_args({"text": fields.Str(required=True)}, location="json")
def get_embedding(req: Request, args: dict):
    try:
        text = args["text"]
        return {
            "code": 200,
            "embedding": embed_text(text),
            "bypass_cache": should_bypass_semantic_cache(text),
            "context_dependent": is_context_dependent(text),
            "intent": extract_intent(text),
            "subject": extract_subject(text),
        }
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}

@route("/rerank", methods=["POST"])
@use_args({"query": fields.Str(required=True), "cached_query": fields.Str(required=True)}, location="json")
def get_rerank(req: Request, args: dict):
    try:
        score, shared = rerank_score(args["query"], args["cached_query"])
        return {"code": 200, "score": score, "shared": shared}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}
