# ECHO-CHAT 语义检索 Phase 1.5 实施计划：术语实体识别与受控概念扩展

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让语义检索正确识别软件问答里的术语**实体**及其写法变体（真正别名进指纹、语言库/内建实体与实现族分层、模板参数/namespace/多主题安全处理），以识别验收集驱动、保持 Cache Precision。

**Architecture:** `semantic/` 内：ontology loader 自动生成折叠+有限复数变体并做启动期唯一性校验；新增 `lang_terms.json`（语言→实体表，entity_id/kind/family/namespace/type_args 策略）；parse 重写术语解析（句式→全句扫描、语言门控、multi_subject、type_args/namespace 结构化、fingerprint_safe）；decision 增受控 `family_compat`（默认关）。改动仅 semantic；Go/kvstore/向量命名空间不变。

**Tech Stack:** Python 3.8（semantic）；pytest 离线（语义测试已含 e5 权重，测试环境需模型文件）。

## Global Constraints

- 红线：**语义指纹只对 `alias_of` 同实体且无 type_args、白名单/无 namespace 直命**；`implementation_family` 相同**不**默认共享（family_compat 默认关）；库/内建实体 entity 或 type_args 非空或非白名单 namespace 或 multi_subject → `fingerprint_eligible=false`。
- 取消通用 s/es/ing/ed 词干；仅已审定英文别名的**有限复数变体**（lists/trees 级）。加载期变体唯一性碰撞=启动失败。
- namespace：只白名单规范化（`std::`…）；其它 `X::` 作约束/使指纹不安全。
- 语言门控：弱词（go/swift/ruby/c 单现）需编程上下文锚点；优先级 `objective-c>c++>c#/csharp>c`、`typescript>javascript`、`golang>go`。
- 版本：parser_version="v3"、ontology_version="2026-09-04.1"、lang_terms_version="2026-09-04.1"（fp payload 含 parser_version → 旧 fp 自然失效成孤儿，不迁移）。
- /embed 仍输出旧字段（subject/intent/language/…）以兼容 Go；新增字段仅作可选扩展。
- 命令默认仓库根执行；测试 `cd semantic && python3 -m pytest tests/ -q`（需模型文件在 `semantic/models/e5s-v1/`，缺失则语义相关用例 skip 提示）；禁 sudo/root；勿 `pkill -f`。
- 提交只 stage 本任务文件；仓库脏文件（openai proxy dev.config / kvstore 工作树）不碰。

---

### Task 1: loader 变体折叠 + 白名单 namespace + 有限复数 + 启动唯一性

**Files:**
- Modify: `semantic/ontology/loader.py`、`semantic/ontology/validator.py`
- Test: `semantic/tests/test_ontology.py`（扩展）

**Interfaces:**
- Consumes: 现 `ontology/concepts.json`（保持 id/canonical/aliases 结构）。
- Produces: 保持 `lookup_subject_id(text)->str|None`（alias_of 语义、词干=无）；新增模块级：
  `folding_variants(alias) -> set[str]`、`ENTITY_COUNT` 统计导出（供 healthz）；validator 唯一性改对**原始+折叠+复数**全集执行，碰撞抛异常。
  注意：本任务**不改**任何概念数据/parse，只让 loader 变体逻辑就位且对外行为不回归。

- [ ] **Step 1: 写失败测试**（在 `test_ontology.py` 追加）

```python
from ontology import loader, load, lookup_subject_id
def test_lookup_handles_glued_and_ns_variants():
    # red black tree 折叠变体应命中
    assert lookup_subject_id("redblacktree") == "red_black_tree"
    assert lookup_subject_id("std::rbtree") == "red_black_tree"  # 白名单 std:: 剥离
def test_plural_variant_english():
    # 仅已审定英文别名有限复数：linked lists ↔ linked list（本条依赖 linked_list aliases 含 "linked list"）
    assert lookup_subject_id("linked lists") == "linked_list"
def test_validator_uniqueness_includes_generated():
    # 人为制造碰撞应失败：这里用 monkeypatch 不方便，改为断言 validator 覆盖变体（通过不抛 + 存在性弱断言）
    # 真实负例放实现后用 CLI 试：制造重复别名由单测不易——以“generated 全集唯一”辅助函数暴露测试
    from ontology.validator import _generated_keys
    keys = _generated_keys(load())
    assert len(keys) == len(set(keys))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py -q`
Expected: FAIL。

- [ ] **Step 3: loader 实现折叠与复数（最小、白名单）**

在 `loader.py`：

```python
import re, unicodedata
_NS_WHITELIST = ("std::",)
_GEN_MIN_LEN = 3

def _fold(alias: str) -> str:
    s = unicodedata.normalize("NFKC", alias).lower()
    return re.sub(r"\s+", " ", s).strip()

def _strip_whitelisted_ns(text: str) -> str:
    for p in _NS_WHITELIST:
        if text.startswith(p):
            return text[len(p):]
    return text

def folding_variants(alias: str) -> set:
    a = _fold(alias)
    out = {a}
    base = _strip_whitelisted_ns(a)
    if base != a:
        out.add(base)
    glued = re.sub(r"[\s\-_]", "", base)
    if len(glued) >= _GEN_MIN_LEN and glued != base:
        out.add(glued)
    return out
```

英文**有限复数**：只对已审定别名显式处理——改用**别名条目内显式复数**而非自动词干：常见英文别名补复数条目由数据负责（见 Task-1 Step4），loader **不做** s/es 自动裁剪。故统一“显式复数别名”（列表/数据驱动、可溯源、无词干裁剪）。

**索引集成**：loader 构建查找索引时，把每条别名展开为 `folding_variants(alias)` 后的全部 key 一并入倒排（`lookup_subject_id` 即在这些折叠 key 上做最长/词界匹配），保证 `std::rbtree`、`redblacktree`、`red black trees` 等都能命中对应概念。

`validator._generated_keys(data)`：返回对每条 alias 应用 `folding_variants` 后 ∪ 全集的规范化 key；唯一性检查在其上进行；碰撞 `raise AssertionError`。

- [ ] **Step 4: 复数用显式别名数据（数据驱动、可溯源）**

在 `concepts.json` 相关概念 aliases 显式加入复数形式（仅常见的确定性复数，逐条人工可审）：
`linked list`/`linked lists`；`red black tree`/`red black trees`；`binary search tree`/`binary search trees`（其余不自动加）。跑测试 `test_lookup_handles_glued_and_ns_variants` 中 `linked lists` 用例改为匹配显式复数别名。

- [ ] **Step 5: 跑测试通过 + 全量回归**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py tests/test_parse.py -q`
Expected: PASS（parse 未改不应回归）。

- [ ] **Step 6: 提交**

```bash
git add semantic/ontology/loader.py semantic/ontology/validator.py semantic/ontology/concepts.json semantic/tests/test_ontology.py
git commit -m "feat(ontology): loader 折叠变体(去空格/连字符/下划线+白名单std::剥离)+显式复数别名+唯一性校验含生成变体"
```

---

### Task 2: lang_terms.json + 实体解析层

**Files:**
- Create: `semantic/ontology/lang_terms.json`
- Modify: `semantic/ontology/loader.py`（lang 实体索引 + `TermMatch`）
- Test: `semantic/tests/test_lang_terms.py`（新）

**Interfaces:**
- Produces（供 Task3 parse 用）：
  - `ontology.load_lang_terms() -> dict[lang, dict[term, entity]]`（启动校验 status∈{mapped,ambiguous,unmapped}、mapped 必含 entity_id/kind）
  - `TermMatch` 数据类：`entity_id, kind, family, namespace, type_args(list), surface, source(concept|lang_terms), multi(bool)`；`lookup_lang_entity(lang, text)->list[TermMatch]`（词界、返回全部匹配以便多主题判定）
- 常量：`LANG_TERMS_VERSION="2026-09-04.1"`。

- [ ] **Step 1: 写失败测试 `tests/test_lang_terms.py`**

```python
import pytest
from ontology.loader import lookup_lang_entity, LANG_TERMS_VERSION

def test_cpp_list_entity():
    ms = lookup_lang_entity("cpp", "实现一个 list")
    assert [m.entity_id for m in ms] == ["cpp_std_list"]
    assert ms[0].family == "linked_list" and ms[0].kind == "library_type"

def test_python_list_entity():
    ms = lookup_lang_entity("python", "python 的 list 复杂度")
    assert ms[0].entity_id == "python_builtin_list"
    assert ms[0].family == "dynamic_array"

def test_java_list_ambiguous():
    assert lookup_lang_entity("java", "List") == []        # ambiguous → 不映射

def test_type_args_kept():
    ms = lookup_lang_entity("cpp", "std::list<int>")
    assert ms[0].entity_id == "cpp_std_list" and ms[0].type_args == ["int"]
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL（import 错误）。

- [ ] **Step 3: 建 `lang_terms.json`**

```json
{
  "lang_terms_version": "2026-09-04.1",
  "languages": {
    "cpp": {
      "list":  {"status":"mapped","entity_id":"cpp_std_list","kind":"library_type","namespace":"std","family":"linked_list","type_args":"keep"},
      "vector":{"status":"mapped","entity_id":"cpp_std_vector","kind":"library_type","namespace":"std","family":"dynamic_array","type_args":"keep"},
      "map":   {"status":"mapped","entity_id":"cpp_std_map","kind":"library_type","namespace":"std","family":null,"type_args":"keep"},
      "unordered_map":{"status":"mapped","entity_id":"cpp_std_unordered_map","kind":"library_type","namespace":"std","family":"hash_table","type_args":"keep"}
    },
    "python": {
      "list": {"status":"mapped","entity_id":"python_builtin_list","kind":"builtin_type","family":"dynamic_array"},
      "dict": {"status":"mapped","entity_id":"python_builtin_dict","kind":"builtin_type","family":"hash_table"},
      "set":  {"status":"mapped","entity_id":"python_builtin_set","kind":"builtin_type","family":null}
    },
    "java": {
      "List": {"status":"ambiguous"},
      "ArrayList": {"status":"mapped","entity_id":"java_array_list","kind":"class","family":"dynamic_array"}
    }
  }
}
```
（其余语言后续按验收集增量补。）

- [ ] **Step 4: loader 实现**

`load_lang_terms()`：读文件；校验：status 枚举、mapped 必含 entity_id/kind；ambiguous/unmapped 视同不可映射（返回 []）。
`TermMatch` 与 `lookup_lang_entity(lang, text)`：
- 对 `languages[lang]` 每词在 `text` 上做**词界**匹配（拉丁用 ASCII lookaround；CJK 用受控子串）；
- 命中词后取**整 token 区间** `surface`；若词前紧跟 `std::` 且白名单则 `namespace="std"`；
- 解析紧跟的 `<...>`（限一层、≤64 字符、内层无 `<`），得 `type_args`；
- 返回全部命中（供多主题）。

- [ ] **Step 5: 跑测试通过**

Run: `cd semantic && python3 -m pytest tests/test_lang_terms.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add semantic/ontology/lang_terms.json semantic/ontology/loader.py semantic/tests/test_lang_terms.py
git commit -m "feat(ontology): lang_terms 语言实体表(cpp/python/java 首批)+lookup_lang_entity→TermMatch(entity/kind/family/type_args)"
```

---

### Task 3: parse 术语实体化 + 指纹安全 + 语言门控 + multi_subject + 版本

**Files:**
- Modify: `semantic/parse.py`、`semantic/semantic.py`（/embed 可选新字段、版本常量）
- Modify/Test: `semantic/tests/test_parse.py`、`semantic/tests/test_fingerprint.py`、`semantic/tests/test_decision.py`（必要修正）
- Test: `semantic/tests/test_parse_entity.py`（新）

**Interfaces:**
- Consumes: Task1 folding/lookup_subject_id、Task2 lookup_lang_entity/TermMatch。
- Produces:
  - `ParsedQuery` 增（兼容旧字段保留）：`subject_kind, implementation_family, namespace, type_args(tuple), matched_surface, multi_subject, reason`；`parser_version="v3"`、`ontology_version/lang_terms_version`。
  - `subject_id` 语义：alias_of 概念→概念 id；库/内建实体→entity_id；多主题/未解析→None。
  - `fingerprint_eligible`：仅 alias_of 概念且无 type_args 且 namespace∈{None,"std" 白名单} 且非 multi_subject 且 intent 已知 且 residual 空 时为 True；库/内建实体 entity_id 或 type_args 非空或非白名单 ns → False。
  - `extract_language` 保留；新增 `detect_language(text)` 门控（弱词需编程锚点）并让 extract_language 用其强/中证据结果（保持既有识别回归）。
- /embed 增可选字段（subject_kind/implementation_family/type_args/multi_subject），不改旧字段名。

- [ ] **Step 1: 写失败测试 `tests/test_parse_entity.py`**

```python
from parse import parse

def test_cpp_list_entity_not_fp():
    q = parse("用 C++ 实现一个 list")
    assert q.subject_id == "cpp_std_list"
    assert q.subject_kind == "library_type"
    assert q.language == "cpp"
    assert q.fingerprint_eligible is False      # 库实体 → 只向量/decision

def test_type_args_block_fp():
    q = parse("std::list<int> 怎么遍历")
    assert q.type_args == ("int",)
    assert q.fingerprint_eligible is False

def test_alias_concept_still_fp():
    q = parse("生成一个 rbtree")
    assert q.subject_id == "red_black_tree"
    assert q.fingerprint_eligible is True

def test_multi_subject():
    q = parse("C++ list 和 vector 有什么区别")
    assert q.multi_subject is True and q.fingerprint_eligible is False

def test_no_lang_ambiguous_word_miss():
    q = parse("写一个 list")          # 无语言歧义 → subject None
    assert q.subject_id is None and q.fingerprint_eligible is False

def test_lang_gating_weak_words():
    assert parse("go to the next step").language is None
    assert parse("go 语言的 map").language == "golang"
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL。

- [ ] **Step 3: 语言门控**

在 parse 加：

```python
_WEAK_LANGS = {"go": ("go",), "swift": ("swift",), "ruby": ("ruby",), "c": ()}
_STRONG_HINTS = {"golang": ("golang",), "c++": ("c++", "cpp", "cxx"),
                 "python": ("python",), "rust": ("rust",), "typescript": ("typescript", "ts"),
                 "javascript": ("javascript", "js"), "csharp": ("c#", "csharp"),
                 "swift": ("swift struct", "swift enum", "swift 语言"), "ruby": ("ruby array", "ruby 语言"),
                 "go": ("go 语言", "golang"), "php": ("php",), "kotlin": ("kotlin",)}
_PROG_ANCHOR = re.compile(r"(实现|编写|代码|类型|类|接口|函数|方法|数组|map|list|struct|class|func|interface|var|import|return)", re.I)
def detect_language(text) -> str | None:
    low = " ".join(text.lower().split())
    # 强证据（长 token/符号）
    order = ["objective-c","c++","cpp","cxx","c#","csharp","dotnet","golang","typescript","ts","javascript","js","python","rust","go","php","swift","kotlin","ruby","c"]
    for tok in order:  # c 单独处理
        ...
    # 弱词：go/swift/ruby/c 需 _PROG_ANCHOR 命中或技术词共现才判
```
实现细则：先扫强证据（`golang/python/c++/cpp/...` 与现 LANG_PATTERNS 一致但剔除弱短词），弱短词（`go/swift/ruby`）仅当句中出现编程锚点时识别；`c` 仅 `c语言` 或实现语境。**回归要求**：现有 `test_parse.py` 的语言断言（用Go/用Python/用 C++/c 语境等）不回归；其实现逻辑可复用 extract_language 的强模式。实现后先跑全量 parse 测试确认旧用例仍绿再继续。

- [ ] **Step 4: parse 实体化主流程**

将原 `_extract_subject_pair`/`parse` 中 subject 解析替换为：句式→span；空则全句；在 span 上 `lookup_subject_id`（alias_of 概念）；若空且 `language` 已判定，`lookup_lang_entity(lang, span)` 得候选实体列表：
- ≥2 个不同 entity → `multi_subject=True, subject_id=None, eligible=False, reason=multiple_subjects`
- 恰 1 → entity（entity_id 为 subject_id；kind/family/namespace/type_args 填）
- 0 → subject_id=None, reason=subject_unresolved
residual 计算需把**命中 surface 及其实体别名族**计入 cover（保证 `std::list`/`cpp_std_list` 的相关 token 被消掉而 `int` 等 type_args 保留为约束）。
`fingerprint_safe` 判定见 Interfaces（库/内建实体→eligible False）。

注意兼容：`subject`(text) 旧字段=matched_surface 或句式原文；`intent/language/operation/output_type/bypass` 逻辑不变；`residual_words` 仅 eligible 路径需要为空（entity 路径 eligible=False，不强求空）。

- [ ] **Step 5: 版本常量与 /embed 可选字段**

- parse 顶部：`PARSER_VERSION="v3"`；ontology/lang_terms 版本从各自模块 import（Task1/2 已设）。
- `/embed` 响应增：`subject_kind/implementation_family/type_args(list)/multi_subject`（无则 null/false）；旧字段名不变。

- [ ] **Step 6: 全量回归 + 修正受影响的既有断言**

Run: `cd semantic && python3 -m pytest tests/ -q`
逐项处理回归：
- fp 因 parser_version v3 变化 → 既有相等性用例（同文本同侧）仍应绿；若某用例断言“旧指纹值”需改为只断言相等性。
- `test_parse.py` 语言用例须不回归（Task3 Step3 已保证）。
- `test_eval`/`test_decision`/`test_embedding` 若有因 subject 语义变化(实体化后部分歧义词从 None 变实体)受影响 → 依据 spec 更新该行期望并注释原因。
Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add semantic/parse.py semantic/semantic.py semantic/tests/test_parse_entity.py semantic/tests/test_parse.py semantic/tests/test_fingerprint.py semantic/tests/test_decision.py
git commit -m "feat(parse): 术语实体化+指纹安全(entity/type_args/ns/multi→eligibleFalse)+语言门控+parser v3+embed 扩展字段"
```

---

### Task 4: decision family_compat（受控、默认关）

**Files:**
- Modify: `semantic/decision.py`、`semantic/semantic.py`（/v1/decision 透传 source）
- Test: `semantic/tests/test_decision.py`（增）或新 `test_family_compat.py`

**Interfaces:**
- Consumes: Task3 `implementation_family`、type_args、multi_subject。
- Produces: `decide()` 增可选 family 兼容：开关 env `SEMANTIC_FAMILY_COMPAT`（默认 "0"）；命中记 `reason="ok"`、soft=False、额外 `source="family_compat"`（/v1/decision 响应对已存在字段外补 `source`，旧字段不变）。

- [ ] **Step 1: 写失败测试**

```python
import os
from parse import parse
from decision import decide

def _d(q, c):
    return decide(parse(q), parse(c))

def test_family_compat_off_by_default_rejects_cross_entity():
    # cpp_std_list(库) vs linked_list(抽象) 默认不共享
    s, r, soft = _d("用 C++ 写一个 list", "用 cpp 实现链表")
    assert s is False or r in ("unknown_subject", "subject_conflict", "intent_conflict")

def test_family_compat_on_allows_clean_implementation_pair():
    os.environ["SEMANTIC_FAMILY_COMPAT"] = "1"
    try:
        s, r, soft = _d("用 C++ 写一个 list", "用 cpp 实现链表")
        # subject 已分别解析为 cpp_std_list / linked_list？需两实体同 family；此断言视实现：若两侧不同实体+同 family+implementation+无模板 → shared True reason ok source family_compat
    finally:
        os.environ.pop("SEMANTIC_FAMILY_COMPAT", None)
```

- [ ] **Step 2: 跑测试确认失败**

Expected: FAIL。

- [ ] **Step 3: 实现**

在 decision 的 subject 判定**之后**、语言/操作/意图/residual 之前插入（仅 family_compat 开启时）：

```python
def _family_compat(qp, cp) -> bool:
    if qp.multi_subject or cp.multi_subject:
        return False
    fam_q = getattr(qp, "implementation_family", None)
    fam_c = getattr(cp, "implementation_family", None)
    if not fam_q or not fam_c or fam_q != fam_c:
        return False
    if qp.type_args or cp.type_args:
        return False
    # 任一侧是无模板的库/内建实体、另一侧为抽象概念或同 family 实体，意图均实现/定义、语言一致
    return (qp.subject_id != cp.subject_id
            and qp.intent in ("implementation", "definition")
            and cp.intent in ("implementation", "definition")
            and qp.language == cp.language)
```

满足时视同 subject 兼容并继续（reason ok，source=family_compat）；不满足则回到原保守 subject 判定（不同实体→拒）。

- [ ] **Step 4: 共享判定集（Precision 门）**

新增 `semantic/tests/eval/family_share_cases.py`（≥30 对）：正例=同 family+实现/定义+无模板+同语言（cpp list↔cpp 链表、python list↔python 动态数组）；负例=跨语言、带模板、splice/API 类（实体边界不同）、操作冲突。开关开时断言 负例 仍 shared False；正例 shared True。跑通后记录报告，作为"是否默认开"的证据（保持默认关）。

- [ ] **Step 5: 跑测试通过 + 提交**

```bash
cd semantic && python3 -m pytest tests/ -q
git add semantic/decision.py semantic/semantic.py semantic/tests/test_decision.py semantic/tests/eval/family_share_cases.py
git commit -m "feat(decision): family_compat 受控跨实体共享(默认关,env 开关,source=family_compat)+共享判定集 Precision 门"
```

---

### Task 5: 识别/边界验收集 + 端到端 + 文档

**Files:**
- Create: `semantic/tests/eval/term_entity_cases.py`
- Modify: `semantic/tests/test_eval.py`（跑该集）、`docs/项目文档/04-业务应用.md`、`semantic/README.md`（如需）

**Interfaces:**
- Consumes: Task1-4。
- Produces：验收集 + 测试（含端到端 parse→fp→decision 断言）、文档更新。

- [ ] **Step 1: 建 `term_entity_cases.py`**

含四段清单（全部走**完整 parse()**）：

```python
RECOGNIZE = [  # (短语, 期望subject_id, 期望language, 备注)
    ("红黑树", "red_black_tree", None, "alias zh"),
    ("rbtree", "red_black_tree", None, "alias abbr"),
    ("red-black tree", "red_black_tree", None, "alias en"),
    ("red black trees", "red_black_tree", None, "plural alias"),
    ("用 C++ 写一个 list", "cpp_std_list", "cpp", "lang entity"),
    ("python 的 list", "python_builtin_list", "python", "lang entity"),
    ("实现一个 cpp rbtree", "red_black_tree", "cpp", "fold+lang"),
    ("std::vector<int>", "cpp_std_vector", "cpp", "entity+type_args"),
]
MISS = [  # (短语, reason子串或None) 期望 subject None 或 multi
    ("写一个 list", "subject_unresolved"),
    ("go to the next step", "subject_unresolved"),
    ("a swift response", "subject_unresolved"),
    ("C++ list 和 vector 有什么区别", "multiple_subjects"),
    ("java 的 List", "subject_unresolved"),
]
REJECT_PAIRS = [  # (q, c, 期望: shared False)
    ("生成一个红黑树", "用 C++ 实现 std::list"),
    ("std::list<int> 怎么遍历", "std::list<string> 怎么遍历"),
    ("std::list 怎么 splice", "实现一个 C++ 链表"),
    ("python list append", "用 python 实现动态数组"),
    ("company::list", "std::list"),   # namespace 不同 → subject 均未映射? company::list 未映射→None
]
FP_ELIGIBLE_SAFE = [("生成一个 rbtree", True), ("用 C++ 写一个 list", False), ("std::list<int>", False)]
```

- [ ] **Step 2: 跑测试确认失败并逐条校正**

Run: `cd semantic && python3 -m pytest tests/test_eval.py -q`
Expected: 多数 FAIL——按 spec 原则逐条实现修正：
- 识别错 → 补本体别名/lang_terms/折叠（回 Task1/2 数据），**不改判据迁就**；
- 语义确实应拒绝的 → 保留并确认是 decision/hard 层面拦截而非 subject 误识别；
- namespace 异（`company::list`）→ 全局不映射、lang 表只 std 白名单——断言其 subject None 即可（从 REJECT_PAIRS 挪到 MISS 更准确，逐条处理）。
修正后全绿，并把每条处置写进报告（识别对/数据补/期望改）。

- [ ] **Step 3: 端到端（parse→fingerprint→decision）**

新用例（`tests/test_eval.py` 增）：
- fp 相等对：`红黑树/rbtree/red-black tree` fp 相等且 eligible；
- `std::list<int>` 对 `std::list<int>` 同文本 fp 相等但 eligible False（直命不启用、仍靠 decision）——验证其指纹一致但被 gate 挡住不产生危险；
- 决策对：`用 C++ 写一个 list ↔ 用 cpp 实现链表`（默认关 family_compat 下）→ 拒绝或 miss；`生成一个红黑树 ↔ 生成一个rbtree` → shared True。
跑 `cd semantic && python3 -m pytest tests/ -q` 全绿。

- [ ] **Step 4: 文档同步**

- `semantic/README.md`：术语识别说明（三层、entity 分层、仅 alias_of 进指纹、type_args/多主题 gate、语言门控、版本）。
- `docs/项目文档/04-业务应用.md` §4.2/§6.2：语义检索 subject 解析升级为实体化（如需要一句话）。

- [ ] **Step 5: 提交**

```bash
git add semantic/tests/eval/term_entity_cases.py semantic/tests/test_eval.py semantic/ontology/concepts.json semantic/ontology/lang_terms.json docs/项目文档/04-业务应用.md semantic/README.md
git commit -m "test(eval): 术语识别/边界验收集(识别+miss+拒绝对+fp-safe)+端到端 + 文档同步"
```

---

## Self-Review 对照（spec 覆盖）

- 变体折叠+白名单 ns+有限复数+唯一性 → Task1（复数改显式数据、无词干裁剪，落实 P0-6/8 部分）
- lang_terms 实体表+TermMatch+type_args → Task2（P0-2/3/4/5）
- parse 实体化+指纹安全(entity/type_args/ns/multi→eligible False)+语言门控+版本 v3 → Task3（P0-1 拒绝样例走 REJECT_PAIRS、P0-7、P0-8、澄清1/2/3/5）
- family_compat 默认关+Precision 门 → Task4
- 识别/边界验收集+端到端 → Task5（评审 §七 各集以代码注释标注并纳入）
- 明确不做：API 全量/算法穷举/设计模式/LLM 别名/Go 改动 —— 全局约束外显。
