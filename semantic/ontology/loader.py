"""本体加载：concepts.json → 倒排 alias→concept，支持最长词边界匹配。"""
import json, os, re, unicodedata

SCHEMA_VERSION = "v1"
ONTOLOGY_VERSION = "2026-09-03.1"

_HERE = os.path.dirname(os.path.abspath(__file__))

def _norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s).lower()).strip()

def load():
    with open(os.path.join(_HERE, "concepts.json"), encoding="utf-8") as f:
        return json.load(f)

def _latin_word(tok):
    return r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])"

def _build_index():
    data = load()
    idx = []          # (norm_alias, concept_id, is_latin)
    for c in data["concepts"]:
        for a in c["aliases"]:
            idx.append((_norm(a), c["id"], bool(re.search(r"[A-Za-z0-9]", a))))
    return idx

_INDEX = _build_index()

def lookup_subject_id(text):
    """全句匹配：latin 用词边界，中文受控子串；返回最长命中；同长多命中→None。"""
    t = _norm(text)
    if not t:
        return None
    best = None
    best_len = 0
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
