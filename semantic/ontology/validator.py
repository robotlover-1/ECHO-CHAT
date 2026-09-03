"""本体校验：导入即检查；违规直接抛异常。"""
import re
from ontology.loader import load

def _norm(a):
    import unicodedata
    s = unicodedata.normalize("NFKC", a).lower()
    return re.sub(r"\s+", " ", s).strip()

def validate():
    data = load()
    ids, alias_owner = set(), {}
    for c in data["concepts"]:
        assert c["id"] not in ids, f"dup concept id {c['id']}"
        ids.add(c["id"])
        assert c.get("canonical_zh") and c.get("canonical_en"), c["id"]
        assert c.get("aliases"), c["id"]
        for a in c["aliases"]:
            assert a and len(a) >= 1
            # 禁止危险单字符英文 alias（可含 'c'/'go' 级别词需显式建档，无 alias 冲突即放行）
            assert not (len(a) == 1 and re.fullmatch(r"[A-Za-z]", a)), f"single-char latin alias {a!r}"
            key = _norm(a)
            assert key not in alias_owner, f"alias {a!r} used by {alias_owner[key]} and {c['id']}"
            alias_owner[key] = c["id"]
    assert len(ids) >= 40, "ontology needs >=40 concepts"
