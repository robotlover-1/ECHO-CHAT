# ECHO-CHAT 语义检索 Phase 2+3（合并）：E5 Dense 语义检索与结构化规则融合 设计文档

- 日期：2026-09-04
- 状态：已批准（吸收评审 P0/P1 修订后）
- 位置：ECHO-CHAT（`semantic/` 模型封装 + kvstore 子模块 VSEARCH 前缀参数 + Go semcache 重构为"一次编码/纯规则决策/复用 VSEARCH 余弦"）
- 关联：`proj/tmp/ECHO-CHAT语义检索高命中率改造方案.md`（远景）、`proj/tmp/ECHO-CHAT语义检索Phase2-3合并方案评审.md`（本设计评审，有条件批准）、`docs/superpowers/specs/2026-09-03-semantic-phase1-rule-alias-design.md`（Phase 1，已完成）

> **命名澄清**：本文的"融合检索"指 **语义指纹 + Dense(E5) 向量召回 + 结构化规则硬门** 三层融合；**不含** Dense+Sparse(BM25) 混合检索，也不含独立 Cross-Encoder 重排（均为显式 Out of scope）。

## 背景与目标

Phase 0/1 后向量层仍是 256 维 FNV 词面哈希。本阶段用真实语义模型 **multilingual-e5-small（ONNX/INT8、免 torch runtime、384 维）** 整体替换词面向量，作为唯一"语义向量 + 排序分数"来源；Phase-1 的本体/槽位/指纹/**纯规则硬门**原样保留，负责确定性精度。目标是 CPU-only(4 核/7GB) 下提升跨语言/超本体同义改写召回，**不牺牲区分度**，且决策层不再为每个候选跑模型。

## 评审修订记录（2026-09-04）

| # | 评审项 | 处置 |
|---|---|---|
| P0-3.1/3.3 | 逐候选重复 E5、Query 重复编码 30 次 | 采纳：**Query 每请求只编码一次**；decision 拆为**纯规则 hard_decide**；线上复用 VSEARCH 返回的余弦；Go 用批量规则决策接口，不再逐候选 /rerank 跑模型 |
| P0-3.2 | /rerank 分数与 VSEARCH 同源、无新信息 | 采纳：模型分数由 VSEARCH 提供；decision 不再产语义分（§决策接口） |
| P0-4 | 前缀职责矛盾 `query: query:` | 采纳：前缀职责收进 models（`encode_query/encode_passage`），调用方传 raw，业务层禁拼前缀；双前缀自剥 |
| P0-5 | `semd:` 无模型/导出版本隔离 | 采纳：命名空间 `semd:e5s:v1:`；`/model-info` 供 Go 启动一致性校验，不一致禁用向量缓存 |
| P0-6.1 | 极值定阈值过拟合 | 采纳：开发/校准/独立测试三份、按主题分组切分、单一 `acceptance_threshold`、bootstrap CI、测试集只报一次 |
| P0-6.2 | 双阈值同源重叠 | 采纳：VSEARCH 不设高阈值(靠 Top-K)；最终仅 `acceptance_threshold` + `min_margin` |
| P0-12 | VSEARCH 前缀缺安全校验 | 采纳：参数白名单校验（§kvstore） |
| P1-7 | soft residual 相等挡真改写 | 采纳：soft 通道按 `critical_constraints` 判定（关键约束清单），软残差交给 E5 |
| P1-7.4 | reason 应区分来源 | 采纳：soft 命中 `reason="semantic_soft_match"`（新增枚举，Go 增量兼容） |
| P1-8 | 池化/一致性/模型清单 | 采纳：attention-mask 平均池化（代码给出）；PT/ONNX-FP32/INT8 一致性测试；模型清单含 checksum |
| P1-9 | Worker/线程需实测 | 采纳：`semantic_runtime` 配置(workers/intra_op/inter_op/warmup)；压测表；默认 workers=1/intra_op=4 起步 |
| P1-10 | 健康检查语义 | 采纳：`/healthz`(liveness, status ok) 与 `/readyz`(含模型加载+warmup+维度/范数) 拆分；启动预加载+warmup |
| P1-11 | 迁移覆盖率/灰度 | 采纳：指纹命中后**异步 E5 回填**（幂等/限速/不阻塞）；`vector_read_mode/write_mode` 灰度 |
| §13 | “混合检索”命名 | 采纳：改“E5 Dense + 指纹 + 规则硬门 融合检索”并加澄清 |

## 已确认决策（用户逐段 + 评审修订后）

1. 模型 multilingual-e5-small(384)，ONNX INT8，CPU EP；runtime 依赖仅 onnxruntime/tokenizers/numpy（torch 只用于一次性导出）。
2. Dockerfile 基座 alpine→`python:3.10-slim`(glibc)；本地 ubuntu py3.8 与镜像同套依赖；模型文件随镜像 COPY；本地验证、镜像不构建。
3. kvstore `VSEARCH` 加**可选前缀参数**（默认 `semcache:`），e5 写 `semd:e5s:v1:`；旧 256 不再写入；重问驱动 + 指纹命中异步回填迁移；向量读写模式可灰度。
4. 前缀对称性：默认 Query/Query（缓存的是历史问题），写入与查询必须一致；预留 passage 模式，在验证集上对比后锁定。
5. 检索质量用**开发/校准/测试**分集与单一阈值。
6. "无本体语义兜底" soft 通道**默认关**；启用需校准集 Precision≥99% 且独立测试通过。
7. 命名空间含模型/导出短版本 `semd:e5s:v1:`，配置与 /model-info 一致校验。

## 目标与验收要点

**性能目标**（作为待测量目标，非预设结论）：完整 CacheQuery p95 ≤ 500ms（e5 短文本 INT8 ~10-60ms、Query 只编一次、规则批量决策无模型）。压测组合（workers×intra_op×inter_op、长度、并发 1/5/10/20/50、冷/热启动）后落定默认值。

**验收要点**：
1. 模型正确性：完整 ID/revision 固定；前缀只加一次；attention-mask 平均池化；输出 384；范数≈1；无 NaN/Inf；PT/FP32/INT8 排序一致且 INT8 临界翻转可接受；模型清单+checksum。
2. 决策零回归：Phase-1 权威集 reject 全拒、match 在纯规则下 shared 且 VSEARCH 余弦 ≥ acceptance_threshold。
3. 新增泛化集（无本体同义改写 + 关键约束正负）进入校准与测试；soft 默认关。
4. 功能：红黑树↔red-black tree/rbtree 命中；C↔C++ 永不高余弦放行；指纹候选仍复核；namespace 内才可比；Top1-Top2 过近 miss。
5. Go/semantic namespace 或 dimension 不一致 → 禁用语义缓存并报错、聊天走 LLM。
6. 故障降级：模型缺失/校验和错/加载失败 → readyz 失败、不写错误 namespace、缓存 miss、聊天不断。

## 明确不做（Out of scope）

BM25/Sparse；Cross-Encoder 重排；Qdrant/外部向量库；passage/query 不对称（除非验证集显示显著更优再改，且写查保持一致）；模型微调；reason 驱动 Go 决策逻辑；Prometheus 指标（标签日志）；全量一次性离线迁移（不暴露危险的全库扫描接口）。

## 组件设计

### ① 模型封装 `semantic/models.py`（唯一前缀职责）

```python
MODEL_ID = "intfloat/multilingual-e5-small"
REVISION = "<固定 commit SHA>"
EXPORT_VERSION = "onnx-int8-v1"
DIMENSION = 384
MAX_LENGTH = 512
NAMESPACE = "semd:e5s:v1:"
_KNOWN_PREFIX = ("query: ", "passage: ")

def _strip_prefix(text):
    for p in _KNOWN_PREFIX:
        if text.startswith(p):
            return text[len(p):]
    return text

def _encode(text):
    s = _session()  # 懒加载单例 InferenceSession(INT8, intra_op/inter_op 来自配置)
    tok = _tokenizer()
    enc = tok(text, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="np")
    feeds = {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"]}
    if "token_type_ids" in s.get_inputs():
        feeds["token_type_ids"] = enc.get("token_type_ids", np.zeros_like(enc["input_ids"]))
    out = s.run(None, feeds)[0]                     # [1, seq, hidden]
    last, mask = out, enc["attention_mask"]
    m = mask[..., None].astype("float32")
    summed = (last * m).sum(axis=1)
    counts = np.clip(m.sum(axis=1, keepdims=True), 1e-9, None)
    vec = summed / counts
    norm = np.linalg.norm(vec, axis=1, keepdims=True)
    return (vec / np.clip(norm, 1e-12, None))[0].astype("float32")

def encode_query(text) -> list[float]:  # 业务层禁止手工拼前缀
    return _encode("query: " + _strip_prefix(text))

def encode_passage(text) -> list[float]:
    return _encode("passage: " + _strip_prefix(text))
```

- 懒加载 + **启动预加载与 warmup**（首条固定短文本）；`/readyz` 复用同一次 warmup 结果。
- ORT 线程经 `SessionOptions.intra_op_num_threads/inter_op_num_threads` 取配置。
- `model-info()` 返回 `{model, revision, dimension, vector_namespace, export_version}`。
- 模型目录 `semantic/models/e5s-v1/`（gitignore）：`.onnx`、`tokenizer.json`、`config.json`、`special_tokens_map.json`、`MANIFEST.json`（含 `upstream_model/upstream_revision/export_tool_version/onnxruntime_version/quantization_config/model_sha256/tokenizer_sha256/export_timestamp`）。启动读取 MANIFEST 校验 sha256。

### ② `embedding.py`

`embed_text(text)` = `models.encode_query(text)`（对称 Query 前缀）。FNV/桶逻辑删除。**写/查一致性**：存储与查询都用 encode_query（语料为历史问题）；保留 passage 函数备用，若要改 passage 须先在验证集对比并保持一致。

### ③ 决策层：纯规则 hard_decide（不含模型）+ 分数归 VSEARCH

- `semantic/decision.py` 拆出纯规则 `hard_decide(qp, cp) -> (shared, reason, soft_flag)`，**不跑模型、不给语义分**。规则/顺序与 Phase-1 decide 一致（subject/language_sensitive/operation/intent/critical-residual），但：
  - **签名迁移**：Phase-1 的 `decide()` 现返回语义余弦 score——迁移后**不再返回模型分**；凡断言"ok 分支余弦≥阈值"的旧测试（test_decision/test_eval 的 accept 谓词）改为**检索级断言**（用 `models.encode_query` 对 q/c 算余弦作 vector_score 代理），decision 单元只断 shared/reason/soft。`/rerank` 保留为兼容入口但 Go 不再调用。
  - reason 新增 `semantic_soft_match`（soft 通道命中专用）。
  - **critical_residual 判定**：Phase-1 的 `residual_words` 保留用于普通路径的 `constraint_conflict`（线程安全↔持久化 等仍拒）；soft 通道改用**关键约束清单**（否定词/数量容量/时空复杂度/并发线程安全/持久化/递归非递归/泛型/重复键/返回格式/完整vs仅某操作/版本号），仅当两侧关键约束冲突才拒。
- **接口**：
  - `POST /v1/decision`：`{query, cached_query}` → `{shared, reason, soft}`（纯规则，无模型）。
  - `POST /v1/decision/batch`：`{query, candidates:[...]}` → `[{cached_query, shared, reason, soft}]`（避免 Go 30 次串行 HTTP）。
  - 保留 `POST /rerank` 兼容（纯规则返回 shared/reason；score 语义废弃字段保留 0，标注 deprecated）——Go 主路径不再用它。
- **soft 通道触发**（feature 开关 + 校准）：
  ```
  enabled(default false)
  且 query.intent != unknown 且 query.intent == candidate.intent
  且 language_compatible（语言敏感意图下同值/双空；否则保守）
  且 operation_compatible（保守：任一侧不同拒）
  且 not critical_constraint_conflict
  且 任一侧有 subject_id 的另一侧缺失时：Phase2 初始仍拒（待独立验证再放宽）
  且 vector_score ≥ soft_threshold 且 margin ≥ soft_min_margin
  ```
  `reason="semantic_soft_match"`，Go 记录 source=soft 可单独评估/关闭。

### ④ 语义路由 `semantic/semantic.py`

- `/embed`：encode_query 一次 → 384 向量 + 全部文本/槽位字段（不变字段集合）。
- `/v1/decision`、`/v1/decision/batch`：见 §③。
- `/healthz`：liveness，`{status:"ok"}`（进程存活即 ok）。
- `/readyz`：tokenizer+session 已载、warmup 成功、输出维度 384、范数≈1、模型/namespace 可返回；失败非 2xx。
- `/model-info`：见 §①。
- 启动预加载+warmup；加载失败：readyz 非 2xx，不阻止 healthz；Go 视为不可用→缓存 miss。

### ⑤ kvstore（子模块 pocket-kv）VSEARCH 前缀参数 + 边界校验

- 命令：`VSEARCH <dim> <query_vec> <topk> [prefix]`；prefix 缺省 `semcache:`（向后兼容）。
- 参数校验（C 内）：
  - `1 ≤ prefix_len ≤ 64`；prefix 仅允许打印 ASCII（字母数字 `:_-`）；**空/缺失时用默认 `semcache:`**，显式空前缀拒绝；
  - `dim ∈ {256, 384, 1024}`（允许集可配常量）；
  - `1 ≤ topk ≤ 100`；
  - query_vec 字节数 == `dim*4`；
  - 允许前缀集：`semcache:`、`semd:e5s:v1:`（常量表；超集拒绝）。
- 测试：老三参兼容、新四参、空前缀/非法参数、前缀索引隔离、384 与 256 记录混存（dim 不一致记录被 parse_vec 跳过，不误扫）、边界单元（含 ASan 若环境允许）。
- 子模块独立提交 → ECHO-CHAT bump 指针 → 重编 `kvstore/kvstore/kvstore`。

### ⑥ Go `semcache`：一次编码 / 纯规则 / 复用 VSEARCH 分数

- config.go：`SemanticCache` 增
  ```go
  Dimension int                 // 384
  VectorNamespace string        // "semd:e5s:v1:"
  EmbeddingModel/EmbeddingRevision/ExportVersion
  AcceptanceThreshold float32   // acceptance_threshold
  MinMargin float32             // min_margin
  SoftSemanticFallback bool     // 默认 false
  VectorReadMode string         // "new_only" | "dual_read"
  VectorWriteMode string        // "new_only" | "dual_write"
  AsyncBackfill bool            // 默认 true
  ```
  保留 `Threshold/RerankThreshold` 字段但标注废弃（兼容读取，不再参与新链路）。
- `CacheQuery` 新流程：
  1. `/embed`（**Query 只编码一次**，返 vec+parse 元）→ bypass/双空门；
  2. fp(`semfp:v1:`) 命中 → 读候选 → `hard_decide`(纯规则，可走 `/v1/decision`) → 通过 → 返答；**若 fp 命中但 `semd:` 缺该候选向量且 AsyncBackfill** → 异步回填（幂等/限速/不阻塞/记成功率）；
  3. `VSEARCH <dim> vec topK <VectorNamespace>` → 对每个候选问题：
     - `/v1/decision/batch`(一次) 得 {shared,reason,soft}；
     - 通过者按 VSEARCH cosine 排序；`cos ≥ acceptance_threshold`（soft 命中另要求 soft_threshold/margin）；
     - Top1-Top2 margin < min_margin → miss；
     - 命中返回答案。
  4. 全不满足 → miss。
- `CacheWrite`：`SET <q>=<ans>` + `HSET <ns><q> = [u32 384][vec]` + fp；`ns` 来自 config。写模式 new_only：仅写新 ns；dual_write：旧 256 也写（灰度期）。
- 启动一致性：调 semantic `/model-info`，校验 model/revision/dimension/namespace 与 config 一致；不一致 → **禁用向量缓存**（fp 仍可用？评审建议禁用语义向量缓存；fp 不依赖向量可保留——决策：禁用 VSEARCH/写入，fp 路径保留并告警，保守可用）并记录明确错误。
- 旧 `semcache:`(256) 查询不再执行（read_mode=new_only 默认）；dual_read 期仅对照不打分命中。
- 老 `/rerank` 主路径移除（不再逐候选）；残留兼容入口保留但 Go 不调。

### ⑦ 迁移与灰度

- 重问驱动 + **指纹命中异步 E5 回填**（缓解长尾不进索引）：CacheQuery fp 命中且候选无新向量时异步 encode+写 `semd:`；幂等(先查)、限速、失败重试上限、不阻塞、记录回填成功率。
- `vector_read_mode/write_mode`：上线按 只写不读 → 影子双读 → 小比例启用 → 全量 → 停旧读 → 清旧前缀。
- 版本隔离：namespace 内才可比；换模型→新 namespace 灰度，不清现网索引。

### ⑧ 配置（dev + ai-chat-stack 同步）

```yaml
semantic_cache:
  enabled: true
  dimension: 384
  vector_namespace: "semd:e5s:v1:"
  embedding_model: "intfloat/multilingual-e5-small"
  embedding_revision: "<固定 SHA>"
  export_version: "onnx-int8-v1"
  top_k: 30
  acceptance_threshold: <启动前由校准产物填入，不得为 null 上线>
  min_margin: <同上>
  soft_semantic_fallback: false
  soft_threshold: null
  soft_min_margin: null
  exact_fingerprint_enabled: true
  async_backfill: true
  vector_read_mode: "new_only"
  vector_write_mode: "new_only"

semantic_runtime:
  request_timeout_ms: 1500
  workers: 1
  intra_op_threads: 4
  inter_op_threads: 1
  warmup_on_start: true
  max_length: 512
```

## 阈值校准与评估（开发/校准/测试）

- 数据构建（离线）：Phase-1 权威集并入"本体覆盖正样本"；新增超本体跨语言/同义正样本、普通负、同主题硬负、约束冲突负、绕过；按**语义主题分组**切分（同概念别名不跨集）。
- 流程：规则在开发集调；在**校准集**选 `acceptance_threshold`（Precision≥99% 时 Recall 最高）与 `min_margin`、soft 阈值；**独立测试集**只报一次（Recall@30/MRR/Cache Precision/False Hit/各类冲突误命中/95%CI bootstrap）。
- 样本规模目标（可持续扩）：本体正 80~150、超本体正 100~200、普通负 100~200、同主题硬负 150~300、约束负 100~200、绕过 30~50（可分批达成，首版 ≥200 可跑校准）。
- soft 默认关；启用须校准集 Precision≥99% 且独立测试假命中=0 或达标。
- 性能验收：见 §目标与验收要点（完整 CacheQuery p95/p99、QPS、CPU、RSS、切换、超时率；workers×线程×长度×冷热组合），达标后落默认配置。

## 测试与故障

- 模型：一致性(PT/FP32/INT8 on 50~100 条)、池化、NaN、空串/超长、checksum。
- 决策零回归(129)+ 泛化集断言；soft 开关两态。
- kvstore VSEARCH 边界/隔离/兼容（§⑤）。
- Go：build、/model-info 一致性、一次编码（埋点断言每次请求 encode_query 恰好 1 次）、fp 回填幂等、read/write mode。
- e2e（尽力）：写缓存→跨语言改写命中；停 semantic→miss 聊天不断。
- 故障表：模型缺失/校验和错→readyz 失败+不写 ns+miss；ONNX 加载失败→不写；/embed 超时→miss；维度≠384→拒写报警；Go/semantic namespace 不一致→禁用向量缓存(fingerprint 保留)+日志；kvstore 旧版不支持新 VSEARCH→回退 miss 不崩；新索引空→fp 仍工作。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| E5 INT8 CPU 延迟/内存 | Query 单次编码；规则无模型；INT8；线程配置压测；p95 为待测目标非预设 |
| 前缀双拼 | 前缀职责唯一在 models；双前缀自剥；测试 |
| 阈值过拟合/重叠 | 三分集+主题分组+bootstrap；单一 acceptance_threshold+margin |
| namespace 混模型 | `semd:e5s:v1:`+ /model-info 一致校验+换模型新 ns 灰度 |
| soft 误命中 | 默认关；critical 约束清单；独立 reason；Precision≥99% 门 |
| VSEARCH 前缀滥用 | C 内白名单/长度/dim/topk 校验 |
| 旧缓存不进新索引 | fp 异步回填+灰度读/写模式 |
| alpine 装不了 ORT | Dockerfile 换 slim |
| 池化/一致性出错 | 参考实现一致性测试+模型清单 sha256 |
| py3.8 vs 3.10 | 双环境同依赖；导出工具标注 3.10 |

## 涉及文件清单

新增：
- `semantic/models.py`；`semantic/models/e5s-v1/*`（onnx/tokenizer/MANIFEST.json，gitignore）
- `semantic/tools/export_e5_onnx.py`（一次性导出+INT8+MANIFEST，torch 仅此处）
- `semantic/tools/calibrate.py`（三分切分+阈值选择+bootstrap）
- `semantic/tests/test_models.py`、`semantic/tests/test_soft_fallback.py`、`semantic/tests/` 检索质量集（新增 fixtures）
- kvstore 子模块：VSEARCH 前缀参数+校验（`src/storage/kvs_vector.c`、命令分发、C 测试）+ ECHO-CHAT bump 指针

修改：
- `semantic/embedding.py`（→models.encode_query）、`semantic/decision.py`（hard_decide 纯规则 + critical 约束 + semantic_soft_match）、`semantic/semantic.py`（/v1/decision[/batch]、/healthz /readyz /model-info、warmup）、`semantic/requirements.txt`（onnxruntime/tokenizers/numpy）、`semantic/Dockerfile`(slim+COPY models+启动参数)、`semantic/README.md`
- `ai-chat-service/pkg/config/config.go`（Dimension/VectorNamespace/AcceptanceThreshold/MinMargin/soft/runtime 配置）
- `ai-chat-service/chat-server/semcache/semcache.go`（一次编码/纯规则/批决策/回填/modes/一致性校验）
- `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`、`ai-chat-stack/compose.yaml`（semantic 启动 env/workers/threads）
- `lib.sh`（semantic 启动参数若随配置变）
- `docs/项目文档/04-业务应用.md`（§4.2/§6.2 更新到 e5+纯规则决策）
