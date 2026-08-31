# ai-chat 本地语义知识库（kvstore VSEARCH + jieba 嵌入 + 语义缓存）设计文档

- 日期：2026-08-16
- 状态：已批准
- 位置：跨组件（9.1-kvstore 内核 + ai-chat/tokenizer + ai-chat-service）

## 背景与目标

ai-chat 原有知识库走**腾讯云向量库**（tcvectordb SDK），云资源不可达，路径失效。本设计把知识库做成**完全本地可达的语义问答缓存**（复刻 InazumaPlasma 架构，但全部落在自研组件上）：
- kvstore 内核加 `VSEARCH` 向量检索命令
- tokenizer 加 jieba 轻量嵌入 `/embed` + `/rerank`
- ai-chat-service 每条查询先查语义缓存，命中直接返回缓存答案（不调大模型）

## 已确认决策

- **向量存储**：复用现有 `SET`（kvstore 的 HSET/SET 同底层），每条缓存一个 key `semcache:<hash>`，值为二进制记录 `{query, answer, vector}`；**不新增 VADD/VDEL**（无独立向量索引、无单独持久化——SET 自带 AOF/dump）
- **VSEARCH**：kvstore **唯一新增命令**，遍历内部哈希表找 `semcache:*` 条目，暴力余弦 top-k，返回 RESP `(key, score)`（score=余弦相似度 0~1）
- **嵌入方案**：jieba 轻量嵌入（分词 + 字/词 n-gram 哈希 → 256 维，L2 归一化），零大依赖
- **触发范围**：**每条查询都查缓存**（非关键词门控）；关键词从"检索触发"改为"rerank 校验因子"
- **rerank 校验**：jieba 关键词/实体重合度打分，命中候选须过阈值，防假命中
- **维度固定 256**；阈值可配（默认待调优）

## 关键事实（已核实）

- kvstore 的 hash 是**全局 key→value 哈希表**（`global_hash`），HSET/HGET 即 SET/GET；无公开遍历 API，但 VSEARCH 在内核内可直接访问 `hashtable_s` 的 bucket（rehash 中需查新旧两张表）
- kvstore 有 `kvs_hash_set_len/get_len`（二进制安全，value 可含 `\0`）；RESP 解析支持 len 感知（argl）
- ai-chat-service 的 `server.go` 现有 `QueryData`(server.go:61/199)/`UpsertData`(:147/348) 是替换点
- tokenizer 现为 nuxt + tiktoken（仅 token 计数），Python 3.8；jieba 纯 Python 可装
- 本机内存 3Gi 可用 → 排除 torch/transformer（装不下、易 OOM）

## 组件设计

### ① kvstore：VSEARCH 命令（C）

**命令**：`VSEARCH <dim> <query_vec_binary> <topk>`
- 参数：dim（=256）、query 向量（bulk string 二进制，float 数组）、topk
- 语义：遍历 `global_hash` 中 key 前缀为 `semcache:` 的条目，读 value 里的向量，与 query 算余弦相似度，取 top-k
- 返回：RESP 交替数组 `*[2*k]` → `(key, score)`，score 为余弦 0~1（保留 4 位小数）
- 无匹配返回 `*0`

**缓存条目 value 格式**（自定义二进制，双方共享）：
```
[u32 q_len][q bytes][u32 a_len][a bytes][u32 dim][float vec[dim]]
```
- 由 ai-chat-service（Go）写入/解析；VSEARCH 只需跳过前两段、定位 `float vec[dim]` 算余弦
- 复用 `SET` 存储（二进制走 len 感知路径），VSEARCH 只读不写

**接入点**：`src/main/kvstore.c` 的 `handle_parsed_command` 命令分发；新模块 `src/storage/kvs_vector.c` + 头文件（纯 C，不引 FAISS）；AOF 不新增命令（VSEARCH 只读，缓存由 SET 持久化）

### ② tokenizer.py：/embed + /rerank（jieba）

- `POST /embed {text}` → `{code:200, embedding:[float;256]}`
  - jieba 分词 → 每词 hash % 256 桶（有符号累加）+ 字 bigram 同样处理 → L2 归一化
- `POST /rerank {query, cached_query}` → `{code:200, score:float}`（jieba 关键词重合度，0~1，供校验）
- requirements.txt 加 `jieba`

### ③ ai-chat-service：语义缓存集成（Go）

**新模块** `chat-server/semcache/semcache.go`（或并入 vector-data）：
- 客户端：go-redis（kvstore）+ tokenizer 客户端（复用现有 tokenizer 服务包）
- `CacheQuery(query)`：/embed 得向量 → `VSEARCH 256 <vec> 5` → 遍历候选，score>阈值 且 `/rerank` 校验过 → 返回缓存答案
- `CacheWrite(query, answer)`：/embed 得向量 → 组二进制记录 → `SET semcache:<hash> <记录>`

**server.go 流程改造**：
- `ChatCompletionStream`：敏感词校验后 → `CacheQuery` → 命中 → 流式返回缓存答案（复用现有"直接返回历史答案"的流式分支）→ 未命中 → 正常大模型
- 异步收尾：聊天完成后 `CacheWrite` 写入缓存（与现有 saveContext/MySQL goroutine 并列）
- `ChatCompletion`（非流式）同样接入
- **移除腾讯 vectorDB 路径**（`vector-data` 的 tcvectordb 实现不再使用；关键词抽取服务保留，但结果只进 rerank 校验 + MySQL 记录）

**配置**（ai-chat-service/dev.config.yaml）：
```yaml
semantic_cache:
  enabled: true
  threshold: 0.70        # VSEARCH 相似度阈值（默认待调优）
  rerank_threshold: 0.50 # jieba 校验阈值
```

## 验收标准

1. kvstore：`SET semcache:a <记录>` + 另一条相似记录后，`VSEARCH` 返回正确的 top-k 与 score（相似文本 score 高、无关文本 score 低）
2. kvstore 重启（AOF 重放）后 `VSEARCH` 仍能检索（SET 持久化生效）
3. ai-chat：发一条消息 → 缓存写入；再发**语义相近**的消息 → 直接返回缓存答案（秒回、`questions_total` 不增）
4. 语义不相关但文本相似的消息 → 被 rerank 校验拦下，正常走大模型
5. 全程本地，无外部云依赖；tokenizer 装 jieba 后启动正常
6. 回归：登录/鉴权/计费/限流/前端余额 不受影响

## 明确不做（Out of scope）

- 真 transformer 嵌入（内存/兼容性不允许）
- 文档上传/分段 RAG（只做问答语义缓存）
- kvstore VSEARCH 的 ANN 优化（暴力检索，本地规模足够）
- 关键词过滤器下线（保留，角色改为校验/记录）

## 任务分解（一个 plan，按依赖排序）

1. **kvstore VSEARCH**（C：命令分发 + 内部哈希遍历 + 余弦 + RESP 组装）
2. **tokenizer /embed + /rerank**（jieba）
3. **ai-chat-service 语义缓存集成**（CacheQuery/CacheWrite + server.go 改造 + 配置）
