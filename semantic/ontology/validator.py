"""本体校验：导入即检查；违规直接抛异常。
唯一性现基于 原始别名 + folding 生成变体(std:: 剥离/glued) 全集；跨概念碰撞抛 AssertionError，即启动失败=唯一行为。"""
from ontology.loader import load, folding_variants

def _generated_keys(data):
    """对每条 alias 应用 folding_variants 展开后的 ∪ 全集（规范化 key 的集合，返回全局去重动态全集）。"""
    keys = set()
    for c in data["concepts"]:
        for a in c["aliases"]:
            keys.update(folding_variants(a))
    return keys

GROUP_ALLOW = frozenset({"electrical", "mechanical", "embedded"})
_PREFIX_GROUP = {"elec_": "electrical", "mech_": "mechanical", "emb_": "embedded"}


def validate_groups(concepts):
    """group 标注校验：值须在白名单；带领域前缀 id 须带且匹配对应 group。
    旧 CS 概念（无前缀无 group）不受影响。违规抛 AssertionError（启动即失败）。"""
    for c in concepts:
        cid = c["id"]
        g = c.get("group")
        if g is not None and g not in GROUP_ALLOW:
            raise AssertionError(f"concept {cid}: illegal group {g!r} (∈{sorted(GROUP_ALLOW)})")
        for prefix, expect in _PREFIX_GROUP.items():
            if cid.startswith(prefix) and g != expect:
                raise AssertionError(
                    f"concept {cid}: prefix {prefix!r} requires group={expect!r}, got {g!r}")


def validate():
    data = load()
    ids, owner = set(), {}   # owner: normalized/generated key -> concept id（含原始折叠 + 变体）
    for c in data["concepts"]:
        cid = c["id"]
        assert cid not in ids, f"dup concept id {cid}"
        ids.add(cid)
        assert c.get("canonical_zh") and c.get("canonical_en"), cid
        assert c.get("aliases"), cid
        for a in c["aliases"]:
            assert a and len(a) >= 1
            # 禁用危险单字符英文 alias（可含 'c'/'go' 级词需显式建档，无 alias 冲突即放行）
            assert not (len(a) == 1 and a.isascii() and a.isalpha()), f"single-char latin alias {a!r}"
            for key in folding_variants(a):
                assert key, f"empty generated key for alias {a!r}"
                prev = owner.get(key)
                # 同概念内的变体自碰撞（如 glue 出显式别名）不视为冲突；跨概念才 raise
                assert prev is None or prev == cid, \
                    f"generated key {key!r} (from alias {a!r}) collides across {prev} and {cid}"
                owner[key] = cid
    assert len(ids) >= 40, "ontology needs >=40 concepts"
    validate_groups(data["concepts"])
