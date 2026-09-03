# ECHO-CHAT 语义检索 Phase 2+3（合并）：真实 Embedding(multilingual-e5) + 混合硬门检索 设计文档

- 日期：2026-09-04
- 状态：已批准（brainstorming 逐段确认）
- 位置：ECHO-CHAT（`semantic/` 服务内 models 封装 + kvstore 子模块 VSEARCH 前缀参数 + Go semcache 384 维接入）
- 关联：`proj/tmp/ECHO-CHAT语义检索高命中率改造方案.md`（远景 Phase 2/3）、`docs/superpowers/specs/2026-09-03-semantic-phase1-rule-alias-design.md`（Phase 1 已完成）、`docs/superpowers/specs/2026-09-03-semantic-service-decouple-design.md`（Phase 0）

## 背景与目标

Phase 0/1 后：语义服务(:3003)有 256 维 FNV 词面向量 /embed + 本体/槽位/残差/指纹 parse + 保守 decision 硬门(9 reason) + 安全指纹(候选+复核)。向量层仍是**词面哈希**——跨语言/超本体改写召回受限。tmp 方案把"换真实 Embedding"与"混合检索/精排"分成 Phase 2/3；本设计**合并两者**，在 CPU-only(4 核/7GB)约束下落地"真实语义向量 + 既有规则硬门"的混合检索。

**已确认决策（与用户逐段对齐）**：
1. **范围**：multilingual-e5-small(ONNX/INT8、免 torch runtime)整体替换 256 维词面向量作为唯一"语义向量"层；Phase-1 的 parse/本体/指纹/硬门**原样保留**作硬门与精确命中。**不做** BM25/sparse、cross-encoder、Qdrant、微调。
2. **运行时/镜像**：Dockerfile 基座 alpine→`python:3.10-slim`(glibc)；本地(ubuntu py3.8)与镜像同一套 onnxruntime+tokenizers 依赖；模型文件 COPY 进镜像；本地验证为主、镜像不构建(无 daemon 权限，交付 Dockerfile/README)。
3. **索引/迁移**：给 kvstore `VSEARCH` 加**可选前缀参数**(向后兼容默认 `semcache:`)，e5 向量写新前缀 `semd:`；旧 256 `semcache:` 不再写入。**放弃全量迁移**——重问驱动重建（见 §迁移）；语义指纹直达与向量无关、旧缓存仍可 fp 命中。
4. **阈值**：离线校准脚本在权威集上取 `vector_search_threshold` / `rerank_threshold` / `soft_θ`，不拍脑袋。
5. **"无本体语义兜底"软通道**：默认关；条件窄化 + 校准验证后由数据决定是否开。
6. **管线**：查询 → parse(不变)→ ①指纹精确 ②VSEARCH `semd:`(e5 384, TopK30)→ decision(不变硬门)+ e5 余弦阈值 → 命中/miss。

## 目标与验收要点

**目标**：跨语言/同义改写召回从"受本体/别名覆盖"升级为"真语义泛化且不牺牲区分"；CPU-only 上 p95 ≤500ms（量化 e5-small 短文本单次 ~10-60ms、纯本地推理）；决策级零回归。

**验收要点**：
1. Phase-1 权威集(129 对)在 e5 下：全部 reject 对仍被硬门拒（decision-level 零回归）；match 对 decision.ok 的 e5 余弦全部 ≥ 新 `rerank_threshold`（跨语言对应更高更稳）。
2. 语义泛化样本（无本体改写，如 `LRU缓存实现 ↔ 最近最少使用缓存怎么写`）纳入校准集；软通道默认关时它们走保守 miss（与现状一致），开关开启时按 θ_soft 判定（验收脚本两者都覆盖）。
3. 阈值校准脚本输出建议值与直方图；数值写入 config，并加测试断言（match 行余弦 ≥阈值+裕量；未被硬门拦住的 reject 对上限之下）。
4. /embed 返回 384 维 e5 向量并保留全部文本/槽位字段；healthz 报 `model_loaded/embedding_model=multilingual-e5-small/dimension=384/model_version`。
5. Go：CacheQuery 走 `VSEARCH 384 … semd:`；CacheWrite 写 `semd:`；旧 256 不再写入；kvstore 子模块 VSEARCH 加可选前缀且向后兼容。
6. 服务重启后 `/rerank` reason 语义不变、ok 分值来自 e5 余弦。

## 明确不做（Out of scope）

BM25/稀疏检索；cross-encoder 精排；Qdrant/外部向量库；passage/query 不对称编码；模型微调/训练；全量旧数据离线迁移（重问驱动重建）；reason 驱动 Go 决策逻辑；Prometheus 指标（仍走标签日志）。

## 组件设计

### ① 模型封装 `semantic/models.py`（新增，纯函数、无 nuxt）

- 常量：`EMBEDDING_MODEL="multilingual-e5-small"`、`DIMENSION=384`、`MODEL_VERSION`（来自模型目录元信息）、`QUERY_PREFIX="query: "`。
- `encode(text) -> list[float]`：输入 `QUERY_PREFIX+text` → HF fast tokenizer → ONNX `InferenceSession`(INT8, CPU EP) → 平均池化 → L2 归一化 → 384 维。
- 懒加载单例：`InferenceSession` + tokenizer 首调时载入（~200-500ms）；nuxt `--workers 2` 各进程持一份（INT8 内存 ~200MB/worker）。
- 线程安全：onnxruntime session `run` 并发安全；tokenizer 调用加进程内锁（如有必要）。
- 失败策略：载入/编码失败抛明确异常；/embed 转 500 + msg，Go 优雅 miss。
- 模型文件放在 `semantic/models/<model>/`（gitignore），含 `.onnx`、`tokenizer.json`、`config.json`、`special_tokens_map.json`、`model_version` 文本；Dockerfile `COPY semantic/models/ /app/models/`。

### ② `embedding.py` 改造（替换向量算法）

- `embed_text(text)`：改为 `models.encode(text)`（对称 "query: " 前缀，语料本为问句，不做 passage 不对称——管线最简、阈值好标）。FNV/`_hash_bucket`/`SUBJECT:…` 桶逻辑退役（可从文件中删除；本体/别名仍被 parse/fp/decision 使用，不需要向量里重复）。
- 保留接口 `embed_text(text)->list[float]`（维度变为 384），`/embed` 与 decision 继续复用。

### ③ `decision.py` ok 分值换 e5 余弦

- decide() 硬门顺序与 reason 完全不变；reject/unknown 分支仍 0.0。
- ok 分支分值：`cos(models.encode("query: "+q.raw_text), models.encode("query: "+c.raw_text))`（e5 余弦，替代 Phase-1 最终修复里的 FNV canonical 余弦）。跨语言/同义对更高更稳。

### ④ 语义路由 `semantic.py`

- `/embed`：返回原全部文本/槽位字段（code/embedding/bypass_cache/context_dependent/intent/subject/subject_id/language/operation/output_type/fingerprint/fingerprint_eligible/parser_version/ontology_version）+ embedding 由 256→384 维 e5。embedding 字段语义变化（有意，Phase 2）。
- `/rerank`：reason 不变；score 来自 e5（见 §③）。
- `/healthz`：增 `model_loaded/embedding_model/dimension/model_version`；模型未载入时 status 仍 ok 但 model_loaded=false（编排可据此决定是否健康）。

### ⑤ kvstore（子模块 pocket-kv）VSEARCH 前缀参数

- 命令：`VSEARCH <dim> <query_vec> <topk> [prefix]`；`prefix` 缺省 `"semcache:"`（向后兼容）。`kvs_vector.c` 前缀匹配由常量改为参数；前缀长度随参数。
- 改动在子模块仓库（路径 `kvstore/`，remote robotlover-1/pocket-kv）：子模块内提交 → ECHO-CHAT bump 指针 → 重编 `kvstore/kvstore/kvstore` 二进制（lib.sh 已用该路径）。
- 不改记录格式（`[u32 dim][float vec[dim]]` 小端）、不动 AOF（VSEARCH 只读）。

### ⑥ Go `semcache`（384 + semd:）

- config.go：`SemanticCache` 增 `Dimension int`(`dimension`, 默认 384)、`EmbeddingModel/EmbeddingVersion`(观测)、`VectorSearchThreshold`(复用 `Threshold`)、`RerankThreshold`(复用)、`SoftSemanticFallback bool`(`soft_semantic_fallback`, 默认 false)；yaml(dev + ai-chat-stack)同步。
- 常量 dim=256 → 用 config `Dimension`(或包级 384 常量并注释与 semantic 一致)。
- `CacheQuery`：bypass/双空门 → fp(`semfp:v1:`,不变)→ `VSEARCH <dim> vec topK <"semd:">` → 逐候选 `GET <candidate>` + rerank(decision) → `shared && reason=="ok" && score>=RerankThreshold`。旧 `semcache:` 前缀不再查询。
- `CacheWrite`：`SET <q>=<ans>`(不变) + `HSET semd:<q> = [u32 384][vec]` + fp(不变)。旧 256 不再写。
- 软通道（§⑦）透传由 semantic decision 实现，Go 不新增判断。

### ⑦ "无本体语义兜底"软通道（默认关）

新值所在。目的：把**两侧都无 subject_id、靠句式抽到 subject_text 但不相等**的超本体改写召回（Phase-1 硬门会保守拒）。
- 触发（semantic decision 内新 stage，仅在严格 subject 规则因"缺 subject_id"而想拒时启用）：
  1. 两侧 subject_id 皆空，或一侧空但两侧 subject_text 规范化相等；
  2. intent 已知且相等；
  3. 语言相等或双空；
  4. residual 相等；
  5. e5 余弦 ≥ θ_soft（校准）。
- 不触发（仍硬拒）：任一侧 subject_id 有值且不等 → subject_conflict；或 intent/residual/语言冲突。
- 开关：常量/配置 `soft_semantic_fallback`，**默认 false**。校准脚本对"泛化正/负样本"验证假命中=0 后，建议才置 true；false 时上述样本走保守 miss。
- reason：软通道命中给 `reason="ok"`（记日志 `source=semantic_soft`），不新增枚举以保持 Go 兼容；关闭时行为=现有保守拒。

## 迁移与兼容

- **重问驱动重建**：kvstore 无公开遍历命令 → 不做全量离线迁移。旧 `semcache:`(256) 不再写入，保留可后续清；e5 向量随新问答写入 `semd:` 逐步回填。语义指纹(fingerprint)与向量无关，**旧缓存仍可被 fp 精确命中**（同槽位措辞），无需重建。
- 模型/版本变更：`model_version` 进 healthz/日志；若换模型需清 `semd:` 重建（与 Phase-0 §18 隔离原则一致）。
- Docker：镜像基座 `python:3.10-slim`，requirements 加 `onnxruntime`/`tokenizers`/`numpy`（torch 不进镜像；仅导出期 dev venv 用）。本地 ubuntu py3.8 装同套（onnxruntime cp38 wheel 存在）。alpine 不再支持模型运行（基座替换是必须的）。
- docker-compose/stack config 同步 `dimension: 384` 等（compose 定义更新，不构建）。

## 阈值校准（方法）

`semantic/` 下新增校准/工具脚本（离线、pytest 可引）：
1. 输入：Phase-1 权威集(129)+ 语义泛化样本(无本体改写正/负集，>30 对)。
2. 对 match 行、reject 行（未被硬门拦的）分别算 e5 余弦，输出直方图与分位。
3. 定值规则：`vector_search_threshold` = 未拦 reject 对 max 之上留裕量；`rerank_threshold` = match 对 min 之下留 0.05 裕量；`θ_soft` = 泛化正集 min 与 泛化负集 max 的分界留裕量。
4. 结果写回 config(dev/stack) 与测试断言；报告中给实测分布。

## 测试与评估

- 离线 pytest（`cd semantic && python3 -m pytest tests/ -q`）：
  - decision/权威集 129：决策级零回归 + match 行 e5 余弦 ≥ rerank_threshold（Go accept 谓词级）。
  - embedding：维度 384、L2 归一、跨语言对(红黑树↔red-black tree/rbtree)余弦高、跨主题低。
  - models：encode 确定性（同输入同向量）、懒加载、healthz model_loaded 字段。
  - 软通道：默认 false 行为 = 现状保守；true + θ_soft 样本断言（正过负拒）。
  - 校准脚本：可复现输出建议阈值。
- Go：`go build ./...`；服务层冒烟——/embed 384、rerank reason、fp 命中；kvstore VSEARCH 带前缀返回 semd: 候选。
- e2e（尽力）：写缓存后跨语言改写命中（如 "用 C 语言实现一个红黑树" → "write a red-black tree in C"）；停 semantic 降级 miss 聊天不断。
- 性能：/embed、/rerank p50/p95 记录（e5 CPU），给出数字作后续基线。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| e5 INT8 CPU 延迟/内存 | e5-small 量化；懒加载；nuxt workers 2×(~200MB) 内存可控；超时走 miss |
| ONNX 导出受阻（需 torch 仅导出期） | 一次性 dev venv(3.10) 用 optimum/transformers 导出；不进 runtime 依赖；若受阻回退有官方 ONNX 的多语言模型（记录选型） |
| 阈值标错 | 离线校准 + 裕量 + 测试断言；默认保守 |
| 新前缀需改 pocket-kv C | 子模块内提交 + 可选参数向后兼容 + bump 指针 + 重编；子模块 diff 单独评审 |
| alpine 装不了 onnxruntime | Dockerfile 基座换 slim(glibc) |
| 无 subject 软通道误命中 | 默认关；触发条件窄 + θ_soft 校准 + 假命中=0 门；开关由数据决定 |
| py3.8 vs 3.10 | 双环境同依赖；镜像 3.10-slim；导出工具标注 3.10 |
| 旧缓存无 e5 向量 | fp 直达仍可用；`semd:` 由重问逐步回填；旧 256 保留可清 |

## 涉及文件清单

新增：
- `semantic/models.py`
- `semantic/models/<model>/…`（.onnx/tokenizer/…，gitignore）
- `semantic/tools/calibrate_thresholds.py`（离线校准）
- `semantic/tests/test_models.py`、`semantic/tests/test_embedding.py`（改造）、`semantic/tests/test_soft_fallback.py`

修改：
- `semantic/embedding.py`（e5 封装调用）、`semantic/decision.py`（ok 分值=e5 余弦 + 软通道 stage）、`semantic/semantic.py`（/embed /healthz 字段）、`semantic/requirements.txt`（onnxruntime/tokenizers/numpy）、`semantic/Dockerfile`（slim 基座 + COPY models）、`semantic/README.md`
- kvstore 子模块（`src/storage/kvs_vector.c` VSEARCH 前缀参数 + 命令分发）+ ECHO-CHAT bump 子模块指针
- `ai-chat-service/pkg/config/config.go`（Dimension/SoftSemanticFallback 等）
- `ai-chat-service/chat-server/semcache/semcache.go`（semd: + 384 + soft 透传）
- `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`（dimension/阈值/开关）
- `ai-chat-stack/compose.yaml`（镜像 tag 变化说明）、`docs/项目文档/04-业务应用.md`（§4.2/§6.2 更新到 e5）
