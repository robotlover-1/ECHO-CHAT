# semantic（语义检索独立服务）

承接 ECHO-CHAT 语义缓存的向量生成与规则校验，与 tokenizer(计 token)解耦。
纯逻辑模块（parse/decision/models/embedding/ontology）不 import nuxt，py3.8 兼容；服务薄路由在 `semantic.py`。

- `/embed`：返回 **384 维 L2 归一 e5 语义向量**（`multilingual-e5-small` INT8 ONNX，token 输出经 attention-mask 平均池化 L2），
  语义向量 = `models.encode_query(text)`（写/查对称加一条 `query:` 前缀，详见 encode_query/encode_passage），另带双路主题/意图/语言/操作/残差/指纹字段（见下）。
- `/v1/decision`：**纯规则决策**（scoring 已移交 VSEARCH/Go，端点不再产模型分）：`{code, shared, reason, soft}`。同 `/v1/decision/batch`
  批量逐个 `{query, candidates[]} → {code, results:[{cached_query, shared, reason, soft}]}`，供 Go 一次嵌入后批量复核 VSEARCH 候选题。
- `/rerank`：**deprecated**——decision 纯规则化后不再产模型分，保守保留返回 `{code, score:0.0, shared, reason, soft}`（score 恒 0.0）供旧兼容。
- `/healthz`：liveness，进程活着即 `{"status":"ok"}`（不依赖模型加载）。
- `/readyz`：readiness，模型已载 + warmup 通过 → `{"status":"ok",...model-info}`；否 → `{"status":"error",...}`。
- `/model-info`：`{code, model, revision, dimension, vector_namespace, export_version}`（供 Go 启动一致性校验，
  不一致则禁用语义向量 cache）。

## /embed 响应字段（POST /embed {text}）
| 字段 | 含义 |
|---|---|
| `embedding` | 384 维 L2 归一 e5 语义向量（mean-pool + L2，== models.encode_query） |
| `subject` / `subject_id` | 主题文本 / canonical 本体 id（别名共享同一 id；无则 null） |
| `intent` | definition/implementation/operation/…/unknown |
| `language` | 目标语言 id（python/c/go/cpp…） |
| `operation` | 规范操作 id（insert/delete/find…）；概念名内嵌操作字不算 |
| `output_type` | code/explanation |
| `bypass_cache`/`context_dependent` | 缓存准入：上下文依赖/状态修改指令 → 不查不写全局语义缓存 |
| `fingerprint` | 版本化语义指纹 sha256（schema v1）；不可安全建模时 null |
| `fingerprint_eligible` | 保守准入：subject_id 有、intent 非 unknown、非 bypass、无残差 |
| `parser_version` / `ontology_version` | v1 / 版本号 |

## /v1/decision[/batch] 说明（POST；Go 一次嵌入后对该批候选纯规则复核，不再逐候选 /rerank）
- `/v1/decision` 入参 `{query, cached_query}` → `{code, shared, reason, soft}`；`/v1/decision/batch` 入参
  `{query, candidates:[...]}` → `{code, results:[{cached_query, shared, reason, soft}]}`（go 向量路径一次 HTTP 复核 topK）。
- `shared=true` 语义等价可共享缓存条目（soft 兜底通道时另需 Go `soft_semantic_fallback`）；`shared=false` 为保守硬拒。
- `reason` 取值：`ok` / `subject_conflict` / `language_conflict` / `operation_conflict` / `intent_conflict` /
  `constraint_conflict`（残差不同→未解析约束）/ `unknown_subject` / `unknown_intent`（一侧槽位缺失时的拒因）/
  `semantic_soft_match`（soft 兜底允许的语义近似匹配）。端点**不产模型分**——scoring(acceptance_threshold=0.6 / min_margin=0.0)
  由 Go 在 VSEARCH 余弦上本地判定（见 §① 缓存编排）。
- 主题冲突仅在双方都有 subject_id 且不同才算；None 侧一律保守拒（避免弱匹配）。语言对实现/输出 code 敏感对称比较。
- `/rerank`（旧名，曾返回 score）已 deprecated：现走同一 `hard_decide`，`score` 恒 0.0，仅供旧客户端兼容。
- soft 兜底：默认关（`SEMANTIC_SOFT_FALLBACK=0`）；仅在 subject 硬门因"缺 id"拟拒(unknown_subject)时才有机会，
  且 critical 约束(intersection 差)非空、语言敏感意图下语言不同、operation 不同等情况下不放行。

## 模型工件（semantic/models/e5s-v1/，gitignore；由 Task1 导出）
- `model.onnx`：multilingual-e5-small 动态量化 **INT8** ONNX；输入 `input_ids`/`attention_mask`(int64,[1,512])，
  输出 `token_embeddings`[1,512,384]（服务手动平均池化）+ `sentence_embedding`[1,384]（备用）。
- `tokenizer.json` / `special_tokens_map.json` / `config.json` / `MANIFEST.json`（含 upstream_model/
  upstream_revision/onnxruntime_version/quantization_config/model_sha256/…）；`onnx-fp32/` 为 FP32 对照。
- 池化与 `tools/verify_consistency.py` 一致：token 输出 `token_embeddings` 按 `attention_mask` 真实 token
  平均（除以 mask 和）再 L2。pad 以模型 pad token(=1, `<pad>`) 对齐 transformers/PT。

## 服务启动（nuxt）
```bash
# 本机直跑（ORT 线程可用环境变量调，默认 intra=4/inter=1；模型缺失/加载失败仅记日志，readyz 反映）
SEMANTIC_INTRA_OP=4 SEMANTIC_INTER_OP=1 \
  nuxt --port 3003 --module semantic.py --workers 2
```
- 启动即 `models.warmup()`（首条固定短文本校验 384 维）；失败仅记日志不退出，`/readyz` 会返回 error。
- healthz==liveness；readyz 才反映模型就绪（warmup+维度/范数）。
- ORT 线程 env：`SEMANTIC_INTRA_OP`(默认 4) / `SEMANTIC_INTER_OP`(默认 1)，CPUExecutionProvider；`SEMANTIC_SOFT_FALLBACK`(默认关) 控制 decision soft 兜底。sw 内每容器一个副本，`intra_op` ≥ 扩容无争抢（详见 Dockerfile/compose 启动命令）。
- 容器：`Dockerfile` 基镜像 **`python:3.10-slim`**（非 alpine），把 `semantic.py/parse.py/embedding.py/models.py/decision.py/ontology` 与模型工件 `models/e5s-v1`（INT8 onnx + tokenizer + MANIFEST）整体 ADD 进 `/app`，随镜像打包；requirements 含 `onnxruntime==1.19.2`、`tokenizers==0.15.2`、`numpy==1.24.4`。镜像尺寸相比初版显著缩小（slim + INT8），host 无需预装 transformers/torch。

## 镜像构建 / 服务编排（本机 registry 默认）
```bash
docker build -t 192.168.233.128:5000/2404/semantic:1.1.0 .
docker service create --name 2404-semantic -p 3003:3003 --replicas 2 --with-registry-auth \
  192.168.233.128:5000/2404/semantic:1.1.0
```
