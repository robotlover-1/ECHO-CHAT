from nuxt import route, logger, Request
from nuxt.repositorys.validation import fields, use_args
import traceback
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

def embed_text(text):
    vec = [0.0] * EMBED_DIM
    for w in jieba.cut(text):
        w = w.strip()
        if w:
            vec[_hash_bucket(w, EMBED_DIM)] += 1.0
    chars = [ch for ch in text if not ch.isspace()]
    for i in range(len(chars) - 1):
        vec[_hash_bucket(chars[i] + chars[i+1], EMBED_DIM)] += 1.0
    norm = math.sqrt(sum(v*v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec

def rerank_score(query, cached_query):
    kw1 = set(analyse.extract_tags(query, topK=6))
    kw2 = set(analyse.extract_tags(cached_query, topK=6))
    m1 = set(analyse.extract_tags(query, topK=6, allowPOS=('n', 'vn', 'v', 'eng')))
    m2 = set(analyse.extract_tags(cached_query, topK=6, allowPOS=('n', 'vn', 'v', 'eng')))
    # 双方都没有可用的名词/动词关键词时也视为共享：
    # 否则对"红黑树是什么"这类短问题，m1/m2 都为空 → shared=False → 相同问题缓存也永不命中
    shared = bool(m1 & m2) or (not m1 and not m2)
    if not (kw1 | kw2):
        return 1.0, True
    return len(kw1 & kw2) / len(kw1 | kw2), shared

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
