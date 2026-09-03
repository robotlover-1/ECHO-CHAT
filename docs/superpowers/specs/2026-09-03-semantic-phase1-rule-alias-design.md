# ECHO-CHAT 语义检索 Phase 1：高精度规则与别名体系设计文档

- 日期：2026-09-03
- 状态：已批准（吸收评审意见修订后）
- 位置：ECHO-CHAT monorepo（`semantic/` 服务内模块化 + Go `semcache.go` 指纹候选前置 + Top-K 提升）
- 关联：`proj/tmp/ECHO-CHAT语义检索高命中率改造方案.md`（远景）、`proj/tmp/ECHO-CHAT语义服务解耦方案评审与修改建议 (1).md`、`proj/tmp/ECHO-CHAT语义检索Phase1规则与别名方案评审.md`（本设计评审，有条件批准）、`docs/superpowers/specs/2026-09-03-semantic-service-decouple-design.md`（Phase 0，已完成）

## 背景与目标

Phase 0 完成服务解耦：`semantic/:3003` 独立承担 /embed /rerank，Go `semcache` 做 VSEARCH+阈值。检索效果与拆分前一致——仍受词面嵌入天花板限制（"红黑树 vs rbtree" 难命中；语言空值漏洞）。Phase 1 目标（**有意的行为变化**）：

1. 别名归一：红黑树 / rbtree / RB tree / red black tree → 稳定 `subject_id`，跨措辞命中；
2. 结构化槽位：subject_id/intent/language/operation/output_type + **residual/constraint**，硬约束 C≠C++、定义≠实现、插入≠删除、带约束实现 ≠ 无约束实现；
3. **安全语义指纹**：粗指纹定位候选 + decision/残差复核，**指纹不直达答案**；
4. 验收/权威测试集（≥120 对）取代 Phase-0 金样。

## 评审修订记录（2026-09-03）

| # | 评审意见 | 处置 |
|---|---|---|
| 阻断1 | 指纹字段不足、`fp→answer` 绕过 decision 有碰撞误命中风险 | 采纳：指纹**只存候选问题**、命中后重跑 decision+残差复核；`fingerprint_eligible` 保守准入（§指纹）；新增残差约束与 `constraint_conflict`（§decision） |
| 阻断2 | 现有 extract_subject 覆盖不了英文与"使用/编写"句式，验收矩阵不保 | 采纳：中英文句式补全 + **双路抽取**（全句本体最长匹配 ‖ 句式抽 subject→本体） |
| 阻断3 | 别名模糊匹配边界不明 | 采纳：Phase 1 仅确定性匹配（NFKC+词边界+中文受控子串+最长优先+alias 全局唯一校验）；模糊留后仅作召回 |
| 阻断4 | decision 方向性/缺失槽位/unknown/包含关系过宽 | 采纳：language 敏感性**双方对称**（含 output_type=code）；operation **任一侧不同即拒**；unknown 不进指纹、与代码型不对称默认拒；有 subject_id 时仅 ID 相等放行 |
| 阻断5 | 指纹未版本化、拼接不稳定 | 采纳：稳定 JSON 序列化 + schema/parser/ontology 版本入 payload；键命名空间 `semfp:v1:` |
| 阻断6 | 写入一致性/覆盖/TTL/升级失效未定义 | 采纳：写入顺序与幂等、fp 冲突 last-write-wins、删除/TTL/升级失效策略（§数据一致性） |
| §14 | reason 不能只解析不记录 | 采纳（轻量）：Go 对 fp 命中/碰撞/回退与 decision 关键结果打标签日志；Prometheus 指标延后到 metrics-bus 集成（defer，注明） |
| §13 | 验收集 100~300 对、Top-5→Top-30、Recall@K | 采纳：权威集目标 ≥120 对起（分类见 §测试），VSEARCH Top-K 配置化默认 30；Recalc@K 脚本 |
| 建议 | 先 20-30 概念起步再扩 | 折中：首批 ≥40 核心概念（覆盖矩阵+常见 CS·DS·算法），词库可扩；不追求 60-120 一版到位 |

## 已确认决策

- 决策/解析全部在 semantic（本体所在）；Go 最小改动（fp 候选前置、Top-K、reason 日志），**零 kvstore 格式变更、无迁移**。
- 检索顺序：`normalize → 解析 → 准入/bypass → ① fp 候选(decision 复核) → ② VSEARCH Top-30 → decision → 命中/miss`。
- **指纹安全红线**：`semfp` 存候选问题文本，不存答案；任何命中前必过 `decide()` + 残差复核。
- 模块化：`semantic.py` 薄路由；`ontology/`、`parse.py`、`embedding.py`、`decision.py` 纯函数（离线 pytest 可测）。
- 本体为**数据文件**（`ontology/concepts.json`）+ loader/validator，非硬编码。
- Phase-0 金样退役标注 legacy（canonical 嵌入有意改变输出）。

## 验收矩阵（权威回归，行为预期）

### 必须命中

| # | Query A | Query B | 期望路径 |
|---|---|---|---|
| M1 | 生成一个红黑树 | 生成一个 rbtree | 同 fp 候选→decision |
| M2 | 用 C 语言生成红黑树 | 使用 C 编写 rbtree | 同 fp（中文"使用/编写"句式覆盖） |
| M3 | 红黑树是什么 | what is a red-black tree | 双路 subject 抽取 + definition 语言不敏感 |
| M4 | 用 Python 实现红黑树插入 | implement RB-tree insertion in Python | 槽位全等（英文 op/intent 归一） |
| M5 | 红黑树的插入 | 写一个红黑树的插入 | op 同=insert → 放行（老兼容案例） |

### 必须拒绝

| # | Query A | Query B | reason |
|---|---|---|---|
| R1 | C 实现红黑树 | C++ 实现 rbtree | language_conflict |
| R2 | 生成红黑树（无语言） | 用 C++ 生成红黑树 | language_conflict（实现类敏感、对称） |
| R3 | 红黑树是什么 | 实现 red-black tree | intent_conflict |
| R4 | Python 实现红黑树插入 | Python 实现 rbtree 删除 | operation_conflict |
| R5 | 红黑树插入 | rbtree 删除 | operation_conflict |
| R6 | 完整实现红黑树 | 只实现红黑树插入 | operation_conflict（一方 op=insert 一方 None） |
| R7 | 实现线程安全的 C++ 红黑树 | 实现持久化的 C++ 红黑树 | constraint_conflict / 非 eligible |
| R8 | 实现一个带父指针的红黑树 | 实现一个支持重复键的红黑树 | constraint_conflict / 非 eligible |

### 必须绕过（不查不写全局缓存）

| # | 文本 | reason |
|---|---|---|
| B1 | 继续修改上面的红黑树 | context_dependent |
| B2 | 把刚才的 C++ 版本改成 C | context_dependent |
| B3 | 记住以后都使用 Rust | stateful_instruction |

### 本体/识别边界（防误伤）

| # | 断言 |
|---|---|
| E1 | tree ≠ binary tree ≠ binary search tree ≠ red-black tree（subject_id 只等号放行） |
| E2 | RBAC 不得命中 rbtree（词边界+全局唯一 alias） |
| E3 | JavaScript 不得识别为 Java；C++ 不得识别为 C；C# 不得识别为 C（长模式先匹配） |
| E4 | 无约束查询 不得复用 带约束候选（residual 不等 → 拒） |
| E5 | 指纹碰撞安全：§R7/R8 五变体间互不共享答案 |

## 明确不做（Out of scope）

- framework/version/platform 槽位规则（仅 None 骨架）
- 真 Embedding/Sparse/Qdrant/Cross-Encoder（Phase 2+）
- kvstore 向量记录格式变更、answer_ref 去重、metadata 落库、原子事务（kvstore 无 multi；用确定性顺序+幂等+best-effort）
- Prometheus 指标接入（metrics-bus 集成延后；本阶段 reason 走标签日志）
- 拼写纠错/编辑距离模糊别名（仅作召回候选，不定 subject_id）
- 大规模词库与自动挖掘候选别名

## 组件设计

### ① 模块结构

```text
semantic/
├── semantic.py            # 薄路由层：/embed /rerank /healthz
├── ontology/
│   ├── concepts.json      # 本体数据（首批 ≥40 概念）
│   ├── loader.py          # 读 JSON → 倒排 alias→concept + 规范检查
│   └── validator.py       # 唯一性/安全断言（import 即校验）
├── parse.py               # 槽位抽取 + 双路 subject + residual + build_fingerprint
├── embedding.py           # embed_text（canonical 主题桶）
├── decision.py            # decide(query, candidate) → (score, shared, reason)
├── requirements.txt / Dockerfile / README（不变）
└── tests/
    ├── eval/              # 权威矩阵数据 fixtures（M/R/B/E）
    ├── test_ontology.py test_parse.py test_embedding.py test_decision.py test_fingerprint.py
    └── test_golden.py     # Phase-0 产物，标注 legacy 不进 CI
```

纯逻辑模块不 import nuxt；`semantic.py` 顶部 import 使路由注册。首步冒烟验证 nuxt 同目录兄弟 import；失败回退方案见 §风险。

### ② 本体 `ontology/`

`concepts.json`（首批 ≥40，覆盖矩阵与常见 CS·DS·算法）：

```json
{
  "schema": "v1",
  "ontology_version": "2026-09-03.1",
  "concepts": [
    {"id": "red_black_tree", "canonical_zh": "红黑树", "canonical_en": "red black tree",
     "aliases": ["红黑树", "rbtree", "rb tree", "rb-tree", "red black tree", "red-black tree", "redblacktree"]},
    {"id": "binary_search_tree", "canonical_zh": "二叉搜索树", "canonical_en": "binary search tree",
     "aliases": ["二叉搜索树", "二叉查找树", "bst", "binary search tree"]}
  ]
}
```

`validator.py`（导入即断言，失败即报错）：
- concept id 全局唯一、canonical 字段存在、alias 非空
- alias 经规范化（NFKC+小写+去空白）后**全局唯一**（`alias_owner_count==1`）
- 禁止危险单字符英文 alias（排除已建档的语言名、停用词边界词）
- alias 最长优先匹配；不做字符串包含放行（tree/binary tree 各自是独立概念，仅 ID 相等兼容）

`loader.py`：构建全小写规范化 alias→concept 倒排；`lookup_subject_id(text)` 用**最长 alias 词边界匹配**（latin 用 `\b`/前后非字母数字；中文受控子串），返回最长的命中 concept id，命中多个同长→拒绝（保守）。

### ③ 解析 `parse.py`

`normalize()`：NFKC、全角转半角、英文小写、统一空格/标点、保留 `c++/c#/.net`。

**双路主题抽取**（评审阻断2）：
- 路径 A（句式）：中英文 SUBJECT_PATTERNS（新增 `使用/编写/给我一个…L 版本…`、`what is/implement/write/build/create X`、`implement X in L`、`L implementation of X` 等）→ subject_text
- 路径 B（本体直取）：`lookup_subject_id(full_text)` 全句最长匹配
- 合并：`subject_id = lookup(subject_text) or lookup(full_text)`；均未命中 → subject_id=None（保留 subject_text 供日志，不进指纹）

**意图中英文规则补全**（definition/implementation/comparison/troubleshooting 的 what is/define/implement/write/build/code/compare/vs/error/exception 等），其余规则保留。语言识别按**长模式优先**顺序：`c++/cxx/cpp` → `c#/csharp/.net` → `javascript/node.js` → `typescript` → `java` → `python` → `rust` → `go/golang` → `c`(需上下文词)；`c` 短词仅 `c语言` 或紧跟实现语境才判。操作中英归一：insert/delete/traverse/find/update…（`remove` 视主题与上下文，谨慎）。

**output_type**：implementation/troubleshooting → code；definition/comparison/reason → explanation；operation/unknown → None。

**residual 残差**：取 normalize 后 tokens（jieba）+ latin token 集，减去：subject alias 覆盖词、停用词、intent 命中词、operation 命中词、语言名 → 得 `residual_words`。用于指纹准入与 decision 复核。

**fingerprint_eligible**（保守准入，全真才 True）：
- subject_id 非空；intent 已知且非 unknown；language 无歧义（最多一个语言命中）
- `residual_words` 为空（无 线程安全/持久化/重复键/父指针/无递归 等未解析约束）
- 非 bypass/stateful

**build_fingerprint**：payload 为固定字段顺序的稳定 JSON：

```json
{"schema":"v1","ontology_version":"2026-09-03.1","parser_version":"v1",
 "subject_id":"red_black_tree","intent":"implementation","language":"c",
 "operation":null,"output_type":"code","residual":[]}
```

序列化用 `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",",":"))`，`sha256(utf-8).hexdigest()`。键命名空间 `semfp:v1:`。任何 schema/parser/ontology/词库变更 → payload 中版本字段变化 → 旧指纹自然失效（旧键成孤儿，可后续清理，不影响正确性）。

### ④ canonical 嵌入 `embedding.py`

`embed_text`：若 `subject_id` 命中 → 主题桶用 `SUBJECT:<subject_id>`(3.0)；否则退 `SUBJECT:<subject_text>`(3.0，≈现状)。意图桶/普通词/bigram/模板词权重不变。

### ⑤ decision `decision.py`（严格→宽松，对称）

```python
def decide(query, cached):  # 两者皆为 parse 结果
    # 1. subject_id 双方都有 → 仅相等放行，不等拒 subject_conflict
    #    任一侧无 subject_id → 保守：仅两侧 normalized subject_text 相等才放行，否则拒
    # 2. language_sensitive = (任一侧 intent ∈ {implementation,troubleshooting,code_modification,execution})
    #                         或 任一侧 output_type == "code"
    #    敏感时语言对称严格：query.lang != cached.lang → 拒 language_conflict
    #    （None 不是通配：generic==generic 放行；None vs c/cpp 等一律拒）
    #    非敏感 → 跳过语言
    # 3. operation 保守：query.op != cached.op → 拒 operation_conflict
    #    （含 一方 None 一方有值：完整实现 vs 只插入 拒）
    #    op 同值且非 None → 放行（跳过意图，兼容"X的删除 vs 写X的删除"）
    #    op 双方 None → 进意图
    # 4. intent：双方已知 → 相等放行，不等拒 intent_conflict
    #    一侧 unknown：另一侧代码型 → 拒；另一侧非代码型 → 拒（保守，不进快捷命中）
    #    两侧 unknown → 仅允许向量低分路径，不可 fp/快捷（此处返回 shared=False 由上层继续向量）
    # 5. residual 复核（评审阻断1）：query.residual != cached.residual → 拒 constraint_conflict
    #    （无约束查询 不能复用 带约束候选；反之亦然）
    # 6. 通过 → 关键词 Jaccard 给分；shared=True, reason="ok"
```

`/rerank` 响应追加 reason（只加不删）：`{code, score, shared, reason}`。reason 枚举：`subject_conflict/language_conflict/intent_conflict/operation_conflict/output_conflict/constraint_conflict/unknown_subject/unknown_intent/ok`。

### ⑥ 语义指纹安全落地（评审阻断1/5/6）

**存储语义**：`semfp:v1:<fp>` → **候选问题原文**（array 明文，value 为该缓存问题的 query 文本），不是答案。

**CacheQuery 新顺序**：
1. `/embed` 得 vec + bypass + subject_id + fingerprint + fingerprint_eligible；
2. `bypass || subject_text==""` → miss（准入同 Phase-0；`subject_id==None` **不禁查**，只是禁 fp——未入词库的主题降级走 VSEARCH+decision 保守路径，避免 KMP 等未建档概念整条被砍）；
3. `fp 命中`：`GET semfp:v1:<fp>` 得候选问题 cq → `GET <cq>` 得有答案（无则试 VSEARCH）→ **`decide(query_parsed, parse(cq))` 复核** shared 且无残差冲突 → 返回答案，`source=fingerprint`；复核不过 → 记 collision 日志 → 落入 VSEARCH；
4. VSEARCH Top-K（默认 30，见 §Go）逐候选：阈值 + `decide`（shared + reason）→ 命中返回；拒绝记 reason；
5. 未命中 → miss。

**CacheWrite 新顺序（幂等、best-effort）**：
1. `SET <query> <answer>`（answer 事实源）失败 → 直接返回 err，不写后续；
2. `HSET semcache:<query> <vec>`；
3. `fp 可写`（eligible 且 enabled）→ `SET semfp:v1:<fp> <query>`（last-write-wins；同 fp 等价候选语义相同，覆盖无害）。任一步失败仅记日志，不级联回滚（缺失向量/指纹只损失该条召回，不损坏其它；符合保守 miss）。

**更新/删除/TTL/升级**：
- 同一原问题新答案 → CacheWrite 覆盖（幂等）。
- 同 fp 多问题 → last-write-wins；因 eligible 需 residual 为空，碰撞仅剩真等价措辞，可接受。
- 删除：本期无删除 API；fp/向量孤儿键不影响正确性（查询按前缀精确取），后续清理任务处理。
- TTL：语义缓存维持现状不设（与 Phase-0 一致）。
- 升级：版本字段入 payload → 新 fp 不同，旧 `semfp:v1:*` 成孤儿；本期不迁移、不改 kvstore 记录格式。

### ⑦ Go 侧改动

- config.go：`SemanticCache` 增 `ExactFingerprintEnabled bool`（`exact_fingerprint_enabled`）与 `TopK int`（`top_k`，默认 30）。yaml（dev + ai-chat-stack 配置）同步。
- `embedResp` 增 `subject_id/language/operation/output_type/fingerprint/fingerprint_eligible`；`rerankResp` 增 `reason`。只加不删。
- `embedText` 返回值收敛为小 meta（vec/bypass/subject/subject_id/fp/eligible/slots），`CacheQuery/CacheWrite` 包内配套。
- `CacheQuery`：按 §⑥ 顺序；VSEARCH Top-K 用配置（默认 30），逐候选 `GET`+`rerank`（decision）+ reason 判断。
- 日志（评审 §14 轻量）：`fingerprint_hit / fingerprint_collision / fingerprint_fallback`、decision reason 走包级 `log.*F` 标签，不记用户原文/答案正文；Prometheus 指标延后。
- `services/tokenizer`（GetTokens）与 server.go 调用点不动。

## 数据一致性定义（评审阻断6）

| 问题 | 决策 |
|---|---|
| 写入顺序 | ① SET answer ② HSET vec ③ SET semfp（answer 失败即停） |
| 失败回滚 | 无事务；best-effort，缺失索引只损失该条召回，保守 miss 兜底 |
| 重试幂等 | SET 覆盖幂等 |
| 事实源 | `<query>` 明文 answer 是唯一事实源；fp/vec 皆引用 |
| 指纹冲突 | last-write-wins；eligible 限 residual 空使冲突≈真等价 |
| 删除/TTL/升级 | 本期无删除/无 TTL；升级靠版本命名空间隔离旧键 |

## 测试与评估

semantic/tests 全部离线 import 纯模块（pytest），权威数据放 `tests/eval/`：

1. **权威集 ≥120 对**（fixtures，分类见下），对 `decide(q,c)` 断言 shared 与 reason，对 bypass 断言绕过：

   | 类别 | ≥ |
   |---|---|
   | 同主题同意图正样本（含 alias/中英改写） | 40 |
   | 语言冲突负样本 | 20 |
   | 意图冲突负样本 | 15 |
   | 操作/范围冲突负样本 | 15 |
   | 约束/残差冲突负样本 | 15 |
   | 上下文/状态绕过 | 10 |
   | 本体边界与缩写误伤（tree/bst/RBAC/Java↔JS/C++↔C） | 15 |

2. **矩阵回归**：验收矩阵 M/R/B/E 全部逐行断言（含 R7/R8 指纹碰撞安全：断言五变体 fp 不同 或 非 eligible，且 decision 互拒）。
3. **召回**：`embedding` 别名对余弦断言（M1/M2 余弦 > 0.5 级下界）；离线 Recall@K（VSEARCH 不可离线，用向量余弦 top-K 近似）覆盖 M1-M5。
4. **路由冒烟**：起 :3003 后 /embed 新字段、/rerank reason、/healthz 200。
5. **Go 编译** `go build ./...`；**e2e**：写缓存后跨别名重问命中（source=fingerprint）、C++ 变体拒绝、带约束五变体互不误命中、semantic 停服降级。
6. 指标：语言/意图/操作/约束冲突 false hit 全 0；正样本召回 ≥ 90%（权威集内）；Phase-0 已知正确用例决策级不回归。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| canonical 嵌入扰动既有分布 | 权威集 + Phase-0 已知对"决策级"回归子集 |
| 语言/操作严格化牺牲召回 | 保守优先（miss 只重调 LLM）；验证集校准在 Phase 4 放宽 |
| 指纹碰撞 | 保守准入(residual 空)+候选复核+版本隔离；碰撞有日志并可测 |
| 双路匹配误伤 | alias 全局唯一 + 最长优先 + 边界匹配；E1-E5 回归 |
| nuxt 兄弟模块 import 不可用 | 首步冒烟；失败回退：纯函数仍独立文件（tests 可直接 import），semantic.py 内 `from .` 改同目录绝对 import 验证 |
| 解析规则误判英文/未知 | 意图/句式中文案补全 + unknown 保守不进快捷命中；误判只致 miss 不致远命中 |

## 涉及文件清单

新增：

- `semantic/ontology/concepts.json`、`semantic/ontology/loader.py`、`semantic/ontology/validator.py`
- `semantic/parse.py`、`semantic/embedding.py`、`semantic/decision.py`
- `semantic/tests/eval/`（矩阵+权威集 fixtures）、`semantic/tests/test_{ontology,parse,embedding,decision,fingerprint}.py`

修改：

- `semantic/semantic.py`（薄路由 + 增字段）
- `ai-chat-service/pkg/config/config.go`（ExactFingerprintEnabled/TopK）
- `ai-chat-service/chat-server/semcache/semcache.go`（fp 候选前置 + decision reason + Top-K）
- `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`（exact_fingerprint_enabled/top_k）
- `docs/项目文档/04-业务应用.md`（§4.2/§6 语义能力更新到 Phase 1）
- `semantic/tests/test_golden.py`（标注 legacy）
