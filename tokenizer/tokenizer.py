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

# ---------------- 问句主题抽取（不依赖 jieba 词性） ----------------
# "X是什么/什么是X"等定义句：直接把主语提出来，作为主题判别的可靠依据。
SUBJECT_PATTERNS = [
    r"^什么是(.+?)[？?]?$",
    r"^(.+?)是什么[？?]?$",
    r"^(.+)指的是什么[？?]?$",
    r"^(.+)是什么意思[？?]?$",
    r"^请介绍一下(.+?)[？?]?$",
]
SUBJECT_PREFIXES = ["请问", "简单说一下", "简单介绍一下"]


def extract_subject(text):
    """抽取定义类问句的主题，如 红黑树是什么/什么是红黑树 → 红黑树；非定义句返回 None。"""
    t = re.sub(r"\s+", "", text.strip().lower())
    for p in SUBJECT_PATTERNS:
        m = re.match(p, t)
        if m:
            s = m.group(1).strip()
            for pre in SUBJECT_PREFIXES:
                if s.startswith(pre):
                    s = s[len(pre):]
            return s
    return None


# 模板/功能词：在嵌入中权重为 0，避免"是什么/怎么/实现"制造虚假相似度
STOP_WORDS = {"什么", "是", "是什么", "怎么", "如何", "一个", "一下", "介绍", "简单", "实现"}


def token_weight(token):
    return 0.0 if token in STOP_WORDS else 1.0


def _add(vec, s, w):
    vec[_hash_bucket(s, EMBED_DIM)] += w


def embed_text(text):
    """加权词面嵌入：主题词 3.0+标记桶、普通词 1.0（模板词 0）、字符 bigram 0.4。"""
    vec = [0.0] * EMBED_DIM
    sub = extract_subject(text)
    if sub:
        _add(vec, sub, 3.0)
        _add(vec, "subj:" + sub, 2.0)  # 主题标记桶：同主题聚拢、跨主题拉开
    for w in jieba.cut(text):
        w = w.strip()
        if not w:
            continue
        wt = token_weight(w)
        if wt > 0:
            _add(vec, w, wt)
    chars = [c for c in text if not c.isspace()]
    for i in range(len(chars) - 1):
        _add(vec, chars[i] + chars[i + 1], 0.4)
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


def rerank_score(query, cached_query):
    # 主题冲突硬拒：定义类问句主语不同（红黑树 vs 数组）→ 直接拒绝
    qs = extract_subject(query)
    cs = extract_subject(cached_query)
    if qs and cs and qs != cs:
        return 0.0, False
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
        return {"code": 200, "embedding": embed_text(args["text"])}
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
