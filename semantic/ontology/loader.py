"""本体加载：concepts.json → 倒排 alias→concept，支持最长词边界匹配。"""
import json, os, re, unicodedata

SCHEMA_VERSION = "v1"
ONTOLOGY_VERSION = "2026-09-03.1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_NS_WHITELIST = ("std::",)   # 白名单 namespace：解析待匹配文本/别名时剥离此前缀
_GEN_MIN_LEN = 3             # 仅当 glued 长度 >= 此值时纳入生成变体

def _fold(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s).lower()).strip()

def _strip_whitelisted_ns(text):
    for p in _NS_WHITELIST:
        if text.startswith(p):
            return text[len(p):]
    return text

def folding_variants(alias):
    """把单条别名展开为查找/校验用 key 全集：
       原始折叠 + 白名单 ns 剥离 base + 剥离后的 glued(去空格/连字符/下划线)。
       显式复数别名条目同样走此展开。"""
    a = _fold(alias)
    out = {a}
    base = _strip_whitelisted_ns(a)
    if base != a:
        out.add(base)
    glued = re.sub(r"[\s\-_]", "", base)
    if len(glued) >= _GEN_MIN_LEN and glued != base:
        out.add(glued)
    return out

def load():
    with open(os.path.join(_HERE, "concepts.json"), encoding="utf-8") as f:
        return json.load(f)

ENTITY_COUNT = len(load()["concepts"])

def _latin_word(tok):
    return r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])"

def _build_index():
    data = load()
    idx = []          # (varied_key, concept_id, is_latin)；同一概念的多变体并列同长取长者
    for c in data["concepts"]:
        for a in c["aliases"]:
            is_latin = bool(re.search(r"[A-Za-z0-9]", a))
            for key in folding_variants(a):
                if key:
                    idx.append((key, c["id"], is_latin))
    return idx

_INDEX = _build_index()

def _query_variants(text):
    """匹配用的文本变体：原始折叠 + 白名单 ns 剥离形式。"""
    t = _fold(text)
    base = _strip_whitelisted_ns(t)
    yield t
    if base != t and base:
        yield base

def lookup_subject_id(text):
    """全句匹配：latin 用词边界，中文受控子串；返回最长命中；同长多命中→None。
       索引含折叠/glued/ns 剥离变体；查询侧仅做同级 ns 剥离展开。"""
    query_texts = list(_query_variants(_fold(text) if text else ""))
    if not text or not query_texts[0]:
        return None
    best = None
    best_len = 0
    for t in query_texts:
        for alias, cid, is_latin in _INDEX:
            if not alias:
                continue
            if is_latin:
                hit = re.search(_latin_word(alias), t) is not None
            else:
                hit = alias in t
            if hit and len(alias) > best_len:
                best, best_len = cid, len(alias)
            elif hit and len(alias) == best_len and best != cid:
                best = None  # 同长歧义：保守 None
    return best

# import 时立即校验（早失败）
from ontology import validator  # noqa: F401  导入即校验（此处 load 已定义，避免循环 import）
validate = validator.validate
validate()
