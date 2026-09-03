# ECHO-CHAT 语义检索 Phase 1 实施计划（规则与别名体系）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"红黑树≈rbtree"跨措辞命中、C≠C++/定义≠实现/插入≠删除/带约束≠无约束 硬拒，通过安全语义指纹 + 结构化 decision + canonical 嵌入提升命中率与区分度（不引入真模型/向量库）。

**Architecture:** semantic 服务拆纯函数模块：`ontology/`(数据+校验) → `parse.py`(槽位+residual+指纹) → `decision.py`(保守硬拒) → `embedding.py`(canonical 桶)；`semantic.py` 薄路由；Go `semcache` 加"指纹候选→decision 复核"前置与 Top-K=30。指纹**只存候选问题、不存答案、命中必复核**。

**Tech Stack:** Python 3.8（nuxt 0.2.15 / jieba 0.42.1）/ pytest / Go（config+semcache）/ kvstore redis 协议（SET/HSET/VSEARCH）。

## Global Constraints

- 仓库根 = 本计划 repo；命令默认在仓库根执行；禁止 sudo/root；勿用 `pkill -f`（杀服务用 `runtime/pids/*.pid` + `./start.sh` 补起）。
- 行为红线：**`semfp:*` 只存候选问题文本，绝不存答案；任何命中前必过 `decide()`**。fp 需 `fingerprint_eligible`。
- 纯逻辑模块 `ontology/parse/embedding/decision` **不 import nuxt**；`semantic.py` 才是路由层。
- /embed、/rerank 响应**只增字段不删**（旧字段 code/embedding/bypass_cache/context_dependent/intent/subject 与 code/score/shared 保留）。
- 依赖不新增（无 pyyaml/torch 等；本体用 JSON+stdlib）。
- reason 枚举固定：`subject_conflict / language_conflict / intent_conflict / operation_conflict / output_conflict / constraint_conflict / unknown_subject / unknown_intent / ok`。
- 日志/记录不写用户原文与答案正文、不写凭据。
- 提交只 stage 本任务文件；仓库既有脏文件（kvstore submodule / openai-api-proxy dev.config / backend www）不碰。
- 目标产物可离线 pytest（不依赖服务在跑）；服务级/Go 改动各任务末尾做最小运行验证。

---

### Task 1: 本体数据 + loader/validator

**Files:**
- Create: `semantic/ontology/__init__.py`
- Create: `semantic/ontology/concepts.json`
- Create: `semantic/ontology/loader.py`
- Create: `semantic/ontology/validator.py`
- Test: `semantic/tests/test_ontology.py`

**Interfaces:**
- Produces: `ontology.__init__` 导出 `load()`, `lookup_subject_id(text)->str|None`, `ONTOLOGY_VERSION`, `SCHEMA_VERSION`（=“v1”），`parser_version` 常量（parse 侧）。`loader.lookup_subject_id(text)`：对 **latin token** 用词边界、对**中文**用受控子串，返回**最长**命中 concept id；同长多命中 → None。`validator.validate()` 导入时执行，失败 raise。

- [ ] **Step 1: 写失败测试** `semantic/tests/test_ontology.py`

```python
import pytest
from ontology import load, lookup_subject_id, ONTOLOGY_VERSION

def test_loader_loads_version():
    d = load()
    assert d["concepts"] and ONTOLOGY_VERSION == "2026-09-03.1"

@pytest.mark.parametrize("text,expect", [
    ("红黑树", "red_black_tree"),
    ("rbtree", "red_black_tree"),
    ("red-black tree", "red_black_tree"),
    ("RBAC", None),                       # 词边界，不命中 rb
    ("二叉搜索树", "binary_search_tree"),
    ("bst", "binary_search_tree"),
    ("tree", "tree"),                     # 基础概念独立建档
    ("binary tree", "binary_tree"),
    ("RB tree", "red_black_tree"),
])
def test_lookup(text, expect):
    assert lookup_subject_id(text) == expect

def test_alias_global_unique():
    seen = {}
    for c in load()["concepts"]:
        for a in c["aliases"]:
            key = a.strip().lower()
            assert key not in seen or seen[key] == c["id"], f"alias dup: {key}"
            seen[key] = c["id"]
    assert len(seen) >= 40
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py -q`
Expected: FAIL（import 错误 / 断言失败）。

- [ ] **Step 3: 写 `ontology/validator.py`**

```python
"""本体校验：导入即检查；违规直接抛异常。"""
import re
from ontology import load

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
```

- [ ] **Step 4: 写 `ontology/loader.py`**

```python
"""本体加载：concepts.json → 倒排 alias→concept，支持最长词边界匹配。"""
import json, os, re, unicodedata
from ontology import validator  # noqa: F401  导入即校验

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
validate = validator.validate
validate()
```

- [ ] **Step 5: 建 `concepts.json`（≥40 条）**

先写下面 16 条（结构参照）：

```json
{
  "schema": "v1",
  "ontology_version": "2026-09-03.1",
  "concepts": [
    {"id": "red_black_tree", "canonical_zh": "红黑树", "canonical_en": "red black tree",
     "aliases": ["红黑树", "rbtree", "rb tree", "rb-tree", "red black tree", "red-black tree", "redblacktree"]},
    {"id": "binary_search_tree", "canonical_zh": "二叉搜索树", "canonical_en": "binary search tree",
     "aliases": ["二叉搜索树", "二叉查找树", "bst", "binary search tree"]},
    {"id": "binary_tree", "canonical_zh": "二叉树", "canonical_en": "binary tree",
     "aliases": ["二叉树", "binary tree", "binarytree"]},
    {"id": "tree", "canonical_zh": "树", "canonical_en": "tree",
     "aliases": ["树", "tree"]},
    {"id": "linked_list", "canonical_zh": "链表", "canonical_en": "linked list",
     "aliases": ["链表", "linked list", "linkedlist"]},
    {"id": "array", "canonical_zh": "数组", "canonical_en": "array",
     "aliases": ["数组", "array"]},
    {"id": "stack", "canonical_zh": "栈", "canonical_en": "stack",
     "aliases": ["栈", "stack", "堆栈"]},
    {"id": "queue", "canonical_zh": "队列", "canonical_en": "queue",
     "aliases": ["队列", "queue"]},
    {"id": "hash_table", "canonical_zh": "哈希表", "canonical_en": "hash table",
     "aliases": ["哈希表", "散列表", "hash table", "hashtable", "hash map", "hashmap"]},
    {"id": "heap", "canonical_zh": "堆", "canonical_en": "heap",
     "aliases": ["堆", "heap", "优先队列", "priority queue"]},
    {"id": "singleton", "canonical_zh": "单例", "canonical_en": "singleton",
     "aliases": ["单例", "单例模式", "singleton", "singleton pattern"]},
    {"id": "thread_pool", "canonical_zh": "线程池", "canonical_en": "thread pool",
     "aliases": ["线程池", "thread pool", "threadpool"]},
    {"id": "deep_copy", "canonical_zh": "深拷贝", "canonical_en": "deep copy",
     "aliases": ["深拷贝", "深复制", "deep copy", "deepcopy"]},
    {"id": "bubble_sort", "canonical_zh": "冒泡排序", "canonical_en": "bubble sort",
     "aliases": ["冒泡排序", "冒泡", "bubble sort", "bubblesort"]},
    {"id": "quick_sort", "canonical_zh": "快速排序", "canonical_en": "quick sort",
     "aliases": ["快速排序", "快排", "quick sort", "quicksort"]},
    {"id": "merge_sort", "canonical_zh": "归并排序", "canonical_en": "merge sort",
     "aliases": ["归并排序", "merge sort", "mergesort"]}
  ]
}
```

再按下列 24 个 id **追加**（每条自定 canonical_zh/en，至少 1 中文+1 英文 alias；确保 alias 全局唯一、不做单字符英文 alias；若一条 alias 与上述冲突则换措辞）：`insertion_sort`（插入排序）、`selection_sort`（选择排序）、`heap_sort`（堆排序/堆排）、`counting_sort`、`bucket_sort`、`radix_sort`（基数排序）、`recursion`（递归）、`bit_operation`（位运算/位操作）、`graph`（图）、`avl_tree`（avl 树/平衡二叉树）、`b_tree`（b 树/b-tree）、`trie`（字典树/前缀树/trie）、`union_find`（并查集/union-find）、`topological_sort`（拓扑排序）、`shortest_path`（最短路径）、`string_match`（字符串匹配/kmp）、`regular_expression`（正则表达式/regex）、`json_parse`（json 解析）、`memory_cache`（内存缓存/cache）、`mutex`（互斥锁/锁）、`web_crawler`（爬虫/网络爬虫）、`orm`、`grpc`、`restful`。

- [ ] **Step 6: `ontology/__init__.py`**

```python
from ontology.loader import load, lookup_subject_id, SCHEMA_VERSION, ONTOLOGY_VERSION
from ontology import validator
validate = validator.validate

__all__ = ["load", "lookup_subject_id", "SCHEMA_VERSION", "ONTOLOGY_VERSION", "validate", "validator"]
```

- [ ] **Step 7: 跑测试确认通过**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py -q`
Expected: PASS（≥40 概念、alias 唯一、词边界/longest 断言全过）。

- [ ] **Step 8: 提交**

```bash
git add semantic/ontology semantic/tests/test_ontology.py
git commit -m "feat(semantic): 本体 concepts.json(≥40)+loader 最长词边界匹配+validator 唯一性校验"
```

---

### Task 2: parse.py 槽位抽取（句式补全 + 双路 subject + 中英意图/语言/操作）

**Files:**
- Create: `semantic/parse.py`
- Test: `semantic/tests/test_parse.py`

**Interfaces:**
- Consumes: Task 1 `ontology.lookup_subject_id(text)`, `ONTOLOGY_VERSION`。
- Produces: `ParsedQuery` 数据类 + `parse(text) -> ParsedQuery`。字段：`raw_text, normalized_text, subject_text, subject_id, intent, language, operation, output_type, residual_words(frozenset), context_dependent, stateful, bypass_cache, fingerprint(str|None), fingerprint_eligible(bool), parser_version="v1", ontology_version`。另提供 `normalize(text)`。

- [ ] **Step 1: 写失败测试** `semantic/tests/test_parse.py`

```python
import pytest
from parse import parse

CASES = [
    # (text, subject_id, intent, language)
    ("生成一个红黑树", "red_black_tree", "implementation", None),
    ("什么是红黑树", "red_black_tree", "definition", None),
    ("what is a red-black tree", "red_black_tree", "definition", None),
    ("implement a red-black tree", "red_black_tree", "implementation", None),
    ("使用 C 编写 rbtree", "red_black_tree", "implementation", "c"),
    ("用 C 语言生成红黑树", "red_black_tree", "implementation", "c"),
    ("用 Python 实现红黑树插入", "red_black_tree", "implementation", "python"),
    ("红黑树是什么", "red_black_tree", "definition", None),
    ("RBAC 权限模型是什么", None, "definition", None),   # RBAC 不命中 rbtree
    ("JavaScript 实现深拷贝", "deep_copy", "implementation", "javascript"),
    ("用Java写线程池", "thread_pool", "implementation", "java"),
    ("用 C++ 写红黑树", "red_black_tree", "implementation", "cpp"),
]
@pytest.mark.parametrize("text,sid,intent,lang", CASES)
def test_parse_slots(text, sid, intent, lang):
    q = parse(text)
    assert q.subject_id == sid, text
    assert q.intent == intent, text
    assert q.language == lang, text

def test_parse_operation_maps_to_id():
    assert parse("用 Python 实现红黑树插入").operation == "insert"
    assert parse("rbtree 删除节点").operation == "delete"

def test_parse_bypass():
    assert parse("继续修改上面的红黑树").bypass_cache is True
    assert parse("记住我叫小明").bypass_cache is True
    assert parse("生成一个红黑树").bypass_cache is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_parse.py -q`
Expected: FAIL。

- [ ] **Step 3: 从 `semantic.py` 逐字搬入基础 helper**

把当前 `semantic/semantic.py` 中这些定义**原样搬**到 `parse.py`（含注释）：`SUBJECT_PATTERNS`、`normalize_subject`、`INTENT_RULES`、`extract_intent`、`CONTEXT_PATTERNS`、`is_context_dependent`、`STATEFUL_PATTERNS`、`is_stateful_instruction`、`should_bypass_semantic_cache`、`LANG_PATTERNS`、`extract_language`、`OPERATION_WORDS`、`extract_operation`。搬完后先 `python3 -m py_compile parse.py`。

- [ ] **Step 4: 扩展句式与中文/英文识别（parse.py 内新增）**

把 `SUBJECT_PATTERNS` 追加以下模式（**保持原有全部模式不变**，仅扩展；匹配时先跑原单条模式，再跑新模式）：

```python
_EN = {
    "definition": [r"^what is(?: an?| the)? (.+?)[?]?$", r"^define (.+?)[.]?$",
                   r"^explain(?: the concept of)? (.+?)[?]?$", r"^what does (.+?) (?:mean|do)[?]?$"],
    "implementation": [r"^(?:implement|write|build|create|code) (?:an? |the )?(.+?)[.]?$",
                       r"^implement (.+?) in (.+?)$",            # group2=语言
                       r"^write (.+?) using (.+?)$",
                       r"^(.+?) (?:implementation|implementation in) (.+?)$"],
}
```

- 双路主题：`subject_text = first_match(原 SUBJECT_PATTERNS + 新增中英句式) or None`；`subject_id = ontology.lookup_subject_id(subject_text) or ontology.lookup_subject_id(full_normalized_text)`（全句兜底）。实现为 `_extract_subject_pair(text) -> (subject_text, subject_id)`。
- `extract_intent` 扩展：新 `INTENT_RULES` 追加中文 `使用/编写/给我` → implementation 词集，以及英文规则集（definition 的 `what is/define/explain`、implementation 的 `implement/write/build/create`、comparison 的 `difference/compare/versus/\bvs\b`、troubleshooting 的 `error/exception/failed/cannot/unable to`）。注意**保持现有规则顺序与命中语义**；英文定义句不能同时撞 implementation（先 definition 后 implementation 组即可，靠顺序：把 definition 组排在 implementation 之前）。
- `extract_language` 改为**长模式优先**：依次匹配 `c++ / cxx / cpp` → `c# / csharp` → `.net` → `javascript` → `node.js/nodejs` → `typescript` → `java` → `python` → `rust` → `golang` → `go`(词边界) → `c`。`c` 仅当命中 `c语言` 或紧跟实现语境的 `(?<![a-z0-9+#])c(?![a-z0-9+#])(?=.*(实现|编写|生成|写|编程|代码|implementation))` 才判（去空白后匹配，覆盖"使用 C 编写 rbtree"）。返回第一个命中；若命中 ≥2 个不同语言视为歧义返回 None。实现按序扫描、命中即返回，天然保证 `c++` 不会先被 `c` 吞。
- `OPERATION_WORDS` 拆为**中文→英文 id 映射** `OP_ZH2ID = {"插入":"insert","添加":"insert","插入节点":"insert","删除":"delete","移除":"delete","遍历":"traverse","查询":"find","查找":"find","搜索":"find","更新":"update","修改":"update","替换":"replace"}`，并加英文 `insert/insertion/delete/removal/remove/traverse/traversal/find/search/lookup/update/modify`。`extract_operation(text)` 返回匹配到的英文 id；`remove` 需 `subject_id in {"array","linked_list","vector"}` 之类容器语境才归 delete，否则忽略（保守）。

- [ ] **Step 5: 组装 `ParsedQuery` 与 `parse()`（先不接 residual/fp，下任务补）**

```python
from dataclasses import dataclass, field
import re, unicodedata
import jieba
from ontology import lookup_subject_id, ONTOLOGY_VERSION

PARSER_VERSION = "v1"
STOP_WORDS = {...}   # 原样搬 semantic.py 的 STOP_WORDS

def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)
    s = s.lower()
    # 保留 c++/c#/.net 等符号：先保护占位再转半角
    for sym in ["c++", "c#", ".net", "node.js"]:
        s = s.replace(sym, sym.replace("+", "P").replace("#", "H").replace(".", "D"))
    # 全角→半角由 NFKC 已做；这里仅统一空白与常见全角标点
    s = re.sub(r"[\s　]+", " ", s)
    for sym in ["c++", "c#", ".net", "node.js"]:
        s = s.replace(sym.replace("+", "P").replace("#", "H").replace(".", "D"), sym)
    return s.strip()

@dataclass(frozen=True)
class ParsedQuery:
    raw_text: str
    normalized_text: str
    subject_text: str | None
    subject_id: str | None
    intent: str
    language: str | None
    operation: str | None
    output_type: str | None
    residual_words: frozenset = frozenset()
    context_dependent: bool = False
    stateful: bool = False
    bypass_cache: bool = False
    fingerprint: str | None = None
    fingerprint_eligible: bool = False
    parser_version: str = PARSER_VERSION
    ontology_version: str = ONTOLOGY_VERSION
```

`parse(text)`：normalize → 双路 subject → extract_intent/extract_language/extract_operation（在 normalize 文本上）→ output_type 派生 → context/stateful/bypass 由原 helper 算（对原文本）→ residual/fingerprint 本任务先置空（Task 3 补）→ 返回 ParsedQuery。**output_type 派生**：`intent in {"implementation","troubleshooting"} → "code"；intent in {"definition","comparison","reason"} → "explanation"；否则 None`。

- [ ] **Step 6: 跑测试通过**

Run: `cd semantic && python3 -m pytest tests/test_parse.py -q`
Expected: PASS。逐个 CASE 核对（语言/意图/操作映射）。

- [ ] **Step 7: 提交**

```bash
git add semantic/parse.py semantic/tests/test_parse.py
git commit -m "feat(semantic): parse.py 槽位抽取——双路subject、中英意图/语言长模式优先、操作归一中英 id"
```

---

### Task 3: parse residual + fingerprint_eligible + build_fingerprint

**Files:**
- Modify: `semantic/parse.py`
- Test: `semantic/tests/test_fingerprint.py`

**Interfaces:**
- Consumes: Task 2 `ParsedQuery/parse/normalize`。
- Produces: `parse()` 现填 `residual_words` / `fingerprint` / `fingerprint_eligible`。`_residual_words(qp) -> frozenset`、`_fingerprint_eligible(qp) -> bool`、`build_fingerprint(qp) -> str|None`。fingerprint 格式：`sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode("utf-8")).hexdigest()`；payload 字段见下。

- [ ] **Step 1: 写失败测试** `semantic/tests/test_fingerprint.py`

```python
from parse import parse, build_fingerprint

def fp(s): return parse(s).fingerprint

def test_alias_and_wording_same_fp():
    assert fp("用 C 语言生成红黑树") == fp("使用 C 编写 rbtree")
    assert fp("生成一个红黑树") == fp("生成一个 rbtree")
    assert fp("什么是红黑树") == fp("what is a red-black tree")

def test_fp_differs_on_language_operation():
    assert fp("用 C 语言生成红黑树") != fp("用 C++ 生成红黑树")
    assert fp("用 Python 实现红黑树插入") != fp("用 Python 实现 rbtree 删除")

def test_constraint_queries_not_eligible():
    for t in ["实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树",
              "实现支持重复键的 C++ 红黑树", "实现无递归删除的红黑树",
              "实现带父指针的二叉树"]:
        q = parse(t)
        assert q.fingerprint_eligible is False, t
        assert q.fingerprint is None, t

def test_residual_captures_constraint():
    assert "线程安全" in parse("实现线程安全的 C++ 红黑树").residual_words
    assert parse("生成一个红黑树").residual_words == frozenset()

def test_fp_deterministic_and_versioned():
    a = build_fingerprint(parse("实现红黑树"))
    b = build_fingerprint(parse("实现红黑树"))
    assert a == b and a
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_fingerprint.py -q`
Expected: FAIL。

- [ ] **Step 3: 实现 residual / eligible / fingerprint**

在 `parse.py` 增加（纯 stdlib）：

```python
import hashlib, json

# 模板/句式词白名单：这些词不构成"未解析约束"（算 content 但不阻塞 eligible）
_RESIDUAL_WHITELIST = {"的", "个", "一个", "一下", "请", "帮我", "怎么", "如何", "什么",
                       "是", "吗", "呢", "实现", "生成", "编写", "写", "使用", "用",
                       "语言", "代码", "树", "节点", "中", "里", "进行", "实现"}

# residual = 内容 token 中未被以下任一消费的：
#   停用词 / 白名单 / 命中语言的别名词 / 中文操作词 / subject alias 组成词
LANG_TERMS = {"python", "py", "java", "javascript", "js", "typescript", "ts",
              "go", "golang", "rust", "c", "c++", "cpp", "cxx", "c#", "csharp",
              ".net", "node.js", "nodejs"}
OP_TERMS_ZH = set(OP_ZH2ID.keys())            # 中文操作词（Task 2 定义）
OP_TERMS_EN = {"insert", "insertion", "delete", "removal", "remove", "traverse",
               "traversal", "find", "search", "lookup", "update", "modify",
               "replace", "add"}

def _tokenize_words(text: str):
    return set(w for w in jieba.cut(text) if w.strip() and not w.isspace())

def _is_alias_piece(word, subject_id):
    from ontology import load
    if not subject_id:
        return False
    w = word.lower()
    for c in load()["concepts"]:
        if c["id"] != subject_id:
            continue
        for a in c["aliases"]:
            if a.lower() in w or w in a.lower():
                return True
    return False

def _residual_words(qp) -> frozenset:
    toks = _tokenize_words(qp.raw_text)
    keep = set()
    for w in toks:
        wl = w.lower()
        if w in STOP_WORDS or w in _RESIDUAL_WHITELIST:
            continue
        if wl in LANG_TERMS or wl in OP_TERMS_EN or w in OP_TERMS_ZH:
            continue
        if _is_alias_piece(w, qp.subject_id):
            continue
        keep.add(w)
    return frozenset(keep)

def _fingerprint_eligible(qp) -> bool:
    return (bool(qp.subject_id)
            and qp.intent not in ("unknown",)
            and not qp.bypass_cache
            and not qp.context_dependent
            and not qp.stateful
            and not _residual_words(qp))

def build_fingerprint(qp) -> str | None:
    if not _fingerprint_eligible(qp):
        return None
    payload = {
        "schema": "v1",
        "ontology_version": qp.ontology_version,
        "parser_version": qp.parser_version,
        "subject_id": qp.subject_id,
        "intent": qp.intent,
        "language": qp.language,
        "operation": qp.operation,
        "output_type": qp.output_type,
        "residual": sorted(qp.residual_words),
    }
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
```

residual 排除依赖 `_RESIDUAL_WHITELIST`（含 `实现/生成/编写/写/使用/用/语言/什么/是…`）+ `LANG_TERMS` + `OP_TERMS_ZH/EN` + subject alias 组成词，其余内容词留下（如 `线程安全/持久化/重复键/父指针/无递归`）。初始白名单须保证"用 Python 实现红黑树插入"残差为空（`用/实现` 白名单、`python`∈LANG、`插入`∈OP、`红黑树` alias 覆盖），而"实现线程安全的 C++ 红黑树"残差含 `线程安全`。实现后跑 `test_fingerprint.py` 校验这两条。

在 `parse()` 结尾补：`qp` 计算 `residual_words = _residual_words(qp)`、`fingerprint_eligible = _fingerprint_eligible(qp)`、`fingerprint = build_fingerprint(qp)`。用 `dataclasses.replace` 或在构造时一并算好。

- [ ] **Step 4: 跑测试通过**

Run: `cd semantic && python3 -m pytest tests/test_fingerprint.py tests/test_parse.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add semantic/parse.py semantic/tests/test_fingerprint.py
git commit -m "feat(semantic): residual 残差 + fingerprint_eligible 保守准入 + 版本化 build_fingerprint"
```

---

### Task 4: decision.py（保守对称硬拒）

**Files:**
- Create: `semantic/decision.py`
- Test: `semantic/tests/test_decision.py`

**Interfaces:**
- Consumes: Task 2/3 `parse/ParsedQuery`。
- Produces: `decide(qp, cp) -> (score: float, shared: bool, reason: str)`；`LANGSENS` 常量；`keyword_score(a,b)->float`。

- [ ] **Step 1: 写失败测试** `semantic/tests/test_decision.py`

```python
import pytest
from parse import parse
from decision import decide

def D(q, c):
    return decide(parse(q), parse(c))

def test_match_rows():
    assert D("生成一个红黑树", "生成一个 rbtree")[1] is True
    assert D("用 C 语言生成红黑树", "使用 C 编写 rbtree")[1] is True
    assert D("红黑树是什么", "what is a red-black tree")[1] is True
    assert D("红黑树的插入", "写一个红黑树的插入")[1] is True

def test_language_rejections():
    assert D("C 实现红黑树", "C++ 实现 rbtree")[1:] == (False, "language_conflict")
    assert D("生成红黑树", "用 C++ 生成红黑树")[1:] == (False, "language_conflict")
    assert D("用 Python 实现单例", "实现单例")[1:] == (False, "language_conflict")  # 查询有语言→候选无语言
    assert D("什么是红黑树", "what is a red-black tree")[1] is True  # 定义不卡语言

def test_intent_rejections():
    assert D("红黑树是什么", "实现一个 red-black tree")[1:] == (False, "intent_conflict")
    # 英文定义句可解析为 definition → 与中文实现句 intent_conflict
    assert D("what is a red-black tree", "用 Python 实现红黑树")[1:] == (False, "intent_conflict")

def test_operation_conservative():
    assert D("Python 实现红黑树插入", "Python 实现 rbtree 删除")[1:] == (False, "operation_conflict")
    assert D("完整实现红黑树", "只实现红黑树插入")[1:] == (False, "operation_conflict")

def test_constraint_rejections():
    assert D("实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树")[1:] == (False, "constraint_conflict")
    assert D("实现带父指针的二叉树", "实现支持重复键的二叉树")[1:] == (False, "constraint_conflict")
    assert D("生成一个红黑树", "实现线程安全的红黑树")[1:] == (False, "constraint_conflict")

def test_subject_boundary():
    assert D("实现一个二叉搜索树", "实现一棵二叉树")[1] is False
    assert D("quick sort", "sort")[1] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_decision.py -q`
Expected: FAIL。

- [ ] **Step 3: 实现 `decision.py`**

```python
"""决策：subject_id/language/operation/intent/residual 保守对称硬拒，然后给分。"""
import jieba
from jieba import analyse
from parse import ParsedQuery

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

def keyword_score(q, c) -> float:
    kw1 = set(analyse.extract_tags(q, topK=6))
    kw2 = set(analyse.extract_tags(c, topK=6))
    if not (kw1 | kw2):
        return 1.0
    return len(kw1 & kw2) / len(kw1 | kw2)

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
    return keyword_score(qp.raw_text, cp.raw_text), True, "ok"
```

- [ ] **Step 4: 跑测试通过**

Run: `cd semantic && python3 -m pytest tests/test_decision.py tests/test_parse.py tests/test_fingerprint.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add semantic/decision.py semantic/tests/test_decision.py
git commit -m "feat(semantic): decision 保守对称硬拒(subject/language/operation/intent/residual)+reason 枚举"
```

---

### Task 5: embedding.py canonical 主题桶

**Files:**
- Create: `semantic/embedding.py`
- Test: `semantic/tests/test_embedding.py`

**Interfaces:**
- Consumes: Task 2 `parse()`（内部取 subject_id）。
- Produces: `embed_text(text) -> list[float]`（256 维，L2 归一）。

- [ ] **Step 1: 写失败测试** `semantic/tests/test_embedding.py`

```python
import math
from embedding import embed_text

def cos(a, b):
    return sum(x*y for x, y in zip(a, b))

def test_canonical_alias_high_cosine():
    v = cos(embed_text("生成一个红黑树"), embed_text("生成一个 rbtree"))
    assert v > 0.55, v
    v2 = cos(embed_text("用 C 语言生成红黑树"), embed_text("使用 C 编写 rbtree"))
    assert v2 > 0.5, v2

def test_cross_topic_low():
    v = cos(embed_text("红黑树是什么"), embed_text("线程池是什么"))
    assert v < 0.35, v

def test_dim_and_norm():
    v = embed_text("生成一个红黑树")
    assert len(v) == 256
    assert abs(math.sqrt(sum(x*x for x in v)) - 1.0) < 1e-6
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_embedding.py -q`
Expected: FAIL（红黑树 vs rbtree 余弦低）。

- [ ] **Step 3: 实现 `embedding.py`（从 semantic.py 的 embed_text 改造）**

把 `semantic.py` 的 `EMBED_DIM/_hash_bucket/_add` 与 `embed_text` 原样搬入；`embed_text` 内把：

```python
sub = extract_subject(text)
```
改为：
```python
from parse import parse
q = parse(text)
sub_id, sub_txt = q.subject_id, q.subject_text
sub = sub_id or sub_txt
```
（标记桶用 `SUBJECT:<subject_id>` 当 ID 命中，否则 `SUBJECT:<subject_text>`）。其余逻辑逐字保留。

- [ ] **Step 4: 跑测试通过**

Run: `cd semantic && python3 -m pytest tests/test_embedding.py -q`
Expected: PASS（阈值达 0.55/0.5/0.35）。若阈值不过，先检查 residual/intent 是否错把别名词当普通词（不应影响主题桶）；只可微调阈值为断言值并注释依据，不得关断言。

- [ ] **Step 5: 提交**

```bash
git add semantic/embedding.py semantic/tests/test_embedding.py
git commit -m "feat(semantic): canonical 嵌入——主题桶用 subject_id(别名共享桶)"
```

---

### Task 6: semantic.py 薄路由 + 新字段 + 服务重启

**Files:**
- Modify: `semantic/semantic.py`
- Modify: `semantic/tests/test_golden.py`（标注 legacy，不删）
- Test: 服务冒烟 curl

**Interfaces:**
- Consumes: Task 1-5 全部纯函数。
- Produces: /embed 响应追加 `subject_id/language/operation/output_type/fingerprint/fingerprint_eligible/parser_version/ontology_version`；/rerank 响应追加 `reason`。保留全部旧字段。

- [ ] **Step 1: 重写 `semantic.py` 为薄路由**

保留 `@route("/healthz")`；`/embed` 与 `/rerank` handler 改为只调纯函数并组装响应：

```python
from nuxt import route, logger, Request
from nuxt.repositorys.validation import fields, use_args
import traceback
from parse import parse, PARSER_VERSION
from ontology import ONTOLOGY_VERSION
from embedding import embed_text
from decision import decide

@route("/embed", methods=["POST"])
@use_args({"text": fields.Str(required=True)}, location="json")
def get_embedding(req: Request, args: dict):
    try:
        text = args["text"]
        q = parse(text)
        return {
            "code": 200,
            "embedding": embed_text(text),
            "bypass_cache": q.bypass_cache,
            "context_dependent": q.context_dependent,
            "intent": q.intent,
            "subject": q.subject_text,
            "subject_id": q.subject_id,
            "language": q.language,
            "operation": q.operation,
            "output_type": q.output_type,
            "fingerprint": q.fingerprint,
            "fingerprint_eligible": q.fingerprint_eligible,
            "parser_version": q.parser_version,
            "ontology_version": q.ontology_version,
        }
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}

@route("/rerank", methods=["POST"])
@use_args({"query": fields.Str(required=True), "cached_query": fields.Str(required=True)}, location="json")
def get_rerank(req: Request, args: dict):
    try:
        score, shared, reason = decide(parse(args["query"]), parse(args["cached_query"]))
        return {"code": 200, "score": score, "shared": shared, "reason": reason}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}
```

（若同目录兄弟 import 在 nuxt 下报错：改为 `sys.path.insert(0, os.path.dirname(__file__))` 后 import，或把纯函数并回单文件——先记录报错再做决定，但**不要**为了绕过而破坏纯函数模块拆分。）

- [ ] **Step 2: py_compile + 离线单测**

Run: `cd semantic && python3 -m py_compile semantic.py parse.py decision.py embedding.py && python3 -m pytest tests/ -q`
Expected: PASS（本任务不含旧 golden 门；golden 已 legacy）。

- [ ] **Step 3: 标注 legacy**：在 `semantic/tests/test_golden.py` 顶部加注释块：

```python
# LEGACY (Phase-0 产物)：canonical 嵌入(FN)Phase-1 有意改变 /embed 输出，本文件不再作 CI 门。
# 权威回归迁移到 tests/ 的 test_parse/test_decision/test_fingerprint/test_embedding + tests/eval。
```

- [ ] **Step 4: 重启 semantic 服务并冒烟**

```bash
kill "$(cat runtime/pids/semantic.pid 2>/dev/null)" 2>/dev/null || true; sleep 1; ./start.sh
ss -tln | grep -E ':3003\b'
curl -s -m 5 http://127.0.0.1:3003/healthz
curl -s -m 5 -X POST http://127.0.0.1:3003/embed -H 'Content-Type: application/json' -d '{"text":"生成一个 rbtree"}'
curl -s -m 5 -X POST http://127.0.0.1:3003/embed -H 'Content-Type: application/json' -d '{"text":"生成一个红黑树"}'
curl -s -m 5 -X POST http://127.0.0.1:3003/rerank -H 'Content-Type: application/json' -d '{"query":"生成一个红黑树","cached_query":"生成一个 rbtree"}'
```
Expected：healthz 200；两个 embed 的 `fingerprint` 相等且非空、`subject_id=red_black_tree`；rerank `shared:true reason:"ok"`。C++ 变体 `shared:false reason:"language_conflict"`。

- [ ] **Step 5: 提交**

```bash
git add semantic/semantic.py semantic/tests/test_golden.py
git commit -m "feat(semantic): 薄路由层——/embed 增结构化+指纹字段、/rerank 增 reason；旧金样标 legacy"
```

---

### Task 7: Go 指纹候选前置 + Top-K + reason 透传

**Files:**
- Modify: `ai-chat-service/pkg/config/config.go`
- Modify: `ai-chat-service/chat-server/semcache/semcache.go`
- Modify: `ai-chat-service/dev.config.yaml`
- Modify: `ai-chat-stack/configs/ai-chat-service.yaml`

**Interfaces:**
- Consumes: Task 6 /embed、/rerank 新字段。
- Produces: `semcache.CacheQuery/CacheWrite` 签名不变；内部按 ①fp→候选→decision 复核 ②VSEARCH Top-K。

- [ ] **Step 1: config.go**：`SemanticCache` 加

```go
		ExactFingerprintEnabled bool `mapstructure:"exact_fingerprint_enabled"`
		TopK                    int  `mapstructure:"top_k"`
```

- [ ] **Step 2: dev/stack yaml** 各加：

```yaml
  exact_fingerprint_enabled: true
  top_k: 30
```

（dev：`ai-chat-service/dev.config.yaml`；stack：`ai-chat-stack/configs/ai-chat-service.yaml` 的 semantic_cache 段，若无该段则建在 dependOn 同级。）

- [ ] **Step 3: semcache.go embedText 返回 meta**：扩 `embedResp` 增 `SubjectID/Language/Operation/OutputType/Fingerprint/FingerprintEligible`；`rerankResp` 增 `Reason`。把 `embedText` 签名改为返回结构：

```go
type embedMeta struct {
	Vec                 []float32
	Bypass              bool
	Subject             string // subject_text
	SubjectID           string
	Fingerprint         string
	FingerprintEligible bool
}
func embedMetaOf(ctx context.Context, text string) (*embedMeta, error)
```

`CacheQuery/CacheWrite` 改用 `embedMetaOf`；逻辑：`bypass || Subject=="" → miss`（同 Phase-0）；`SubjectID==""` 不禁查仅禁 fp。`rerank` 返回加 reason：`func rerank(ctx, query, cached string) (score float64, shared bool, reason string, err error)`，调用点适配。

- [ ] **Step 4: CacheQuery fp 前置**：在 VSEARCH 之前插：

```go
if cnf.SemanticCache.ExactFingerprintEnabled && m.Fingerprint != "" && m.FingerprintEligible {
    if cand, err := getClient().Do(ctx, "GET", "semfp:v1:"+m.Fingerprint); err == nil {
        if cq, ok := cand.(string); ok && cq != "" {
            rs, shared, reason, err := rerank(ctx, query, cq)
            if err == nil && shared && rs >= float64(cnf.SemanticCache.RerankThreshold) {
                if ans, ok2 := fetchAnswer(ctx, cq); ok2 {
                    log.InfoF("fingerprint_hit: subject=%s", m.SubjectID)
                    return ans, true
                }
            } else if err == nil && reason != "ok" {
                log.InfoF("fingerprint_collision: subject=%s reason=%s", m.SubjectID, reason)
            }
        }
    }
}
```

`fetchAnswer(ctx, cachedQ)`：`GET <cachedQ>` 返回 answer（复用现 GET 逻辑，抽成小函数）。

- [ ] **Step 5: VSEARCH Top-K**：`"VSEARCH", strconv.Itoa(dim), binVec, strconv.Itoa(k)`，`k := cnf.SemanticCache.TopK`，默认 30（config 读取后 `<1` 时用 30）。循环内 decision 门不变；`rerank` 现返回 4 值，非 `ok` reason 也当 miss（`shared` 已含）。VSEARCH `res` 数组长度上限 = 2*k 防御。

- [ ] **Step 6: CacheWrite 写 fp**：现有 SET/HSET 之后追加：

```go
if cnf.SemanticCache.ExactFingerprintEnabled && m.Fingerprint != "" && m.FingerprintEligible {
    if _, err := getClient().Do(ctx, "SET", "semfp:v1:"+m.Fingerprint, query); err != nil {
        log.ErrorF("fingerprint_write_failed: %v", err)
    }
}
```

- [ ] **Step 7: 编译 + 重启 + 冒烟**

```bash
cd ai-chat-service/chat-server && go build ./... && cd ../..
kill "$(cat runtime/pids/service.pid 2>/dev/null)" 2>/dev/null || true; sleep 1; ./start.sh
ss -tln | grep -E ':50055\b'
```
端到端最小验证（需一次 LLM 写缓存；用真实链或临时 mock 均可，做法同 Phase-0 Task 6）：发"用 C 语言实现一个红黑树"得到答案后，再发"使用 C 编写 rbtree"→ 期望命中（`source` 显示 cache/秒回）；发"用 C++ 实现红黑树"→ 期望 miss 走 LLM。若不便驱动聊天，则跳过并在报告注明（服务层已由 6 冒烟覆盖），不得以假结果冒充。

- [ ] **Step 8: 提交**

```bash
git add ai-chat-service/pkg/config/config.go ai-chat-service/chat-server/semcache/semcache.go ai-chat-service/dev.config.yaml ai-chat-stack/configs/ai-chat-service.yaml
git commit -m "feat(semcache): 语义指纹候选前置(decision复核)+VSEARCH TopK=30+reason 透传与日志"
```

---

### Task 8: 权威集 eval(≥120) + 文档同步

**Files:**
- Create: `semantic/tests/eval/cases.py`（矩阵 + 模板扩种）
- Create: `semantic/tests/test_eval.py`
- Modify: `docs/项目文档/04-业务应用.md`（§4.2/§6 语义能力到 Phase 1）
- Modify: `semantic/README.md`（/embed /rerank 新字段说明）

**Interfaces:**
- Consumes: Task 1-6 的 parse/decision/embedding/ontology。
- Produces: `tests/eval` 权威数据 + `test_eval.py` 门禁（类别下限 + 总数 ≥120）。

- [ ] **Step 1: 写矩阵 + 扩种 builder `semantic/tests/eval/cases.py`**

```python
# 权威集：MATRIX(手工,来自 spec) + EXPAND(模板扩种)。全量 ≥120，按类设有下限。
from itertools import product

MATRIX = [  # (q, c, expect)  expect: "match" | ("reject", reason)
    ("生成一个红黑树", "生成一个 rbtree", "match"),
    ("用 C 语言生成红黑树", "使用 C 编写 rbtree", "match"),
    ("红黑树是什么", "what is a red-black tree", "match"),
    ("红黑树的插入", "写一个红黑树的插入", "match"),
    ("用 Python 实现红黑树插入", "implement RB-tree insertion in Python", "match"),
    ("C 实现红黑树", "C++ 实现 rbtree", ("reject", "language_conflict")),
    ("生成红黑树", "用 C++ 生成红黑树", ("reject", "language_conflict")),
    ("用 Python 实现单例", "实现单例", ("reject", "language_conflict")),
    ("红黑树是什么", "实现一个 red-black tree", ("reject", "intent_conflict")),
    ("用 Python 实现红黑树插入", "用 Python 实现 rbtree 删除", ("reject", "operation_conflict")),
    ("红黑树插入", "rbtree 删除", ("reject", "operation_conflict")),
    ("完整实现红黑树", "只实现红黑树插入", ("reject", "operation_conflict")),
    ("实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树", ("reject", "constraint_conflict")),
    ("实现带父指针的二叉树", "实现支持重复键的二叉树", ("reject", "constraint_conflict")),
    ("生成一个红黑树", "实现线程安全的红黑树", ("reject", "constraint_conflict")),
    ("实现一个二叉搜索树", "实现一棵二叉树", ("reject", "subject_conflict")),
    ("用 Rust 写一个快速排序", "用 C 写一个快速排序", ("reject", "language_conflict")),
]

# 扩种主体（16 个已建档概念；en 必须是 concepts.json 里的英文 alias）
SUBJ = [
    ("linked_list", "链表", "linked list"),
    ("singleton", "单例", "singleton"),
    ("thread_pool", "线程池", "thread pool"),
    ("stack", "栈", "stack"),
    ("queue", "队列", "queue"),
    ("hash_table", "哈希表", "hash table"),
    ("quick_sort", "快速排序", "quick sort"),
    ("deep_copy", "深拷贝", "deep copy"),
    ("red_black_tree", "红黑树", "red-black tree"),
    ("binary_search_tree", "二叉搜索树", "binary search tree"),
    ("binary_tree", "二叉树", "binary tree"),
    ("heap", "堆", "heap"),
    ("merge_sort", "归并排序", "merge sort"),
    ("insertion_sort", "插入排序", "insertion sort"),
    ("selection_sort", "选择排序", "selection sort"),
    ("graph", "图", "graph"),
]
# 操作类主体子集：插入/删除有意义的容器型概念
OP_SUBJ = [
    ("linked_list", "链表"), ("red_black_tree", "红黑树"), ("binary_tree", "二叉树"),
    ("binary_search_tree", "二叉搜索树"), ("stack", "栈"), ("queue", "队列"),
    ("hash_table", "哈希表"), ("heap", "堆"),
]

def _expand():
    cases = list(MATRIX)
    # 正样本·中↔英措辞（16×3 组）
    for _sid, zh, en in SUBJ:
        cases.append((f"实现一个{zh}", f"implement {en}", "match"))
        cases.append((f"用 Python 实现{zh}", f"implement {en} in python", "match"))
        cases.append((f"写一个{zh}", f"write {en}", "match"))
    # 负样本·语言冲突 C vs C++（16）
    for _sid, zh, _en in SUBJ:
        cases.append((f"用 C 实现{zh}", f"用 C++ 实现{zh}", ("reject", "language_conflict")))
    # 负样本·意图冲突 定义 vs 实现（16）
    for _sid, zh, _en in SUBJ:
        cases.append((f"{zh}是什么", f"实现一个{zh}", ("reject", "intent_conflict")))
    # 负样本·操作冲突 插入 vs 删除（8）
    for _sid, zh in OP_SUBJ:
        cases.append((f"用 Python 实现{zh}插入", f"用 Python 实现{zh}删除", ("reject", "operation_conflict")))
    # 负样本·约束冲突 无约束 vs 线程安全（16）
    for _sid, zh, _en in SUBJ:
        cases.append((f"实现一个{zh}", f"实现一个线程安全的{zh}", ("reject", "constraint_conflict")))
    # 负样本·主题边界（E1 及相似概念对，≥8）
    for (q, c) in [
        ("实现一棵二叉树", "实现一棵二叉搜索树"),
        ("实现二叉搜索树", "实现红黑树"),
        ("写一个快排", "写一个归并排序"),
        ("写一个快速排序", "写一个冒泡排序"),
        ("实现一个插入排序", "实现一个选择排序"),
        ("实现一个链表", "实现一个队列"),
        ("实现一个栈", "实现一个哈希表"),
        ("实现二叉搜索树", "实现一棵二叉树"),
    ]:
        cases.append((q, c, ("reject", "subject_conflict")))
    return cases

CASES = _expand()
```

- [ ] **Step 2: 写门禁 `semantic/tests/test_eval.py`**

```python
import pytest
from decision import decide
from parse import parse
from eval.cases import CASES, MATRIX
from eval import cases as _cases_mod

REASONS = {r for _, c, exp in CASES if isinstance(exp, tuple) for r in [exp[1]]}

def test_total_at_least_120():
    assert len(CASES) >= 120, len(CASES)

def test_matrix_rows_all_pass():
    for q, c, exp in MATRIX:
        shared, reason = decide(parse(q), parse(c))[1:]
        if exp == "match":
            assert shared, (q, c)
        else:
            assert shared is False and reason == exp[1], (q, c, reason)

def test_all_cases():
    bad = []
    for q, c, exp in CASES:
        shared, reason = decide(parse(q), parse(c))[1:]
        if exp == "match":
            if not shared:
                bad.append((q, c, reason))
        else:
            if shared or reason != exp[1]:
                bad.append((q, c, reason, exp))
    assert not bad, f"{len(bad)} failures: {bad[:5]}"

def test_category_floors():
    neg = [x for x in CASES if isinstance(x[2], tuple)]
    assert len(neg) >= 50, len(neg)
    assert sum(1 for x in CASES if x[2] == "match") >= 40
    lang = sum(1 for x in neg if x[2][1] == "language_conflict")
    con = sum(1 for x in neg if x[2][1] == "constraint_conflict")
    op = sum(1 for x in neg if x[2][1] == "operation_conflict")
    intn = sum(1 for x in neg if x[2][1] == "intent_conflict")
    subj = sum(1 for x in neg if x[2][1] == "subject_conflict")
    assert lang >= 10 and con >= 10 and op >= 5 and intn >= 10 and subj >= 6
    assert (len(neg) + sum(1 for x in CASES if x[2] == "match")) == len(CASES)
```

若扩种后仍不足 120，把 `_expand()` 里对应类别按主体×更多模板叠加（正样本再翻倍：`f"用 Python 实现{zh}" vs f"implement {en} in python"`）。类别不足则加模板。最终让 `test_total_at_least_120` 与类别下限全过。

- [ ] **Step 3: 跑全部离线测试**

Run: `cd semantic && python3 -m pytest tests/ -q`
Expected: 全 PASS（test_ontology/test_parse/test_fingerprint/test_decision/test_embedding/test_eval），旧 golden legacy 不进断言套（被上任务注释/或用 marker skip）。

- [ ] **Step 4: 文档同步**

- `docs/项目文档/04-业务应用.md` §4.2：更新语义缓存流程为 `fingerprint 前置 → VSEARCH Top-30 → decision(reason)`；§4.2 rerank 描述指向 Phase1 的保守 decision；§6.2（semantic 节）描述补：双路主题/中英句式/residual/指纹（不存答案、命中复核）。按文档真实文本就近改，保持全章自洽（进程数仍 9）。
- `semantic/README.md`：补 /embed 新字段（subject_id/language/operation/output_type/fingerprint/fingerprint_eligible/…）与 /rerank reason 说明。

- [ ] **Step 5: 提交**

```bash
git add semantic/tests/eval semantic/tests/test_eval.py docs/项目文档/04-业务应用.md semantic/README.md
git commit -m "test(semantic): 权威集 eval≥120(M/R/E+模板扩种)+文档同步到 Phase1"
```

---

## Self-Review 对照

- spec 验收矩阵 M1-M5 → Task2(T3 句式)+Task4 + Task1 本体；R1-R8 → Task4（含 constraint/operation 保守）+ Task3（非 eligible）；B1-B3 → Task2 bypass（既有 helper 保留）；E1-E5 → Task1/Task4/Task3 测试。
- spec §② concepts.json≥40/validator/loader → Task1。
- §③ parse 双路+中英+residual+fp → Task2/3。
- §④ canonical embedding → Task5。
- §⑤ decision 保守对称+reason → Task4。
- §⑥ 指纹安全落地（候选/复核/顺序/一致性）→ Task3(fingerprint)+Task7(Go 前置与写)。
- §⑦ Go config/Top-K/reason 日志 → Task7。
- §数据一致性定义 → Task7 写序与 last-write-wins 说明（缓存节）。
- §测试/评估 ≥120 + 类别 → Task8。
- §风险（nuxt 兄弟 import 回退、gate subject_text）→ Task6 Step1 注明 + Task7 Step3 逻辑。
