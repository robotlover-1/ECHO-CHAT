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

# =========================================================================
# lang_terms：语言→实体表（Task 2）。独立于上方的 concept 正排索引。
#
# schema(每条 term 记录)：
#   status ∈ {mapped, ambiguous, unmapped}
#   mapped  必含 entity_id/kind；可选 namespace/family/type_args("keep")
#   ambiguous/unmapped 视同不可映射（lookup 返回 []/跳过）
# =========================================================================

LANG_TERMS_VERSION = "2026-09-04.1"
_LANG_TERMS_FILE = os.path.join(_HERE, "lang_terms.json")
_LANG_TERMS_STATUS_ENUM = {"mapped", "ambiguous", "unmapped"}
_TYPE_ARGS_MAX = 64          # 单层 <...> 体长上限（超限/含内层 < 不算）
_LANG_TERMS_CACHE = {}       # 缓存校验后的 languages: dict[lang, dict[term, entity]]


def load_lang_terms():
    """读 lang_terms.json；启动校验并缓存。返回 languages: dict[lang, dict[term, entity]]。
       校验非法值直接抛异常（status 越枚举、mapped 缺 entity_id/kind 等）。幂等。"""
    if _LANG_TERMS_CACHE:
        return _LANG_TERMS_CACHE["languages"]
    with open(_LANG_TERMS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("lang_terms_version") == LANG_TERMS_VERSION, \
        f"lang_terms version mismatch: {data.get('lang_terms_version')!r}"
    langs = data.get("languages")
    assert isinstance(langs, dict) and langs, "lang_terms: languages 缺失或为空"
    for lang, terms in langs.items():
        for term, rec in terms.items():
            assert isinstance(rec, dict) and "status" in rec, \
                f"lang_terms[{lang}][{term!r}]: 缺 status"
            status = rec["status"]
            assert status in _LANG_TERMS_STATUS_ENUM, \
                f"lang_terms[{lang}][{term!r}]: 非法 status {status!r} (∈{sorted(_LANG_TERMS_STATUS_ENUM)})"
            if status == "mapped":
                assert rec.get("entity_id"), f"lang_terms[{lang}][{term!r}]: mapped 缺 entity_id"
                assert rec.get("kind"), f"lang_terms[{lang}][{term!r}]: mapped 缺 kind"
    _LANG_TERMS_CACHE["languages"] = langs
    return langs


def _term_pattern(word):
    """词界模式：拉丁标识符用前后负 lookaround(字母/数字)→含下划线防 unordered_map 内误命中 map；
       带空白/连字符/非 ascii(CJK) 的术语退化为紧凑子串匹配(_fold 后原文含别名场景走 contains)。"""
    if re.fullmatch(r"[A-Za-z0-9_]+", word, re.ASCII) and re.search(r"[A-Za-z0-9]", word):
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])")
    if word.isascii() and re.search(r"\s|\-", word):
        # 多词/连字符拉丁短语：两侧按非 ascii-alnum 隔开
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])")
    # 含非 ascii（如 CJK）术语：受控子串，仅长于 1 字符安全
    return word if len(word) > 1 else None


def _detect_namespace(text, span_start):
    """若词前紧跟白名单前缀(如 std::)，该前缀并入 token，返回 (namespace, 词前含前缀的有效起点)。"""
    for p in _NS_WHITELIST:
        if span_start >= len(p) and text[span_start - len(p):span_start] == p:
            ns = p[:-2] if p.endswith("::") else p
            return ns, span_start - len(p)
    return None, span_start


def _parse_type_args(text, pos):
    """紧跟紧跟 token 的单层 <...>：体长≤64 且内无 < → 逗号切分 list；否则 []。"""
    rest = text[pos:]
    if not rest.startswith("<"):
        return []
    close = rest.find(">")
    if close == -1:
        return []
    body = rest[1:close]
    if len(body) > _TYPE_ARGS_MAX or "<" in body:
        return []
    return [t.strip() for t in body.split(",") if t.strip()]


# 导入即一次性校验+缓存（早失败），保持与 concept 一致的启动即校验风格
_LOADED_LANG_TERMS = load_lang_terms()


class TermMatch:
    """一次语言实体词命中的结构化结果。
       source ∈ {concept, lang_terms}；multi 保留为后续(多主题/问句)相位扩展标志。"""
    __slots__ = ("entity_id", "kind", "family", "namespace",
                 "type_args", "surface", "source", "multi")

    def __init__(self, entity_id, kind, family=None, namespace=None,
                 type_args=None, surface=None, source="lang_terms", multi=False):
        self.entity_id = entity_id
        self.kind = kind
        self.family = family
        self.namespace = namespace
        self.type_args = list(type_args or [])
        self.surface = surface or entity_id
        self.source = source
        self.multi = multi

    def __repr__(self):
        return (f"TermMatch({self.source}:{self.entity_id}"
                f"{{kind={self.kind},family={self.family},"
                f"ns={self.namespace},type_args={self.type_args},surface={self.surface!r}}})")


def lookup_lang_entity(lang, text):
    """对 languages[lang] 的每词在 text 上做词界命中（拉丁标识符|短语|CJK 子串）；
       命中并映射者解析整 token surface、紧跟 std:: 的 namespace、紧跟单层 <...> type_args。
       返回全部命中 TermMatch（一<词→实体>一结果，供多主题判定）；ambiguous/unmapped 直接跳过。"""
    text = text or ""
    langs = load_lang_terms()
    terms = langs.get(lang)
    if not terms:
        return []
    results = []
    for term, rec in terms.items():
        if rec["status"] != "mapped":
            continue  # ambiguous/unmapped → 该词不映射
        pat = _term_pattern(term)
        if pat is None:
            continue
        m = re.search(pat, text)
        if not m:
            continue
        s = m.start()
        wlen = m.end() - m.start()      # 词本体宽度（不含 std:: 前缀与 <...>）
        namespace, eff_start = _detect_namespace(text, s)
        surface = text[eff_start:s + wlen]
        # 解析紧跟的单层 <...>(cpp 模板实参)；非 "std::vector<int>" 也仅当 type_args=="keep" 启
        type_args = _parse_type_args(text, s + wlen) if rec.get("type_args") == "keep" else []
        # namespace 兜底：显式前缀缺席时用实体表记录的规范 ns（如 cpp→std）
        if namespace is None:
            namespace = rec.get("namespace")
        results.append(TermMatch(
            entity_id=rec["entity_id"], kind=rec["kind"],
            family=rec.get("family"), namespace=namespace,
            type_args=type_args, surface=surface,
            source="lang_terms", multi=False))
    return results


# import 时立即校验（早失败）
from ontology import validator  # noqa: F401  导入即校验（此处 load 已定义，避免循环 import）
validate = validator.validate
validate()
