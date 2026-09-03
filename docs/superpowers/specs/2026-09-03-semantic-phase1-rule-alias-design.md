# ECHO-CHAT 语义检索 Phase 1：高精度规则与别名体系设计文档

- 日期：2026-09-03
- 状态：已批准（brainstorming 逐段确认）
- 位置：ECHO-CHAT monorepo（`semantic/` 服务内模块化 + Go `semcache.go` 指纹前置）
- 关联：`proj/tmp/ECHO-CHAT语义检索高命中率改造方案.md`（远景）、`proj/tmp/ECHO-CHAT语义服务解耦方案评审与修改建议 (1).md`（评审）、`docs/superpowers/specs/2026-09-03-semantic-service-decouple-design.md`（Phase 0，已完成）

## 背景与目标

Phase 0 已完成服务解耦：语义职责独立在 `semantic/:3003`（/embed /rerank，256 维 FNV 词面嵌入，规则决策），Go `semcache` 指语义服务并做 VSEARCH 检索 + 阈值。**检索效果与拆分前一致**——即仍受词面嵌入天花板限制："红黑树 vs rbtree" 无公共词难命中；语言空值漏洞会让"生成红黑树"误用"C++ 答案"。

Phase 1 目标（**有意的行为变化**，不再保持 Phase 0 的逐字段不变）：
1. 别名归一：`红黑树 / rbtree / RB tree / red black tree` 归一到稳定 `subject_id`，跨措辞可命中；
2. 结构化槽位：语言 / 意图 / 操作 / 输出类型可判定，硬约束 C≠C++、定义≠实现、插入≠删除；
3. 语义指纹精确缓存：同槽位不同措辞直接命中、绕开向量与阈值；
4. 验收矩阵测试集（几十条）取代 Phase-0 金样，作为 Phase 1 权威回归。

## 已确认决策

- **范围（裁剪版）**：别名本体(subject_id)+canonical 嵌入 + 语言/意图/操作/输出类型硬约束 + 语义指纹 + 验收矩阵。framework/version/platform 槽位只留 None 骨架不做规则。
- **决策归属 semantic**：解析/decision 全部在 Python 侧（本体所在）；/rerank 升级返回 `reason`；Go 只加"指纹精确命中"前置与消费新字段，**零 kvstore 格式变更、无数据迁移**。
- **本体规模**：精选手工审核 60~120 条 CS·DS·算法概念，覆盖验收矩阵与常见编码题；中英文/缩写都作 alias。
- **检索顺序**：`① GET semfp:<fp> → ② VSEARCH → ③ decision`（fingerprint 可配置开关）。
- **模块化顺势落地**：`semantic.py` 收敛为薄路由层；纯逻辑拆 `ontology.py / parse.py / embedding.py / decision.py`（离线可测、也为 Phase 2 模型替换铺路）。
- **Phase-0 金样退役**：canonical 嵌入改变 /embed 输出（有意），旧 `test_golden.py`/`golden_cases.json` 不再作 CI 门，标注 legacy。

## 验收矩阵（行为预期，权威回归）

| # | Query A | Query B | 预期 |
|---|---|---|---|
| 1 | 生成一个红黑树 | 生成一个 rbtree | 命中（fp 或召回） |
| 2 | 用 C 语言生成红黑树 | 使用 C 编写 rbtree | 命中（同 fp） |
| 3 | 用 C 语言生成红黑树 | 使用 C++ 编写 rbtree | 拒绝 language_conflict |
| 4 | 红黑树是什么 | what is a red-black tree | 命中（定义类语言不敏感） |
| 5 | 红黑树是什么 | 实现一个 red-black tree | 拒绝 intent_conflict |
| 6 | 用 Python 实现红黑树插入 | 用 Python 实现 rbtree 删除 | 拒绝 operation_conflict |
| 7 | 继续修改上面的红黑树 | 生成红黑树 | 绕过 context_dependent |
| 8 | 生成红黑树（无语言） | 用 C++ 生成红黑树 | 拒绝（实现类敏感） |
| 9 | 用 C 生成红黑树 | 用 C++ 生成 rbtree | 拒绝 language_conflict |
| 10 | 红黑树插入 | rbtree 删除 | 拒绝 operation_conflict |

附加回归子集：Phase-0 期间线上已知能命中/被拒的近似问题对（决策级 match/reject 不回归，非逐字节）。

## 关键事实（已核实，Phase 0 后）

- `semantic/semantic.py` 单文件 265 行 = nuxt 路由 + 全部规则；`/embed` 返回 `{code, embedding[256], bypass_cache, context_dependent, intent, subject}`；`/rerank` 返回 `{code, score, shared}`；`/healthz` GET。
- `ai-chat-service/chat-server/semcache/semcache.go`：`embedText`/`rerank` 走 `DependOn.Semantic.Address`，1.5s 超时；`CacheQuery` = VSEARCH top5 循环（阈值 0.35 + rerank 阈值 0.25 + shared 门）；`CacheWrite` = SET 明文 Q-A + HSET `semcache:<q>` 向量。缓存 key 前缀：`semcache:`（hash 引擎向量）/ 问题原文（array 引擎明文）。
- `dependOn.tokenizer`（计 token）与 `dependOn.semantic`（嵌入/rerank）已分离。
- 主题抽取现状：`SUBJECT_PATTERNS` + `normalize_subject` 产出 subject **文本**（非 ID）；语言冲突现状**只在双方语言都识别且不同**才拒（空值漏洞）；操作冲突现状双方都识别且不同才拒。
- 语义缓存准入现状：`bypass || subject==""` → 不查不写（安全收窄）。
- nuxt 底层 starlette，支持 GET 路由。

## 明确不做（Out of scope）

- framework / version / platform 槽位抽取规则（仅数据结构预留 None）
- 真 Embedding / Sparse / Qdrant / Cross-Encoder（Phase 2/3）
- kvstore 向量记录格式变更、answer 去重（answer_ref）、metadata 落库
- reason 驱动 Go 决策逻辑（v1 仅透传观测）
- 阈值在验证集上的统计校准（Phase 4 数据闭环）
- 大规模词库与自动挖掘别名候选

## 组件设计

### ① 模块拆分（semantic/ 服务内）

```text
semantic/
├── semantic.py        # 薄路由层：/embed /rerank /healthz，只做参数校验+调纯函数+构造响应
├── ontology.py        # CONCEPTS 词库（60~120 条）+ lookup_subject_id()
├── parse.py           # 槽位抽取（subject_text/subject_id/intent/language/operation/output_type）+ build_fingerprint()
├── embedding.py       # embed_text()（canonical 主题桶）
├── decision.py        # decide(query_text, cached_text) → (score, shared, reason)
├── requirements.txt / Dockerfile / README（不变，仍 nuxt+jieba）
└── tests/             # Phase-1 验收矩阵 eval（离线 import 纯模块）
```

关键实现约束：`ontology/parse/embedding/decision` 为**纯函数模块，不 import nuxt**（离线 pytest 可直接 import）；`semantic.py` 顶部 import 它们使路由注册。nuxt 以 `--module semantic.py` 从本目录启动、同目录兄弟模块 import——首步先冒烟验证可行。

### ② 本体 `ontology.py`

```python
# 稳定 ID → canonical + aliases
CONCEPTS = {
    "red_black_tree": {
        "canonical_zh": "红黑树",
        "canonical_en": "red black tree",
        "aliases": ["红黑树", "rbtree", "rb tree", "rb-tree",
                    "red black tree", "red-black tree", "redblacktree"],
    },
    "binary_search_tree": {
        "canonical_zh": "二叉搜索树", "canonical_en": "binary search tree",
        "aliases": ["二叉搜索树", "二叉查找树", "bst", "binary search tree"],
    },
    # ... 覆盖 链表/数组/堆/栈/队列/哈希表/单例/线程池/深拷贝/冒泡/快排/排序/递归/位运算 等 60~120 条
}
```

- 匹配顺序：**精确优先、模糊兜底**。英文/缩写用词边界正则（`\brbtree\b`），避免 `RBAC` 命中 `rb` 之类误伤；中文直接子串/整词。
- 匹配入口为 `extract_subject` 后的 subject 文本；命中返回稳定 ID，未命中返回 None（保留原 subject 文本，不进 canonical/指纹路径）。
- 别名只做**本概念内归一**，不做跨概念全局替换（RBAC 与 rbtree 分离，各查各的）。

### ③ 解析 `parse.py`（槽位 + 指纹）

每段文本解析为结构化槽位；`parse.py` 同时容纳原上下文/状态/语言/操作检测 helper（`is_context_dependent` / `is_stateful_instruction` / `should_bypass_semantic_cache` / `extract_language` / `extract_operation` 等），使 `/embed` 路由仍能回填 `bypass_cache` / `context_dependent` 字段。

| 槽位 | 取值 | 规则来源 |
|---|---|---|
| `subject_text` | 原文主题 | 现有 SUBJECT_PATTERNS + normalize_subject（保留） |
| `subject_id` | red_black_tree / … / None | ontology.lookup_subject_id(subject_text) |
| `intent` | implementation/definition/comparison/operation/reason/troubleshooting/history_query/state_update/unknown | 现有 INTENT_RULES（保留） |
| `language` | c/cpp/python/java/javascript/go/rust/typescript/csharp/…/None | 现有 LANG_PATTERNS + 补强（见下） |
| `operation` | insert/delete/traverse/query/add/update/replace/modify/find/None | OPERATION_WORDS 中文词 → 英文 id 映射 |
| `output_type` | code/explanation/None | 派生：implementation/troubleshooting → code；definition/comparison/reason → explanation；否则 None |
| `parser_version` | v1 | 元信息 |

**语言补强**（方案 §5.4）：`c` 需上下文约束（`c语言` 或 `(?<![a-z0-9+#])c(?![a-z0-9+#])(?=.*(代码|实现|编程))`），防普通字母 c；补 `rust`/`typescript`/`csharp`/`.net`。`c++`、`c#`、`.net` 等符号在早期 NFKC 处理时保留。

**指纹**：仅 `subject_id` 命中时计算：

```text
fp = sha256(subject_id | intent | language | operation | output_type)   # None → ""
```

### ④ canonical 嵌入 `embedding.py`

`embed_text` 唯一实质改动：主题标记桶原文 → 稳定 ID。

```python
sub_id = parse.lookup_subject_id(subject)   # 由 embedding 内部调 parse 得到
if sub_id:
    _add(vec, "SUBJECT:" + sub_id, 3.0)     # 红黑树/rbtree/RB tree 同桶
else:
    _add(vec, "SUBJECT:" + subject, 3.0)    # 未命中退回原文（行为≈现状）
```

意图桶 2.0、普通词 1.0、bigram 0.4、模板词 0 不变。别名措辞与候选共享主题桶 → VSEARCH 余弦抬升。

### ⑤ decision `decision.py`（升级 rerank）

```python
LANGUAGE_SENSITIVE_INTENTS = {"implementation", "troubleshooting", "code_modification", "execution"}

def decide(query, cached):
    """query/cached 为 parse 结果；返回 (score, shared, reason)。
    严格→宽松顺序。"""
    # 1. subject_id 双方都有且不同 → 拒
    #    任一 subject_id 为 None → 退回老 _subject_conflict(文本包含判断)
    # 2. 语言(仅 LANGUAGE_SENSITIVE_INTENTS)：
    #    - 双方都有且不同 → language_conflict
    #    - query 无 & cached 有  → language_conflict
    #    - query 有 & cached 无  → language_conflict
    #    定义等非敏感意图跳过语言
    # 3. 操作：双方都有且不同 → operation_conflict；相同 → 放行(跳过意图)
    # 4. 意图：双方可判定且不同 → intent_conflict；任一方 unknown → 不拒
    # 5. 输出类型：code vs explanation 且未覆盖 → output_conflict
    # 6. 通过 → 关键词 Jaccard 给分，reason="ok"
```

`/rerank` 响应追加 reason（**只加不删**旧字段）：`{code, score, shared, reason}`。reason 集：`subject_conflict / language_conflict / intent_conflict / operation_conflict / output_conflict / unknown_subject / ok`。

### ⑥ Go 侧（最小，决策不动）

- `config.go`：`SemanticCache.ExactFingerprintEnabled bool`（yaml `semantic_cache.exact_fingerprint_enabled`，默认 true）。
- `embedResp` 增 `subject_id/fingerprint/language/operation/output_type`；`rerankResp` 增 `reason`（解析但 v1 只透传观测）。
- `embedText` 返回值收敛为小 meta（vec + bypass + subject + fingerprint + slots），`CacheQuery/CacheWrite` 包内配套。
- `CacheQuery` 顺序：
  1. `embedText`；`bypass || subject==""` → miss（原准入不变）；
  2. `ExactFingerprintEnabled && fp!=""` → `GET semfp:<fp>`，命中直接返回（不走 VSEARCH/阈值）；
  3. 否则 VSEARCH 循环（decision 门不变，`shared` 语义不变）。
- `CacheWrite`：现 SET/HSET 之外，若 `fp!="" && enabled` 追加 `SET semfp:<fp> = <answer>`。键前缀 `semfp:`（array 明文，与 Q-A 无冲突）。
- dev.config.yaml 与 ai-chat-stack 配置各加 `exact_fingerprint_enabled: true`。

## 兼容与数据

- /embed、/rerank 为**增字段**变更：旧 Go/旧客户端只读已知字段仍可用；旧缓存条目无 fp → 仅走 VSEARCH（行为=现状），新条目先走 fp。新旧混存无需迁移。
- Phase-0 `semcache:*/GET 问题原文` 数据结构、`encodeVec` 二进制、kvstore VSEARCH 全部不变。

## 测试与评估

`semantic/tests/` 改造（离线 import 纯模块）：

1. **验收矩阵**（上表 10 行 + 老案例决策级子集 + 额外负样本）对 `decision(q,c)`：
   - match 行 `shared=True`；其中 fp 覆盖行断言 `build_fingerprint(q)==build_fingerprint(c)`
   - reject 行 `shared=False` 且 `reason` == 期望码
   - bypass 行 `should_bypass(text)==True`
2. **别名召回**：`embedding` 上断言 `cosine(v("生成一个红黑树"), v("生成一个 rbtree"))` 明显高于阈值下限（证 canonical 桶生效）。
3. **路由冒烟**（起 semantic:3003 后）：`/embed` 新字段存在、`/rerank` reason 存在、`/healthz` 200。
4. **Go 编译**：`go build ./...`。
5. **e2e 子集**（写缓存后跨别名重问命中；C++ 变体拒绝）——脚本/手工均可。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| canonical 嵌入扰动既有命中分布 | 验收矩阵 + 老案例"决策级 match/reject 不回归"子集兜底 |
| 语言严格化牺牲无语言泛化命中（精度换召回） | 以验收矩阵为准；若召回不足，后续加 config 放行口（如仅未知候选语言且查询无语言才放行），本轮不做 |
| 指纹误命中 | fp 仅含 5 槽位、要求 subject_id 必存在；同槽位不同实质请求概率低；扩展留后 |
| nuxt 同目录兄弟模块 import 不可用 | 实施首步先冒烟；失败则回退为"单一 semantic.py + 顶层 import 子文件函数"，仍满足离线测试（纯函数模块仍可被 tests import） |
| 本体词库质量（错别名→错归一） | 词条手工审核；缩写匹配限词边界；低置信不归一、subject_id=None 走保守老路径 |

## 涉及文件清单

新增：

- `semantic/ontology.py`
- `semantic/parse.py`
- `semantic/embedding.py`
- `semantic/decision.py`
- `semantic/tests/eval/`（验收矩阵数据 + pytest：`test_decision.py`/`test_ontology.py`/`test_embedding.py`/`test_parse.py`）

修改：

- `semantic/semantic.py`（拆薄路由层 + 增字段）
- `ai-chat-service/pkg/config/config.go`（ExactFingerprintEnabled）
- `ai-chat-service/chat-server/semcache/semcache.go`（指纹前置 + meta）
- `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`（exact_fingerprint_enabled）
- `docs/项目文档/04-业务应用.md`（§4.2/§6 语义能力描述更新到 Phase 1）
- `semantic/tests/`（旧金样标注 legacy）
