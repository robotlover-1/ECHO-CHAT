# 设计文档：ai-chat 与 kvstore 拆分 + 明文 KV 存储 + 公有大模型 + tokens 统计

日期：2026-08-31
状态：已获用户批准（口头 OK）

## 1. 背景与目标

当前 `9.1-kvstore` 单仓合并了 kvstore（C 存储）与 ai-chat（Go/Python/Vue3 微服务组）。本次改动共 7 项：

1. ai-chat 与 kvstore 拆成两个独立 git 仓库，ai-chat 通过 submodule 引用 kvstore（同 kvstore 引用 NtyCo 的方式）。
2. 删除手机短信验证码流程。
3. 接入公有大模型（DeepSeek），修复"从 gitlab clone 后不生成真实代码"的问题。
4. 以 key-value 形式存储问题-回答，值必须是字符串（网页输入什么存什么），key 不做 hash。
5. 近似语义查询与向量构建（已实现，随需求 4 适配保留）。
6. 网页显示 tokens 总消耗与 tokens 节省数量（按当前会话累计）。
7. 每个回答标注来源：「公有大模型」或「缓存命中」。

## 2. 现状分析

### 2.1 架构与调用链

```text
ai-chat-web (Vue3)
  │ POST /api/chat-process
  ▼
ai-chat-backend (Go gin, :7080)
  │ gRPC ChatCompletionStream
  ▼
ai-chat-service (Go gRPC, :50055)
  │ 敏感词过滤 → semcache.CacheQuery → 关键词 → 调 LLM
  ▼
openai-api-proxy (:8084, 反向代理) → base_url
  ▼
DeepSeek (https://api.deepseek.com/v1) 或 mock-openai-api (:8083)
```

- `ai-chat-service` 同时写：多轮上下文 → kvstore（redis 协议，`ai_chat_service_` 前缀）；chat_records → MySQL；semcache → kvstore。
- kvstore 引擎：命令首字母路由（`H`→hash 引擎、其余→array 引擎），`SET/GET`→array、`HSET/HGET`→hash。`VSEARCH` 只扫 hash 引擎中 `semcache:` 前缀条目，暴力余弦 top-k。

### 2.2 当前 semcache（语义缓存）实现

- key：`semcache:<fnv32a(query)>`（query 的 hash）。
- value（hash 引擎二进制 record）：`[u32 qlen][query][u32 alen][answer][u32 dim][float vec[dim]]`。
- 写入：`HSET semcache:<hash> <record>`；查询：`VSEARCH dim vec topN` → `HGET semcache:<hash>` → 解析 record → rerank → 返回。

### 2.3 「不生成真实代码」根因（已定位）

gitlab 上提交的 `openai-api-proxy/dev.config.yaml` 的 `base_url` 是 `http://localhost:8083/v1`（指向 mock-openai-api，返回随机 canned 文本）；真实 DeepSeek 地址与 key 只存在于本地未提交的配置。→ 全新 clone 后所有 LLM 调用都打到 mock，输出不真实。

### 2.4 认证现状

- 后端路由：`POST /v1/sms/send/code`（SendCode）、`POST /v1/user/login`（Login，校验验证码 + 发 session token）、`POST /session`。
- 前端：`Permission.vue` 验证码登录弹窗 + 手机号/额度。
- MySQL `users` 表（phone/quota）；额度扣减在 ChatProcess EOF 处，按 tokenizer 统计的 prompt+completion tokens。

## 3. 仓库拆分

| | pocket-kv（C 存储仓） | ECHO-CHAT（ai 助手仓） |
|---|---|---|
| 远程 | `git@github.com:robotlover-1/pocket-kv.git`（新建） | `git@github.com:robotlover-1/ECHO-CHAT.git`（新建） |
| 内容 | `kvstore/`（含 NtyCo submodule）、`Makefile`、`lib.sh`、`configs/`、`docs/`（kvstore 相关）、`README.md`、`LICENSE`、`.gitignore`、`bin/` | `ai-chat-backend`、`ai-chat-service`、`keywords-filter`、`openai-api-proxy`、`mock-openai-api`、`tokenizer`、`ai-chat-web`、`ai-chat-stack`、`monitoring`、`start.sh`、`stop.sh`、`lib.sh`、`configs/`、`docs/` |
| submodule | NtyCo（github） | kvstore → pocket-kv |
| 历史 | 全新初始提交 | 全新初始提交 |

- 现有 `9.1-kvstore` 保持不动。
- ai-chat 的 `start.sh` / Makefile 需从 submodule 路径编译/启动 kvstore（`kvstore/kvstore`），配置 `configs/kvstore-ai.conf`（端口 5160）不变。
- 前置条件：github 空仓库已由用户创建（`robotlover-1/pocket-kv`、`robotlover-1/ECHO-CHAT`）。

## 4. 明文 KV 存储问题-回答（方案 A，已批准）

采用双结构：明文 Q-A 存储 + 独立向量索引。

```text
# Q-A 明文存储（array 引擎，人类可读）
SET  qa:<原始问题>  =  <原始回答>
# 向量索引（hash 引擎，内部结构，key 直接用原始问题，不 hash）
HSET semcache:<原始问题>  =  [u32 dim][float vec[dim]]
```

### 4.1 查询路径（CacheQuery）

1. 问题 → tokenizer `/embed` → 256 维向量。
2. `VSEARCH 256 <bin_vec> 5` 扫 hash 引擎 `semcache:` 前缀，返回 topN `(key, score)`。
3. 取 best `(key, score)`，score ≥ threshold 则继续。
4. 从 key 剥离 `semcache:` 前缀得到缓存问题的原文。
5. `GET qa:<缓存问题>` 取回答（明文 string）。
6. 用原文做 `/rerank` 校验（score ≥ rerank_threshold 且 shared）。
7. 通过 → 返回回答，标记 `source="cache"`。

### 4.2 写入路径（CacheWrite）

1. 问题 → tokenizer `/embed` → 向量。
2. `SET qa:<问题> <回答>`（写明文 Q-A）。
3. `HSET semcache:<问题> <[dim][vec]>`（写向量索引）。

### 4.3 涉及改动

- **kvstore C 侧**（pocket-kv）：`src/storage/kvs_vector.c` 的 `parse_vec` 由解析 `[qlen][query][alen][answer][dim][vec]` 简化为 `[dim][vec]`。VSEARCH 扫描逻辑不变。
- **ai-chat-service**（ECHO-CHAT）：`chat-server/semcache/semcache.go` 重写——去掉 fnv32a hash，key 用原始问题；encode/decode 简化为 `[dim][vec]`；`decodeAnswer`/`decodeQuery` 改为由 key 剥离前缀 + GET qa:。

### 4.4 边界

- array 引擎 value 非长度感知（`engine_set` 用 strlen）：回答为普通文本，无 `\0`，安全。
- RESP bulk 支持 key 内换行/二进制，问题为普通文本，安全。
- 大 value 限制 256KB（现有 GET 限制），回答超过会读回失败——维持现状，不处理。

## 5. 免密自动登录（去验证码）

- 后端：删除 `SendCode` 路由与 handler；`Login` 改造——入参改为 `device_id`（字符串），按 `device_id` upsert 用户（init_quota），发 session token，返回 quota。`Session` 语义不变。
- users 表：`phone` 列改 `device_id`（VARCHAR 64 UNIQUE），`InitUsersTable` 同步；存量库需 ALTER（开发期可直接重建）。
- 前端：删除 `fetchCode`/验证码弹窗逻辑；首访无 token 时生成 device_id（UUID）存 localStorage，调 `/v1/user/login` 自动登录；登录弹窗永不出现，额度仍展示与扣减。
- 额度扣减逻辑保留，仅把主键由 phone 换成 device_id（`users.go` 相关函数）。

## 6. 公有大模型接入

- `openai-api-proxy/dev.config.yaml`：`chat.base_url` 默认 `https://api.deepseek.com/v1`；key 不写死在配置文件，从环境变量 `DEEPSEEK_API_KEY` 读取（proxy 读取逻辑新增）。
- `start.sh`：启动 proxy 前检查 `DEEPSEEK_API_KEY`（为空则提示并可用 mock 兜底或退出），注入环境变量。
- `.gitignore`：确保任何本地 key 不进 git。
- mock-openai-api 保留：作为离线/无 key 时的开发选项，配置可切回 `http://localhost:8083/v1`。
- 模型沿用 `deepseek-v4-flash`。

## 7. 近似语义查询与向量构建

机制保留：tokenizer `/embed`（jieba 词 + 字符 bigram 桶哈希 256 维）、`/rerank`（TF 关键词交集）、kvstore `VSEARCH` 暴力余弦 top-k。仅随 §4 调整 record 格式。

## 8. 来源标注 + tokens 统计（按会话累计）

### 8.1 proto 扩展（ai-chat-service 与 ai-chat-backend 两份 proto 同步）

`ChatCompletionStreamResponse` 新增：

```proto
string source = 6;      // "llm" | "cache"
int32  tokens_used  = 7; // 实际消耗 tokens（LLM 回答）
int32  tokens_saved = 8; // 节省 tokens（缓存命中，若调大模型本会消耗的 prompt+completion）
```

重新生成两份 pb.go（protoc/protoc-gen-go 已装）。

### 8.2 ai-chat-service 侧

- 每条流式响应带上 `source`（命中缓存路径 = "cache"，否则 = "llm"）。
- 每条响应只带 `source`；`tokens_used`/`tokens_saved` 只在流结束前的**最后一包**上携带（该包即 LLM 路径的 `finish_reason=stop` 包；缓存路径是显式发送的 stop 包）：
  - LLM：`tokens_used` = 现有 tokenizer 统计的 prompt+completion 实际值（流结束后计算，补进最后一包后再 `stream.Send`）。
  - 缓存命中：`tokens_saved` = 对 query/answer 各调 tokenizer 估算的"本会消耗"值（命中后立即算出，随 stop 包发出）。
- 缓存命中路径**不**走 LLM、**不**写 chat_records 的新一轮大模型调用记录（沿用当前命中即返回逻辑）。

### 8.3 ai-chat-backend 侧

- `ChatMessage` 增加 `Source`、`TokensUsed`、`TokensSaved` 字段，从流式响应透传。
- EOF 计费逻辑按来源分派：`source=="llm"` → 照旧扣额度、`tokens_used` 上报；`source=="cache"` → 不扣额度、`tokens_saved` 上报。

### 8.4 前端

- 每条 AI 回答下加来源徽标：「公有大模型」/「缓存命中」（按 `data.source`）。
- 页面固定位置显示会话级统计：「总消耗 tokens」与「节省 tokens」，随每条回答的 summary 累加更新。
- `Chat.ConversationResponse`/`ChatMessage` 类型同步扩展。

## 9. 改动文件清单（预估）

**kvstore 仓（pocket-kv）**
- `src/storage/kvs_vector.c`：parse_vec 简化。

**ai-chat 仓（ECHO-CHAT）**
- `ai-chat-service/proto/chat.proto` + 重新生成 pb.go（×2：service/backend）
- `ai-chat-service/chat-server/semcache/semcache.go`：重写为明文 key + GET qa:。
- `ai-chat-service/chat-server/server/server.go`：CacheQuery/CacheWrite 调用适配；流式响应补 source/summary。
- `ai-chat-backend/cmd/main.go`：删 SendCode 路由。
- `ai-chat-backend/pkg/controllers/auth.go`：删 SendCode；Login 改 device_id 自动登录。
- `ai-chat-backend/pkg/controllers/chat.go`：ChatMessage 加字段；计费按 source 分派。
- `ai-chat-backend/pkg/users/users.go`：phone → device_id。
- `ai-chat-backend/pkg/db/mysql/mysql.go`：InitUsersTable 改列。
- `openai-api-proxy/pkg/config` / proxy：key 走环境变量。
- `openai-api-proxy/dev.config.yaml`：base_url 改 DeepSeek。
- `start.sh`：注入 `DEEPSEEK_API_KEY`；kvstore 编译路径适配 submodule。
- `ai-chat-web/src/api/index.ts`、`views/chat/index.vue`、`views/chat/layout/Permission.vue`、`views/chat/components/Message/*`、`typings/chat.d.ts`、store：去验证码、加徽标、加统计条、device_id 自动登录。
- `Makefile`、`.gitmodules`、`.gitignore`：repo 拆分适配。

## 10. 验证方式

1. 两个新 repo 独立 clone 正常；ai-chat clone 后 `git submodule update --init` 拉下 kvstore，`start.sh` 能编出并启动 kvstore。
2. 免密：清 localStorage → 打开页面 → 无弹窗，自动登录成功，额度展示为 init_quota；连续提问扣减额度正常。
3. 公有大模型：`DEEPSEEK_API_KEY=<key> ./start.sh` 后提问得到真实代码/回答（非 mock canned 文本）。
4. 明文 KV：`redis-cli -p 5160 GET qa:<问题>` 返回原始回答字符串；`HSET semcache:*` 不存在 hash key；`VSEARCH` 能命中近似问题。
5. 来源标注：缓存命中回答显示「缓存命中」，未命中显示「公有大模型」。
6. tokens 统计：同一问题问两遍，第一遍总消耗增加、节省为 0；第二遍消耗不变、节省 >0。

## 11. 风险与未决事项

- github 仓库已由用户创建；推送需本机有 `github.com/robotlover-1` 的写权限（SSH key 或 PAT）。
- users 表列变更：存量库需 ALTER/重建（开发期可接受）。
- semcache record 格式变更使旧数据不可用：缓存可整体清空（kvstore dump 重建），无迁移成本。
- 长回答（>256KB）读回失败：维持现状，不处理。
- 全局 tokens 聚合（如需）本期不做，只做会话级累计（用户已确认）。
