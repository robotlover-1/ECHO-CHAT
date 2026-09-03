# semantic 独立服务解耦设计文档（语义检索从 tokenizer 拆分）

- 日期：2026-09-03
- 状态：已批准
- 位置：ECHO-CHAT monorepo（`semantic/` 新服务 + `tokenizer/` 裁剪 + `ai-chat-service` 指路 + 编排/文档）
- 关联：`proj/tmp/ECHO-CHAT语义检索高命中率改造方案.md`（仓外草稿，远期蓝图；本设计只做其"独立 Semantic Service"的宿主拆分第一步）

## 背景与目标

当前 `tokenizer/`（Python nuxt 服务，:3002）身兼三职：

1. `/tokenizer/<model>` —— tiktoken 计 LLM token（**tokenizer 本职**，保留）
2. `/embed` —— 256 维 FNV 哈希加权"词面嵌入"+ 主题/意图/上下文依赖/状态修改抽取
3. `/rerank` —— 规则启发式硬拒（主题/意图/语言/操作冲突）+ 关键词 Jaccard

语义检索逻辑物理上被塞在 tokenizer 服务里，与计 token 职责耦合。tmp 方案（§7.3）规划了一个独立的 Python Semantic Service 作为后续换真模型、混合检索的宿主。

**本轮目标**：只做**架构解耦、行为逐字节不变**——把语义逻辑从 tokenizer 抽到 monorepo 内新增的独立服务 `semantic/`，tokenizer 回归纯计 token。

## 已确认决策

- **范围**：仅解耦，行为不变（嵌入仍 256 维 FNV 哈希、阈值 0.35/0.25 不动、kvstore VSEARCH 不动）
- **位置**：ECHO-CHAT monorepo 新增 `semantic/`，与 `tokenizer/` 平级、同风格（nuxt、独立端口 3003、自带 requirements/Dockerfile/README），注册进本地编排与 compose
- **HTTP 契约**：新服务照搬现有 `/embed`、`/rerank` 路由与请求/响应结构；semcache.go 的 `embedResp`/`rerankResp` 解析零改动
- **镜像地址**：semantic 的构建目标与 compose 镜像 tag 用本地 registry `192.168.233.128:5000/2404/semantic:1.0.0`（本机即 192.168.233.128）。compose 既有 5 个服务仍是 `239.161:5000`，本轮不改旧的

## 关键事实（已核实）

- 消费方只有两处：
  - 计 token：`ai-chat-service` 的 `server/app.go:176,183`、`server.go:247` 经 `services/tokenizer.GetTokens` → `/tokenizer/<model>`；另有 `ai-chat-backend` 同款调用
  - 语义：`ai-chat-service/chat-server/semcache/semcache.go` 的 `embedText`(L59)/`rerank`(L81) → `/embed`、`/rerank`
- `semcache.CacheQuery/CacheWrite` 被 `server.go` 在 4 处调用（CacheQuery@59/176、CacheWrite@130/300），签名不变则调用点零改动
- `dependOn.tokenizer.address` 同时被计 token 与语义使用，需拆分成 `tokenizer`（计 token）+ `semantic`（嵌入/rerank）两个 key
- 新端口 3003 空闲；compose 内服务以 Docker 主机名互访（tokenizer→`http://tokenizer:3002`，semantic→`http://semantic:3003`）
- 开发机无 GPU、4 核、7GB 内存、系统 Python 3.8；`tokenizer` 镜像基座为 `python:3.10-alpine`（nuxt 启动方式沿用）
- Docker daemon 本会话无权限、无本地 registry 可达 → **本轮不执行任何 docker build/push**，只产出 Dockerfile + compose 定义

## 明确不做（Out of scope）

- 不换 Embedding 模型、不改 256 维向量、不调阈值、不动 kvstore/向量记录格式
- 不实现 tmp 方案 §7.3 的 `/v1/parse` `/v1/embed/query` `/v1/embed/documents` `/v1/rerank` `/v1/equivalence` 新契约（留给后续换真模型时定）
- 不做 docker 镜像构建/推送、不新增独立 git 仓库/submodule
- 不改 compose 既有 5 个服务的镜像 registry（`239.161:5000`）
- 不实现 tmp 方案后续阶段（真 Embedding、稀疏/别名混合召回、Cross-Encoder、验证集阈值校准）

## 组件设计

### ① 新服务 `semantic/`（新增目录，镜像 tokenizer/ 布局）

- `semantic.py`：从 `tokenizer/tokenizer.py` **逐字搬迁**语义代码：
  - 常量与工具：`EMBED_DIM`、`_hash_bucket`、`SUBJECT_PATTERNS`、`normalize_subject`、`extract_subject`、`INTENT_RULES`、`extract_intent`、`CONTEXT_PATTERNS`、`is_context_dependent`、`STATEFUL_PATTERNS`、`is_stateful_instruction`、`should_bypass_semantic_cache`、`LANG_PATTERNS`、`extract_language`、`STOP_WORDS`、`_add`、`embed_text`、`intent_compatible`、`OPERATION_WORDS`、`extract_operation`、`_subject_conflict`、`rerank_score`
  - 路由：`/embed`（`get_embedding`）、`/rerank`（`get_rerank`）
  - 依赖：`nuxt>=0.2.0`、`jieba>=0.42.1`（**不含 tiktoken** —— 以此证明语义路径已与计 token 解耦）
- `requirements.txt`：仅 `nuxt`、`jieba`
- `Dockerfile`：仿 tokenizer/Dockerfile，`PORT 3003`，`CMD nuxt --port 3003 --module semantic.py --workers 2`
- `README.md`：构建（`docker build -t 192.168.233.128:5000/2404/semantic:1.0.0 .`）与启动说明

### ② `tokenizer/` 裁剪（删除语义职责）

- `tokenizer.py` 保留：`encoding_cache`、`support_models`、`/tokenizer/<model>` 路由、`num_tokens_from_messages`
- 删除：上列全部语义函数与 `/embed` `/rerank` 路由；`import jieba`、`import math`（若仅被语义代码使用）一并删除
- `requirements.txt`：删 `jieba`（只留 `nuxt`、`tiktoken`）
- 启动后 `/embed`、`/rerank` 应 404，`/tokenizer/<model>` 正常

### ③ HTTP 契约（不变）

- `POST /embed {text}` → `{code, embedding[256], bypass_cache, context_dependent, intent, subject}`（字段原样）
- `POST /rerank {query, cached_query}` → `{code, score, shared}`
- 行为确定性：代码逐字搬迁 + 同版本 jieba → 输出可逐字段字节级比对

### ④ Go 侧（ai-chat-service，行为不变）

- `pkg/config/config.go`：`DependOn` 增
  ```go
  Semantic struct { Address string }   // dependOn.semantic.address
  ```
  `Tokenizer` 保留给 `GetTokens`
- `chat-server/semcache/semcache.go`：`embedText`(L59) 与 `rerank`(L81) 的 `DependOn.Tokenizer.Address` → `DependOn.Semantic.Address`；其余（请求体、响应 struct、CacheQuery/CacheWrite、阈值判断）不动
- `services/tokenizer/tokenizer.go`（GetTokens）不动
- 配置 yaml 增 `dependOn.semantic.address`：
  - `ai-chat-service/dev.config.yaml` → `http://127.0.0.1:3003`
  - `ai-chat-stack/configs/ai-chat-service.yaml` → `http://semantic:3003`

### ⑤ 编排注册

- `lib.sh` `SERVICES` 追加（在 ai-chat-service 之前）：
  ```
  "semantic|3003|$BASE/semantic|nuxt --port 3003 --module semantic.py --workers 2"
  ```
- `ai-chat-stack/compose.yaml` 增 semantic 服务：镜像 `192.168.233.128:5000/2404/semantic:1.0.0`，replicas 2、vip、start-first，command `nuxt --port 3003 --module semantic.py --workers 2`，环境 `PORT: 3003`

### ⑥ 文档同步（仓库内真相源）

- `docs/项目文档/04-业务应用.md`：
  - `§4.2` 语义缓存流程：`/embed`、`/rerank` 所指服务改为 semantic（tokenizer → semantic）
  - `§6` 拆成两节/两服务描述：tokenizer（计 token）与 semantic（嵌入+rerank 规则校验）
  - `§11.7` 误命中治理中的代码位置、附录源码索引补 semantic
- `tokenizer/README.md` 若提及 /embed /rerank 则删除
- 新增 `semantic/README.md`

## 验证策略

**金样差分比对**（证明"行为不变"），不靠肉眼：

1. **录金样（改 tokenizer 前）**：构造 corpus（20-30 条 query，覆盖实现/定义/操作/语言/上下文依赖/状态修改/中英/通用 how-to；另配 query↔candidate 对），curl `tokenizer:3002` 的 `/embed`、`/rerank`，存 JSON 为 golden 文件
2. **起 semantic:3003**：同一 corpus 请求同一路径，逐字段 diff → 须完全一致（fnv/余弦/jieba 均确定、无随机）。语义路径独立于 tokenizer 跑通即已证解耦
3. **裁剪后 tokenizer**：重启确认 `/tokenizer/<model>` 正常、`/embed` `/rerank` 404
4. **Go 侧**：`go build` 通过；`./start.sh` 全栈起；e2e——发消息→缓存写入→语义近似改写命中（秒回、source=cache、不扣额度）；语言冲突对不误命中（复测 tmp 方案 §20 验收用例语义）
5. **计 token 不回归**：正常对话额度扣减与 tokensUsed 与改造前一致

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 差分不一致 | 金样定位差异源（多为迁移误改），金样文件保留可反复对 |
| Go 配置漏改致 /embed 打到 tokenizer | build + e2e 必覆盖；semantic 不可用时 `CacheQuery` 现容错路径 `return "",false`（优雅 miss） |
| 本地进程/端口混淆（新旧服务同跑） | 停服务用根 `stop.sh`/`start.sh`；精确杀进程用 `pkill -x`（`pkill -f` 有 shell 自杀坑） |
| Docker 镜像/registry 不可达 | 本轮不 build/push；本地验证全走 lib.sh 进程 |

回滚：revert 该提交即回到"语义在 tokenizer 内"的旧拓扑；缓存数据（kvstore）不受本轮影响，无需迁移。

## 验收标准

1. `semantic:3003` 对 golden corpus 的 `/embed`、`/rerank` 输出与改造前 tokenizer 逐字段一致
2. `tokenizer:3002` 仅剩 `/tokenizer/<model>`；`/embed`、`/rerank` 404；tokenizer requirements 无 jieba
3. `semantic/` 无 tiktoken 依赖（requirements 只有 nuxt+jieba）
4. Go build 通过；本地全栈启动后：语义近似改写命中缓存（source=cache）、语言冲突不误命中、计 token/额度不回归
5. `docs/项目文档/04-业务应用.md` 与 `lib.sh`/compose 已同步；semantic/README 与 Dockerfile 就绪（含 `192.168.233.128:5000` 打标说明）

## 涉及文件清单

新增：
- `semantic/semantic.py`
- `semantic/requirements.txt`
- `semantic/Dockerfile`
- `semantic/README.md`

修改：
- `tokenizer/tokenizer.py`、`tokenizer/requirements.txt`（裁剪语义）
- `ai-chat-service/pkg/config/config.go`（DependOn.Semantic）
- `ai-chat-service/chat-server/semcache/semcache.go`（改指 Semantic.Address）
- `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`（semantic.address）
- `lib.sh`（SERVICES 加 semantic）
- `ai-chat-stack/compose.yaml`（semantic 服务块）
- `docs/项目文档/04-业务应用.md`、`tokenizer/README.md`（文档同步）
