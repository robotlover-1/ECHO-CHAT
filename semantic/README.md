# semantic（语义检索独立服务）

承接 ECHO-CHAT 语义缓存的向量生成与规则校验，与 tokenizer（计 token）解耦。
纯逻辑模块（parse/decision/embedding/ontology）不 import nuxt，py3.8 兼容；服务薄路由在 `semantic.py`。
- `/embed`：256 维 FNV 哈希加权词面嵌入 + 双路主题/意图/语言/操作/残差/指纹抽取（见下 /embed 响应字段）
- `/rerank`：保守对称硬拒的 decision(reason)（subject/language/operation/intent/residual 五种 conflict 原因 + ok）
- `/healthz`：健康检查（embedding_type=fnv_hash, dimension=256, parser_version=v1）

## /embed 响应字段（POST /embed {text}）
| 字段 | 含义 |
|---|---|
| `embedding` | 256 维 L2 归一 FNV 词面向量（canonical 主题桶用 subject_id） |
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

## 镜像构建（本机 registry 默认）
docker build -t 192.168.233.128:5000/2404/semantic:1.0.0 .

## 服务启动
docker service create --name 2404-semantic \
-p 3003:3003 --replicas 2 --with-registry-auth \
192.168.233.128:5000/2404/semantic:1.0.0

## 本地进程直跑
nuxt --port 3003 --module semantic.py --workers 2
