# ECHO-CHAT 语义检索 · 术语识别扩展设计文档（软件常用词/技术词变体正确识别）

- 日期：2026-09-04
- 状态：已批准（brainstorming 逐段确认）
- 位置：ECHO-CHAT `semantic/`（ontology 扩展 + 变体折叠 loader + `lang_terms.json` 语言受限消歧 + parse 第三步 subject resolve + 语言检测扩展 + 识别验收集）
- 关联：`docs/superpowers/specs/2026-09-03-semantic-phase1-rule-alias-design.md`（Phase1 规则/本体基础）、`2026-09-04-semantic-phase2-3-e5-hybrid-design.md`（Phase2+3）

## 背景与目标

Phase-1 本体为精选 ~40 概念、别名全局唯一、语言检测按词界。问题：软件问答里大量**语言歧义的容器/库词**与**同一概念的各种写法变体**未被正确识别，导致应命中的缓存对 miss（例：`用 C++ 写一个 list` 无法复用 `用 cpp 实现链表`；而 `python 的 list` 不应命中 `链表` 答案）。用户要求把"软开中各种常用词/技术词的各种变体"的正确识别做到位。

**目标**：以**识别验收集**驱动，实现三层识别（全局本体 + 自动变体折叠 + 语言受限消歧），使
```text
C++/cpp 语境 list=链表      python 语境 list=动态数组
red-black tree≈rbtree≈red black tree≈std::list<T>
实现 cpp rbtree             （空格粘连不丢语言）
```
等正确命中，同时**不**让 `python list` 命中 `链表` 答案、`java List` 不武断归因。

## 已确认决策

1. **范围**：①自动字面折叠变体 ②本体扩到"容器+数据结构先行"(~40→~65 概念) ③按语言做容器/库词受限表，覆盖常见语言（含补 PHP/Swift/Kotlin/Ruby 等语言检测）。**不做**：设计模式/框架/API 全量、算法穷举、英文通用动词/设置类消歧、LLM 生成别名。
2. **验收方式**：识别验收集（~250 条 短语→(subject_id, language)）驱动并锁死"识别正确"；命中/拒绝用例 + 全量回归。
3. **架构**：全局本体只收语言无关概念；歧义裸英文容器词走语言表；消歧发生在 residual/指纹前使 subject 别名进入 residual cover 与指纹；Go/kvstore/decision 结构不动。

## 三层识别架构（由宽到严）

```text
1) 全局本体别名（现有 + 新增容器/DS 概念）——语言无关明确概念
2) 变体自动折叠——loader 按规则从 canonical 别名生成，不手写每条
3) 语言受限消歧 lang_terms——parse 全局未命中且语言已知时查表
```

歧义词**不进**全局本体（全局别名仍要求语言无关唯一），杜绝"Python list 命中链表答案"式跨语言误用。

## 组件设计

### ① 本体扩展（容器+数据结构先行，40→约 65）

新增概念（含 canonical_zh/en + 至少 1 中文 + 1 英文别名；以识别验收集实际用到收敛，允许不足则减）：

`double_linked_list`(双向链表)、`circular_linked_list`(环形链表)、`skip_list`(跳表)、`dynamic_array`(动态数组)、`circular_buffer`(环形缓冲)、`deque`(双端队列)、`map`(键值映射)、`set`(集合)、`priority_queue`(优先队列)、`segment_tree`(线段树)、`binary_indexed_tree`(树状数组/Fenwick)、`suffix_array`(后缀数组)、`linked_queue`(链式队列)…（其余在实现期按验收集需求补、超集不鼓励）。

**命名规则**：
- 全局本体只收**语言无关、无歧义**概念。
- **裸英文容器词**（`list`/`vector`/`map`/`dict`/`set`/`string` 等）**不建全局别名**——交给 ③ 语言表；概念自身用更明确的英文别名（如 `dynamic array`、`doubly linked list`），中文别名（`动态数组`/`集合`…）可全局。

### ② 变体自动折叠（loader 生成，非手写 JSON）

对每条 canonical 别名在加载期生成变体并入索引：

- NFKC + 小写 + 空白折叠（→ 规范化文本）
- 剥命名空间前缀：英文 token 前的 `std::`（及任意 `[A-Za-z_]\w*::` 前缀），只留末段
- 剥模板参数：去掉 `<…>`（如 `List<T>`、`std::vector<int>`）
- 字面折叠变体：去空格 / 去连字符 / 去下划线（长度≥3 且互不相同的才保留，避免短词冲突）
- 词干感知比较：匹配时对双端**字母 token（长度≥5）**去尾 `s/es/ing/ed` 后再等值（`linked lists`≈`linked list`；只用于比较不做覆盖）
- **唯一性/歧义校验**：loader 对"原始+折叠变体+词干"整体校验——同一变体命中多个概念 → 启动报错或该词歧义置 None，**不放行误配**。

### ③ 语言受限消歧表 `ontology/lang_terms.json`（新增数据文件）

`{ "cpp": {"list":"linked_list", "vector":"dynamic_array", "deque":"deque",
           "unordered_map":"hash_table", "map":"map", "unordered_set":"set", "set":"set"},
   "python": {"list":"dynamic_array", "dict":"hash_table", "set":"set", "tuple": null},
   "csharp"/"go"/"rust"/"javascript"/"typescript"/"java"/"php"/"swift"/"kotlin"/"ruby"/...: {…} }`

映射目标以验证集为准逐项审定（示例：`java` 的 `list`/`arraylist`/`linkedlist` 属接口层，建议保守 `null`；`cpp map`(有序,红黑树) 与 `unordered_map`(hash) 区分目标）。

**触发与安全判据**：
1. 仅当 parse **语言已识别**且**全局本体未命中**时查表；
2. 词按词界小写匹配（拉丁词界；中文词在对应语言行单列）；
3. 恰一个命中才设 subject_id（并把 subject_text 置为命中词）；多个/冲突 → 保守 None；
4. **无语言 → 裸歧义词不识别** → miss（防跨语言误命中）。

### ④ parse 接入（subject resolve 三步）

解析顺序：
1. 句式抽 subject_text（现有 SUBJECT_PATTERNS 不变）
2. 全局本体（折叠变体索引、词干感知）→ subject_id
3. **第三步（新增）**：subject_id 仍 None 且 language 已知 → 匹配 `lang_terms[lang]`；命中恰一 → subject_id + subject_text 定
4. residual / fingerprint_eligible / fingerprint 在 resolve **之后**计算（covers 含概念别名 → `cpp list` 与 `cpp 链表` 同 subject、同 fp）

涉及改动仅 `semantic`：`ontology/loader.py`（折叠索引 + 校验 + lang_terms 加载）、`ontology/lang_terms.json`、`parse.py`（第三步 + 语言检测扩展）、`parse.py/ontology` 相关测试。Go/kvstore/decision 不动。

### ⑤ 语言检测扩展

在现有 `cpp/csharp/dotnet/javascript/node/typescript/java/python/rust/golang/go/c` 基础上补常见语言：`php/swift/kotlin/ruby`（如需 `lua/objective-c` 一并），与 lang_terms 表配套；长模式优先、ASCII 词界（保空格文本）规则沿用。

## 识别验收集与测试

- 新增 `semantic/tests/eval/lang_terms_cases.py`：~250 条 `(短语 → 期望 subject_id, language)`，覆盖：
  - 各语言容器裸词（cpp list/vector/map/set、python list/dict/set、csharp List、go slice/map、rust Vec、java List→None 等）
  - DS 中英文名与别名及**变体**（大小写/空格/连字符/`std::`/`<T>`/复数/动名词）
  - 负例：无语言歧义词、java List、通用动词撞词等 → 期望 None
- 测试：
  1. 验收集逐条断言 parse 的 subject_id/language
  2. 折叠变体唯一性（扩展 `test_ontology`）
  3. 命中/拒绝用例（决策/fp 级）：`用 C++ 写一个 list`↔`用 cpp 实现链表` 命中；`python 的 list`↔`python 动态数组` 命中；`C++ list`↔`Python list` 拒绝；无语言 `写一个 list` → miss
  4. 全量回归：现有语义测试集（parse/decision/fingerprint/embedding/权威集 129/retrieval 226/e2e 基线）不回归
- 冒烟（可选手工/脚本）：重启 semantic 后 curl /embed 验 `cpp list`→linked_list 且 eligible。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 折叠变体撞其它词/概念 | loader 唯一性校验失败即启动报错或标歧义 None；验收集覆盖 |
| 英文词干误剥（class/ing 等） | 只对长度≥5 的字母 token 去尾 s/es/ing/ed，且仅用于等值比较不覆盖；验收集回归 |
| java List / 无语言歧义误伤 | 显式 None 保守（查表目标逐项审定） |
| 语言表目标选错（如 cpp map vs unordered_map） | 分概念目标 + 验收集条目锁死 |
| 误把"集合/set/设置"等通用词当容器 | 英文 `set`/`map` 等仅语言表；中文集合只在上下文明确时入概念 |
| 性能 | 折叠索引一次性构建；查询 O(词数)；无新增请求往返 |

## 明确不做（Out of scope）

设计模式/框架/API 名称全量；算法穷举；英文通用动词/“设置类”措辞消歧（保持保守 miss）；LLM 生成别名/大规模词库自动挖掘；改 Go/kvstore/decision 结构；改指纹/阈值语义。

## 涉及文件清单

新增：
- `semantic/ontology/lang_terms.json`
- `semantic/tests/eval/lang_terms_cases.py`（识别验收集 + 测试）

修改：
- `semantic/ontology/concepts.json`（容器/DS 概念 40→约 65）
- `semantic/ontology/loader.py`（折叠变体索引 + 词干比较 + 唯一性校验 + lang_terms 加载/查询）
- `semantic/parse.py`（subject 三步 resolve + 语言检测扩展）
- `semantic/tests/test_ontology.py`、`semantic/tests/test_parse.py` 等（折叠唯一性、语言表、验收集、回归）
- `docs/项目文档/04-业务应用.md` / `semantic/README.md`（术语识别说明，若需要）
