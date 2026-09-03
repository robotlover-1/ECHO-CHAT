# semantic 独立服务解耦设计文档（语义检索从 tokenizer 拆分 · Phase 0）

- 日期：2026-09-03
- 状态：已批准（吸收评审意见修订后）
- 位置：ECHO-CHAT monorepo（`semantic/` 新服务 + `tokenizer/` 裁剪 + `ai-chat-service` 指路 + 编排/文档）
- 关联：`proj/tmp/ECHO-CHAT语义检索高命中率改造方案.md`（仓外草稿，远期蓝图）与 `proj/tmp/ECHO-CHAT语义服务解耦方案评审与修改建议 (1).md`（评审）
- 定位：tmp 方案的 **Phase 0：服务解耦**。只改善架构、不直接改善检索准确率；"红黑树≈rbtree、C≠C++"需在后续 Phase 1（别名/硬约束）实现

## 背景与目标

当前 `tokenizer/`（Python nuxt 服务，:3002）身兼三职：

1. `/tokenizer/<model>` —— tiktoken 计 LLM token（**tokenizer 本职**，保留）
2. `/embed` —— 256 维 FNV 哈希加权"词面嵌入"+ 主题/意图/上下文依赖/状态修改抽取
3. `/rerank` —— 规则启发式硬拒（主题/意图/语言/操作冲突）+ 关键词 Jaccard

语义检索逻辑物理上被塞在 tokenizer 服务里，与计 token 职责耦合。本轮目标：**架构解耦、语义行为与检索效果与拆分前完全一致**——把语义逻辑抽到 monorepo 内新增的独立服务 `semantic/`，tokenizer 回归纯计 token。后续换真模型/混合检索在此服务内演进，不影响计 token 链路。

## 评审修订记录（2026-09-03）

| # | 评审意见 | 处置 |
|---|---|---|
| 3.1 | "行为不变"与高命中率验收用例矛盾 | 采纳：验收拆 A 阻断（新=旧输出一致）/ B 非阻断基线（§验证策略、§验收标准） |
| 3.2 | "逐字节一致"标准不严谨 | 采纳：改 JSON 字段级 + 浮点容差 `<1e-7` 比较 |
| 3.3 | `>=` 依赖无法复现 | 采纳：锁 `nuxt==0.2.15`、`jieba==0.42.1`、`tiktoken==0.7.0`（本机已装版本，实施期以金样实测为准） |
| 3.4 | Go→semantic 无超时/无 ctx 取消 | 采纳：共享 Client `Timeout=1.5s` + `NewRequestWithContext(ctx,…)` + 失败即 cache miss 降级 |
| 3.5 | semantic 无健康检查 | 采纳：`GET /healthz` + compose healthcheck |
| 3.6 | registry 地址不一致 | 采纳：compose 镜像用 `${SEMANTIC_IMAGE:-192.168.233.128:5000/2404/semantic:1.0.0}`，注明默认仅限本开发机 |
| 4.1 | 提前提供 `/v1/embed` `/v1/rerank` 版本化别名 | 不采纳（本轮）：与既定"沿用现有契约"冲突；真模型落地时 `/v1/*` 契约形态未定，过早加别名易误导。记为 defer |
| 4.2 | 拆多模块避免 semantic.py 堆大 | 不采纳（本轮）：逐字搬迁+金样保证要求最小 diff；nuxt 跨模块注册路由需额外验证、引入风险。记为 defer——真模型换契约时随 `/v1/*` 一并模块化 |
| 4.3 | 结构化错误日志 | 采纳（最小集）：semantic 调用失败路径记标签日志（§错误处理） |
| 4.4 | 嵌入元信息 | 采纳：经 `/healthz` 暴露 `embedding_type/dimension/parser_version`；不改 `/embed` 响应结构 |
| 5.4 | 并发与资源测试 | 采纳（轻量可选）：4 核/7GB 上短测 1/10/50 并发，记录延迟与错误率 |

## 已确认决策

- **范围**：仅解耦，语义行为/检索效果不变（嵌入仍 256 维 FNV 哈希、阈值 0.35/0.25 不动、kvstore VSEARCH 与向量记录格式不动、`CacheQuery/CacheWrite` 签名不动）
- **位置**：ECHO-CHAT monorepo 新增 `semantic/`，与 `tokenizer/` 平级、独立端口 3003、自带 requirements/Dockerfile/README/tests
- **HTTP 契约**：新服务照搬现有 `/embed`、`/rerank` 路由与请求/响应结构；`semcache.go` 的 `embedResp`/`rerankResp` 解析零改动
- **超时**：Go→semantic 请求整体超时 1.5s、绑定 ctx；失败→快速 cache miss（聊天继续走 LLM）
- **依赖锁定**：`nuxt==0.2.15`、`jieba==0.42.1`、`tiktoken==0.7.0`
- **镜像地址**：`${SEMANTIC_IMAGE:-192.168.233.128:5000/2404/semantic:1.0.0}`（本机 registry 默认；compose 既有 5 服务仍 `239.161:5000`，本轮不改旧的）
- 本轮**不执行任何 docker build/push**（本机 docker daemon 无权限、registry 不可达）；只产出 Dockerfile + compose 定义

## 关键事实（已核实）

- 消费方只有两处：
  - 计 token：`ai-chat-service` 的 `server/app.go:176,183`、`server.go:247` 经 `services/tokenizer.GetTokens` → `/tokenizer/<model>`；另有 `ai-chat-backend` 同款调用
  - 语义：`ai-chat-service/chat-server/semcache/semcache.go` 的 `embedText`(L59)/`rerank`(L81) → `/embed`、`/rerank`
- `semcache.CacheQuery/CacheWrite` 被 `server.go` 在 4 处调用（CacheQuery@59/176、CacheWrite@130/300），签名不变则调用点零改动
- `dependOn.tokenizer.address` 同时被计 token 与语义使用，需拆成 `tokenizer`（计 token）+ `semantic`（嵌入/rerank）两个 key
- 新端口 3003 空闲；compose 内服务以 Docker 主机名互访（tokenizer→`http://tokenizer:3002`，semantic→`http://semantic:3003`）
- 开发机 192.168.233.128、无 GPU、4 核、7GB、系统 Python 3.8；`tokenizer` 镜像基座 `python:3.10-alpine`（nuxt 启动方式沿用）
- 本机实测已装版本：`nuxt 0.2.15` / `jieba 0.42.1` / `tiktoken 0.7.0`
- `semcache` 当前调用无超时、无 ctx 绑定（`http.Post`）；`services/tokenizer`(GetTokens) 同款（本轮不动，仅语义路径加）

## 明确不做（Out of scope）

- 不换 Embedding 模型、不改 256 维向量、不调阈值、不动 kvstore/向量记录格式（Phase 1+ 再做）
- 不实现 tmp 方案 `/v1/parse` `/v1/embed/query` `/v1/embed/documents` `/v1/rerank` `/v1/equivalence` 新契约
- 不提供 `/v1/embed`、`/v1/rerank` 版本化别名（defer，见评审记录）
- 不做 semantic.py 模块拆分（defer，见评审记录）
- 不做 docker 镜像构建/推送、不新增独立 git 仓库/submodule
- 不改 compose 既有 5 个服务的镜像 registry（`239.161:5000`）
- 不承担高命中率目标（rbtree↔红黑树、C≠C++ 全形态）——仅作为非阻断基线记录

## 组件设计

### ① 新服务 `semantic/`（新增目录，镜像 tokenizer/ 布局）

```text
semantic/
├── semantic.py          # 逐字搬迁的语义代码 + /embed /rerank /healthz 路由
├── tests/
│   ├── golden_cases.json   # 金样 corpus + 拆分前旧输出（由实施期录制）
│   └── test_golden.py      # 字段级差分脚本（读 golden，比对 live semantic 输出）
├── requirements.txt     # nuxt==0.2.15 / jieba==0.42.1（无 tiktoken）
├── Dockerfile           # python:3.10-alpine, PORT=3003
└── README.md            # 构建/启动/healthz 说明
```

`semantic.py` 从 `tokenizer/tokenizer.py` **逐字搬迁**：`EMBED_DIM`、`_hash_bucket`、`SUBJECT_PATTERNS`、`normalize_subject`、`extract_subject`、`INTENT_RULES`、`extract_intent`、`CONTEXT_PATTERNS`、`is_context_dependent`、`STATEFUL_PATTERNS`、`is_stateful_instruction`、`should_bypass_semantic_cache`、`LANG_PATTERNS`、`extract_language`、`STOP_WORDS`、`_add`、`embed_text`、`intent_compatible`、`OPERATION_WORDS`、`extract_operation`、`_subject_conflict`、`rerank_score`，及 `/embed`（`get_embedding`）、`/rerank`（`get_rerank`）。

`semantic.py` 无 tiktoken 依赖，**以依赖消失证明语义与计 token 解耦**。

**`GET /healthz`**（新增，评审 3.5/4.4）：

```json
{ "status": "ok", "service": "semantic",
  "embedding_type": "fnv_hash", "dimension": 256, "parser_version": "v1" }
```

不改 `/embed` 响应结构（契约不变；元信息走 /healthz）。

### ② `tokenizer/` 裁剪（删除语义职责）

- `tokenizer.py` 保留：`encoding_cache`、`support_models`、`/tokenizer/<model>` 路由、`num_tokens_from_messages`
- 删除全部语义函数与 `/embed` `/rerank` 路由；`import jieba`、`import math`（仅语义代码用则删）
- `requirements.txt`：删 `jieba`，改锁 `nuxt==0.2.15`、`tiktoken==0.7.0`
- 启动后 `/embed`、`/rerank` 404，`/tokenizer/<model>` 正常

### ③ HTTP 契约（不变）

- `POST /embed {text}` → `{code, embedding[256], bypass_cache, context_dependent, intent, subject}`
- `POST /rerank {query, cached_query}` → `{code, score, shared}`
- 验收以**字段级 + 浮点容差**比较（评审 3.2），非字节级

### ④ Go 侧（ai-chat-service）

- `pkg/config/config.go`：`DependOn` 增 `Semantic struct { Address string }`（`dependOn.semantic.address`）；`Tokenizer` 保留给 GetTokens
- `chat-server/semcache/semcache.go`：
  - `embedText`/`rerank` 的地址 `DependOn.Tokenizer.Address` → `DependOn.Semantic.Address`
  - **共享 HTTP client**：`&http.Client{ Timeout: 1500 * time.Millisecond }`
  - **ctx 绑定**：`http.NewRequestWithContext(ctx, POST, endpoint, body)` + `Content-Type`；`embedText`/`rerank` 现有 `ctx` 参数接入
  - **失败即降级**：调用失败/超时/非 200/非法 JSON/维度错 → 记标签日志 → `CacheQuery` 走现有 `return "",false`（cache miss，聊天继续 LLM）
  - 请求体、`embedResp`/`rerankResp`、`CacheQuery/CacheWrite` 判断逻辑、签名**不动**
- `services/tokenizer/tokenizer.go`（GetTokens）不动（计 token 链路零改动）
- 配置 yaml 增 `dependOn.semantic.address`：`ai-chat-service/dev.config.yaml`→`http://127.0.0.1:3003`；`ai-chat-stack/configs/ai-chat-service.yaml`→`http://semantic:3003`

### ⑤ 错误处理与日志（评审 4.3，最小集）

semantic 调用失败路径记标签日志，不含用户原文/凭据：

```text
semantic_unavailable   # 连接失败
semantic_timeout       # 超时
semantic_bad_status    # 非 200
invalid_json           # 非法 JSON
invalid_embedding_dimension  # 维度 ≠ 256
rerank_failed
```

### ⑥ 编排注册

- `lib.sh` `SERVICES` 追加（在 ai-chat-service 之前）：
  `"semantic|3003|$BASE/semantic|nuxt --port 3003 --module semantic.py --workers 2"`

- `ai-chat-stack/compose.yaml` 增 semantic 服务：镜像 `image: ${SEMANTIC_IMAGE:-192.168.233.128:5000/2404/semantic:1.0.0}`，replicas 2、vip、start-first，command `nuxt --port 3003 --module semantic.py --workers 2`，环境 `PORT: 3003`，healthcheck 用容器内 python 探 `/healthz`：

  ```yaml
  healthcheck:
    test: ["CMD", "python3", "-c",
           "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3003/healthz', timeout=2).status==200 else 1)"]
    interval: 5s
    timeout: 2s
    retries: 3
    start_period: 5s
  ```

### ⑦ 文档同步（仓库内真相源）

- `docs/项目文档/04-业务应用.md`：`§4.2` 语义缓存流程所指服务改 semantic；`§6` 拆成 tokenizer（计 token）与 semantic（嵌入+rerank）两节；`§11.7` 与附录源码索引补 semantic
- `tokenizer/README.md` 若提及 /embed /rerank 则删除
- 新增 `semantic/README.md`

## 验证策略

> 原则：**本轮不新增语义能力，只要求"拆分前=拆分后"完全一致**（评审 3.1/3.2）。

### A. 阻断性回归（必须通过）

1. **录金样（改 tokenizer 前）**：构造 corpus（§A.1）curl 现存 `tokenizer:3002` 的 `/embed`、`/rerank`，解析后存 `semantic/tests/golden_cases.json`
2. **起 semantic:3003**：`test_golden.py` 读 golden、请求新服务，**字段级差分**（评审 3.2）：

   ```python
   assert old["code"] == new["code"]
   assert old["bypass_cache"] == new["bypass_cache"]
   assert old["context_dependent"] == new["context_dependent"]
   assert old["intent"] == new["intent"]
   assert old["subject"] == new["subject"]
   assert len(old["embedding"]) == len(new["embedding"]) == 256
   assert all(abs(a-b) < 1e-7 for a, b in zip(old["embedding"], new["embedding"]))
   # /rerank：score 容差 <1e-7，shared 相等
   ```

3. **裁剪后 tokenizer**：重启确认 `/tokenizer/<model>` 正常、`/embed` `/rerank` 404、requirements 无 jieba
4. **依赖锁定验证**：`pip show` 核对 nuxt==0.2.15 / jieba==0.42.1 / tiktoken==0.7.0
5. **Go 侧**：`go build` 通过；本地 `./start.sh` 全栈起

### B. 端到端一致性 + 故障降级

1. **命中/拒绝判定一致**：取拆分前**已知会命中缓存**的近似问题对（实施期从线上行为录制），拆分后仍应命中（source=cache、秒回、不扣额度）；已知被拒的对（如语言冲突）仍应被拒走 LLM
2. **计 token 不回归**：正常对话额度扣减与 tokensUsed 与拆分前一致；tokenizer 故障不影响 semantic，反之亦然
3. **故障注入**（评审 5.3）：

   | 故障场景 | 预期 |
   |---|---|
   | semantic 未启动 | 快速 cache miss（≤超时），聊天继续走 LLM |
   | semantic 超时 | 1.5s 内 miss |
   | semantic 返回 500 | miss 并记 semantic_bad_status |
   | 非法 JSON / 维度 ≠256 | 拒绝结果并 miss |
   | tokenizer 正常、semantic 故障 | token 计数与额度正常 |
   | kvstore 不可用 | cache miss，聊天继续 |

4. **健康检查**：`GET /healthz` 200 且字段符合 §①

### C. 高命中率用例基线记录（非阻断，评审 3.1）

运行 tmp 方案 §20 类用例（rbtree↔红黑树、C↔C++、定义↔实现、插入↔删除、上下文依赖）并**只记录拆分前后结果一致**，不要求命中。产出基线留待 Phase 1+ 对照。

### D. 并发/资源（轻量可选，评审 5.4）

4 核/7GB 上对 `/embed`、`/rerank` 短测并发 1/10/50、每组 ~30s，记录 P50/P95/P99、错误率、CPU/内存、超时数；仅记录不设闸。

## 验收标准

### 阻断（必须通过）

1. semantic 的 `/embed`、`/rerank` 解析后字段与旧 tokenizer 一致（容差 `<1e-7`）
2. tokenizer 仅剩 `/tokenizer/<model>`；旧语义路由不可访问；tokenizer 不再依赖 jieba
3. semantic 不依赖 tiktoken；`nuxt`/`jieba`/`tiktoken` 为锁定版本
4. Go build 通过；Go→semantic 带 ctx + 明确超时；semantic 不可用时快速降级 cache miss、聊天不受影响
5. `/healthz` 正常；compose 含 healthcheck 定义与 `${SEMANTIC_IMAGE}` 可覆盖
6. 本地全栈缓存写入/读取正常；拆分前能命中的近似问题仍命中、被拒对仍被拒
7. token 计数、tokensUsed、额度无回归
8. 文档、lib.sh、compose、config 同步完成

### 非阻断（本轮仅记录基线，不要求达标）

rbtree↔红黑树 命中、C↔C++ 全形态区分、真实 Embedding、混合召回、Cross-Encoder、阈值校准（Phase 1+）

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 差分不一致 | 金样定位差异源（多为迁移误改）；golden_cases.json 保留可反复对 |
| semantic 卡死/故障拖慢聊天 | 1.5s 超时 + ctx + 失败 cache miss（评审 3.4） |
| Go 配置漏改致 /embed 打到 tokenizer | build + e2e + 故障注入必覆盖 |
| 本地新旧进程端口混淆 | 停用根 `stop.sh`/`start.sh`；精确杀进程用 `pkill -x`（`pkill -f` 有 shell 自杀坑） |
| registry 地址在别节点拉不到 | `${SEMANTIC_IMAGE}` 覆盖 + 文档注明默认仅限本开发机（评审 3.6） |
| Docker 镜像/registry 不可达 | 本轮不 build/push；本地验证全走 lib.sh 进程 |

回滚：revert 该提交即回到"语义在 tokenizer 内"的旧拓扑；缓存数据（kvstore）不受影响、无需迁移。

## 涉及文件清单

新增：

- `semantic/semantic.py`、`semantic/requirements.txt`、`semantic/Dockerfile`、`semantic/README.md`
- `semantic/tests/golden_cases.json`、`semantic/tests/test_golden.py`

修改：

- `tokenizer/tokenizer.py`、`tokenizer/requirements.txt`（裁剪语义 + 锁版本）
- `ai-chat-service/pkg/config/config.go`（DependOn.Semantic）
- `ai-chat-service/chat-server/semcache/semcache.go`（指 Semantic.Address + ctx/超时/降级/日志）
- `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`（semantic.address）
- `lib.sh`（SERVICES 加 semantic）
- `ai-chat-stack/compose.yaml`（semantic 服务 + healthcheck）
- `docs/项目文档/04-业务应用.md`、`tokenizer/README.md`（文档同步）
