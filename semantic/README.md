# semantic（语义检索独立服务）

承接 ECHO-CHAT 语义缓存的向量生成与规则校验，与 tokenizer(计 token)解耦。
纯逻辑模块（parse/decision/models/embedding/ontology）不 import nuxt，py3.8 兼容；服务薄路由在 `semantic.py`。

- `/embed`：返回 **384 维 L2 归一 e5 语义向量**（`multilingual-e5-small` INT8 ONNX，word 向量经 attention-mask 平均池化），
  语义向量 = `models.encode_query(text)`（写/查对称 query 前缀），另带双路主题/意图/语言/操作/残差/指纹字段（见下）。
- `/rerank`：保守对称硬拒的 decision(reason)（subject/language/operation/intent/residual 五种 conflict 原因 + ok）。
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

## /rerank 说明（POST /rerank {query, cached_query}）
- 返回 `{score, shared, reason}`：`shared=true` 语义等价可命中；`shared=false` 为保守硬拒。
- `reason` 取值：`subject_conflict` / `language_conflict` / `operation_conflict` / `intent_conflict` /
  `constraint_conflict`（残差不同→未解析约束）/ `unknown_subject` / `unknown_intent`（一侧槽位缺失时的拒因）。
- 主题冲突仅在双方都有 subject_id 且不同才算；None 侧一律保守拒（避免弱匹配）。语言对实现/输出 code 敏感对称比较。

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
- 容器：镜像内 `/app/models/e5s-v1` 随镜像打包；requirements 含 `onnxruntime==1.19.2`、`tokenizers==0.15.2`、`numpy==1.24.4`。

## 镜像构建 / 服务编排（本机 registry 默认）
```bash
docker build -t 192.168.233.128:5000/2404/semantic:1.1.0 .
docker service create --name 2404-semantic -p 3003:3003 --replicas 2 --with-registry-auth \
  192.168.233.128:5000/2404/semantic:1.1.0
```
