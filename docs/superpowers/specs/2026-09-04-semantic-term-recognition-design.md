# ECHO-CHAT 语义检索 · Phase 1.5：术语实体识别与受控概念扩展 设计文档

- 日期：2026-09-04
- 状态：已批准（吸收评审 P0/P1 修订后）
- 位置：ECHO-CHAT `semantic/`（ontology 实体化 + 变体折叠 loader + `lang_terms.json` 语言实体表 + parse 术语解析 + 版本隔离）
- 关联：`proj/tmp/ECHO-CHAT术语识别扩展迭代方案评审意见.md`（评审，有条件批准）、`docs/superpowers/specs/2026-09-03-semantic-phase1-rule-alias-design.md`、`2026-09-04-semantic-phase2-3-e5-hybrid-design.md`

> **定位**：本阶段只做"术语**实体**识别与受控概念扩展"，**不把'识别相近'直接推导为'可共享缓存答案'**。答案可共享与否仍由实体边界、意图、操作、类型参数与硬约束共同决定。语义指纹**只对 `alias_of` 合并**。

## 背景与目标

语义缓存对软件问答里的语言歧义词/写法变体识别不足（例：`用 C++ 写一个 list` 无法识别、`python list` 不应命中 `链表` 答案）。目标：以识别验收集驱动，把"术语→实体"识别做对；同时**绝不**因 `implementation_family` 相同就默认共享缓存答案，维持 Cache Precision 优先。

## 评审修订记录（2026-09-04）

| # | 意见 | 处置 |
|---|---|---|
| P0-1 | 文档 `std::list<T>≈红黑树` 是概念错误 | 采纳：拆成两组示例；加 `C++ rbtree ↔ std::list<int>` 拒绝测试 |
| P0-2 | 库实体与底层结构不得共用 subject_id | 采纳：**entity_id** 进硬门/指纹；`implementation_family` 仅召回扩展；跨 family 共享需受控条件（见 §决策 G） |
| P0-3 | 模板参数不得无条件剥除 | 采纳：`type_args` 结构化；非空→指纹不可直命（fingerprint_eligible=false），仅向量+决策 |
| P0-4 | 匹配须返回完整 span | 采纳：resolver 返回 `TermMatch(entity_id, surface, spans, namespace, type_args, source)`；residual cover 结构化 |
| P0-5 | 任意 `X::` 前缀剥离过宽 | 采纳：仅白名单 namespace（`std::`…）规范化；其它 namespace 作约束或使指纹不安全 |
| P0-6 | 通用 s/es/ing/ed 词干裁剪有误 | 采纳：取消通用词干；仅从已审定英文别名生成**有限复数变体**（lists/trees），全程碰撞检查、可溯源 |
| P0-7 | go/swift/ruby/c 裸词误检 | 采纳：语言证据分级（强/中/弱）；弱证据需编程上下文锚点；优先级表明确 |
| P0-8 | 本体/解析变更需版本隔离 | 采纳：parser_version=v3、ontology/lang_terms 版本新值；fp 已含 parser_version → 旧 fp 值自然失效成孤儿、无跨版误用；向量命名空间不变（模型未变）；说明可选的旧缓存重解析 |
| 澄清2 | `null` 语义含混 | 采纳：用显式 `status: ambiguous\|unmapped\|mapped` |
| 澄清3 | 多主题 ≠ 无主题 | 采纳：`multi_subject=true/fingerprint_eligible=false/reason=multiple_subjects` |
| 澄清5 | 启动碰撞 vs 查询多主题 | 采纳：加载期碰撞=配置错误→**启动失败**（唯一行为）；查询期多主题=合法→绕过单主题指纹 |
| 澄清1 | 句式可能是瓶颈 | 采纳：句式抽不到时**保守全句扫描**，多实体→multi_subject |
| 澄清4 | map 语义不绑定实现 | 采纳：`cpp_std_map`/`cpp_std_unordered_map` 分开 entity，family 分别是 ordered_map?/hash_table，不归 red_black_tree |
| 性能 | 顺序扫表、无索引 | 采纳：加载期建 alias 索引；语言分区查询；预编译正则；/healthz 报版本与加载统计 |

## 已确认决策（含评审修订）

1. **三层识别**：全局本体(alias_of)→变体折叠→语言实体表。
2. **实体分层**：真正别名(`alias_of`，如 rbtree→red_black_tree)可进同一指纹；**库/内建实体**(entity_id)与实现族(family)分开，family 相同**不**默认共享答案。
3. **指纹安全**：`type_args` 非空、非白名单 namespace、`multi_subject`、无实体 → `fingerprint_eligible=false`（仅向量召回 + decision）。
4. **模板/namespace**：结构化保留；只认白名单 `std::` 类前缀做规范化。
5. **词干**：无通用裁剪；仅已审定英文别名的有限复数变体。
6. **语言门控**：强/中/弱证据；弱词需编程上下文锚点；优先级 `objective-c>c++>c#/csharp>c`、`typescript>javascript`、`golang>go`。
7. **版本**：parser v3 / ontology·lang_terms 2026-09-04.1；fp 含版本→旧值孤儿、无跨版混用。
8. **验收**：识别集 + 拒绝集 + 缓存共享判定对 + 端到端 parse→fp→retrieval→decision；不以"约 65 概念"为硬目标，以测试覆盖与正确性为准。
9. **改动域**：仅 `semantic`（数据/loader/parse/测试）；Go/kvstore 结构不动（fp 已版本化，向量 ns 不变）。

## 实体与关系模型

```text
alias_of               真正同义词 → 可共享指纹（rbtree → red_black_tree）
library_entity_of      库实体与抽象概念关联（cpp_std_list → linked_list, 仅召回扩展）
implementation_family  常见实现族（仅排序/召回，不直接共享）
ambiguous / unmapped   保守 miss（区分：上下文不足 / 未建模）
```

全局本体 `concepts.json`：语言无关概念（linked_list、dynamic_array、hash_table…），别名走 `alias_of`（含自动折叠有限复数变体）。

`lang_terms.json`：`lang → {词 → entity}`，例如：

```json
{
  "cpp": {
    "list":  {"status":"mapped", "entity_id":"cpp_std_list",  "kind":"library_type",
              "namespace":"std", "family":"linked_list",   "type_args":"keep"},
    "vector":{"status":"mapped", "entity_id":"cpp_std_vector","kind":"library_type",
              "namespace":"std", "family":"dynamic_array", "type_args":"keep"},
    "map":   {"status":"mapped", "entity_id":"cpp_std_map",   "kind":"library_type",
              "namespace":"std", "family":null},
    "unordered_map":{"status":"mapped","entity_id":"cpp_std_unordered_map","kind":"library_type",
                     "namespace":"std", "family":"hash_table"}
  },
  "python": {
    "list": {"status":"mapped","entity_id":"python_builtin_list","kind":"builtin_type","family":"dynamic_array"},
    "dict": {"status":"mapped","entity_id":"python_builtin_dict","kind":"builtin_type","family":"hash_table"},
    "set":  {"status":"mapped","entity_id":"python_builtin_set"," kind":"builtin_type","family":null}
  },
  "java": {
    "List": {"status":"ambiguous"},
    "ArrayList": {"status":"mapped","entity_id":"java_array_list","kind":"class","family":"dynamic_array"}
  }
}
```

解析结果（semantic 内部，/embed 仍回填旧字段以兼容）：

```json
{
  "primary_subject_id": "cpp_std_list", "subject_kind": "library_type",
  "language": "cpp", "implementation_family": "linked_list",
  "namespace": "std", "type_args": ["int"], "matched_surface": "std::list<int>",
  "multi_subject": false, "fingerprint_eligible": false,
  "parser_version": "v3", "ontology_version": "2026-09-04.1", "lang_terms_version": "2026-09-04.1"
}
```

`/embed` 对外仍输出旧 `subject/intent/language/...`（`subject`=matched surface 或旧 subject_text；`subject_id`=primary_subject_id；新增可选 `subject_kind/implementation_family/type_args/multi_subject`），Go 旧字段消费不变。

## 组件设计

### ① 全局本体与变体折叠（loader）

- 概念补容器/DS（linked_list/dynamic_array/deque/doubly_linked_list…），数量**以验收集覆盖为准**。
- loader 对每条 `alias_of` 别名自动生成折叠变体：NFKC+小写+空白折叠；去空格/连字符/下划线（长度≥3 互异）；**仅白名单 namespace**（`std::` 等）规范化；**取消通用词干**，仅对已审定英文别名生成有限复数变体（lists/trees）。
- 唯一性校验：所有"原始+生成变体"在加载期全局唯一，**碰撞即启动失败**（唯一行为）。

### ② parse 术语解析（替代原 subject 两步，保留兼容）

```text
normalize → detect_language(带门控)
spans = 句式抽取; 若空 → [full text] 保守全句扫描
matches = 全局实体(alias_of) 或(若语言已知) lang_entities
matches = 最长非重叠(matches)
若 ≥2 个不同实体 → multi_subject=true, eligible=false, reason=multiple_subjects
无 → subject_unresolved (eligible=false)
单实体 → 结构化 namespace/type_args; 计算 constraints; fingerprint_safe 判定
residual/fingerprint 在 resolve 后
```

- 语言检测：强（cpp/golang/python 等长 token/`c++`/代码围栏）、中（go map、swift struct 等 技术词+意图 共现）、弱（裸 go/swift/ruby/c 单现 → 不计）。优先级表照评审。
- `c` 仍仅 `c语言` 或实现语境；`objective-c`>c++>csharp>c。

### ③ 指纹安全（评审 P0-2/P0-3）

- 语义指纹只对 **alias_of 同实体 + 无 type_args + 白名单/无 namespace** 的干净查询可直命。
- 下列情形 **fingerprint_eligible=false**，只走 VSEARCH + decision：
  - `entity_id` 为库/内建实体（非 alias_of 概念）；
  - `type_args` 非空；
  - 非白名单 namespace 或自定义 `X::`；
  - `multi_subject` / 无法解析。
- **受控 family 复用（可选、默认关）**：`family_compat.enabled` 为 true 时，decision 允许 两侧 family 相同且均为 `implementation|definition`、语言一致、无 type_args、无其余约束冲突 的候选共享答案（记 reason ok、source=family_compat）。**默认 false**；开启需在共享判定集上验证 Precision≥99%。

### ④ 版本与缓存

- `parser_version=v3`、`ontology_version/lang_terms_version=2026-09-04.1`；fingerprint payload 已含 parser/ontology 版本 → **旧 `semfp` 值对同一文本不再命中**（新值不同），无跨版误用；旧键成孤儿可后续清理。embedding/向量命名空间 `semd:e5s:v1:` 不变（模型未换）。可选：对存量明文问题重解析回填 fp（工具，非必须）。
- `/healthz`/`/readyz` 报 ontology/lang_terms/parser 版本与加载统计（概念数/别名数/变体数/歧义数），不记敏感原始查询。

## 测试与验收

识别验收集拆分（评审 §七）：

| 集 | 数量目标 | 内容 |
|---|---:|---|
| 全局无歧义别名 | 80-120 | 中英概念 + 规范变体 |
| 语言实体词 | 100-150 | 各语言容器/内建实体 |
| 语言误检负例 | 50-100 | go/swift/ruby/c 自然语言冲突 |
| 模板与 namespace | 40-80 | `std::list<int>`、非 std 前缀、完整 span |
| 多主题查询 | 30-50 | compare/convert → multi_subject 绕过 |
| 缓存共享判定对 | 150-300 | "识别相同 ≠ 可共享" |

必含正例：`红黑树/rbtree/red-black tree→red_black_tree`；`C++ std::list<int>→cpp_std_list + type_args=[int]`；`Python list→python_builtin_list`。
必含拒绝/边界：`rbtree ↔ std::list<int>` 主题冲突；`std::list<int> ↔ std::list<string>` 类型约束不同不直命；`std::list splice ↔ 实现 C++ 链表` 实体边界不同；`Python list append ↔ Python 实现动态数组` 不默认共享；`go to the next step→language=None`；`company::list ↔ std::list` namespace 不同；`C++ list 与 vector 的区别→multi_subject`。

测试方法：验收集逐条走**完整 parse()**（不绕过句式层）；碰撞/唯一性（启动期）；差分（新老 parser 对照逐项审）；轮换对称（query/candidate 交换硬冲突不变）；端到端 `parse→fp→VSEARCH→decision`；共享判定集在 family_compat 开关两态各跑。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| family 误共享致 False Hit | family_compat 默认关 + Precision≥99% 门；fingerprint 只认 alias_of |
| 折叠/复数变体碰撞 | 加载期全局唯一校验，碰撞启动失败（唯一行为） |
| 模板参数误合并 | type_args 保留 + 非空即指纹不可直命 |
| namespace 盲删 | 白名单规范化，其余作约束/使指纹不安全 |
| go/swift/ruby/c 误检 | 语言证据分级 + 上下文锚点 |
| 版本混用 | fp 含 parser/ontology 版本自动隔离；向量 ns 不变 |
| 句式层漏识别 | 句式空→保守全句扫描；多实体→multi_subject |

## 明确不做（Out of scope）

API/方法级全量词典；算法穷举；设计模式/框架全量；LLM 生成别名；英文通用动词消歧；改 Go/kvstore/decision 结构；向量模型更换；把 `implementation_family` 直接进指纹。

## 涉及文件清单

新增：
- `semantic/ontology/lang_terms.json`
- `semantic/tests/eval/term_entity_cases.py`（识别/拒绝/多主题/模板/namespace）
- `semantic/tools/` 差分与共享判定脚本（可选）

修改：
- `semantic/ontology/concepts.json`（容器/DS 概念，受测试集驱动）
- `semantic/ontology/loader.py`（折叠+白名单 namespace+有限复数变体+唯一校验+lang_terms 加载）
- `semantic/parse.py`（术语解析/实体化/语言门控/版本）、`semantic/semantic.py`（/embed 增可选字段、healthz 版本）
- `semantic/tests/test_ontology.py`、`test_parse.py`、`test_fingerprint.py` 等
- `docs/项目文档/04-业务应用.md`/`semantic/README.md`（如需）
