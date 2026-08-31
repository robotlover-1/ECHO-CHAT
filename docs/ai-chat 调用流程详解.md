# ai-chat 调用流程详解（图文版·已更新）

> 画图语法：Mermaid，支持 VSCode 预览（`.md` 预览右上角"打开预览"）或 GitHub 自动渲染。
> 本版反映当前系统：登录鉴权 + 额度计费 + 本地语义知识库（kvstore VSEARCH）+ 真实大模型（DeepSeek）。
> 更新日期：2026-08-16

---

## 0. 端口 / 协议速查表


| 环节                                  | 端口  | 协议       | 调用方 → 被调方                        |
| ------------------------------------- | ----- | ---------- | --------------------------------------- |
| 前端页面 + 聊天接口                   | 7080  | HTTP       | 浏览器 → ai-chat-backend               |
| 登录/验证码/会话                      | 7080  | HTTP       | 浏览器 → ai-chat-backend               |
| 主业务服务                            | 50055 | gRPC(流式) | ai-chat-backend → ai-chat-service      |
| 敏感词过滤                            | 50053 | gRPC       | ai-chat-service → keywords-filter      |
| 关键词抽取                            | 50054 | gRPC       | ai-chat-service → keywords-filter      |
| token 计数 / 嵌入 / rerank            | 3002  | HTTP       | ai-chat-service → tokenizer            |
| 大模型代理                            | 8084  | HTTP       | ai-chat-service → openai-api-proxy     |
| 真实大模型                            | 443   | HTTPS      | openai-api-proxy → DeepSeek API        |
| 多轮上下文 / 会话 / 验证码 / 语义缓存 | 5160  | RESP       | ai-chat-service / backend →**kvstore** |
| 对话记录 / 用户额度                   | 3306  | MySQL      | ai-chat-service / backend → MySQL      |
| 监控指标                              | 8080  | HTTP       | ai-chat-service(Prometheus)             |
| Grafana 看板                          | 3000  | HTTP       | 浏览器 → Grafana                       |
| Prometheus                            | 9090  | HTTP       | 浏览器 → Prometheus                    |

---

## 1. 总览大图（一次对话的完整骨架）

```mermaid
flowchart TB
    subgraph 客户端
        A["浏览器<br/>Vue3 前端(chatgpt-web)<br/>端口 :7080"]
    end

    subgraph 网关层
        B["ai-chat-backend (Gin HTTP)<br/>- 托管前端 + 登录/验证码<br/>- 入口限流 + 鉴权 + 计费<br/>- POST /api/chat-process"]
    end

    subgraph 核心编排
        C["ai-chat-service<br/>gRPC 服务端 :50055<br/>(同时是下游 gRPC/HTTP 客户端)"]
    end

    subgraph 过滤层
        D["sensitive 敏感词<br/>gRPC :50053"]
        E["keywords 关键词<br/>gRPC :50054<br/>(现用于 rerank 校验)"]
    end

    subgraph 数据层
        KV["kvstore :5160<br/>自研 Redis<br/>- 会话 / 验证码<br/>- 多轮上下文<br/>- 语义缓存(VSEARCH)"]
        M["MySQL :3306<br/>- users(phone, quota)<br/>- chat_records"]
        T["tokenizer :3002<br/>token 计数 / jieba 嵌入<br/>/embed + /rerank"]
    end

    subgraph 大模型层
        P["openai-api-proxy :8084<br/>鉴权 + 限流 + 转发"]
        RL["DeepSeek API<br/>(deepseek-v4-flash)"]
    end

    A -- "⓪ 登录: /v1/sms/send/code<br/>/v1/user/login → access_token" --> B
    A -- "① POST /api/chat-process<br/>(Bearer token)" --> B
    B -- "② 限流10/s → 鉴权查 kvstore<br/>session → 计费预检 quota>0" --> KV
    B -- "③ gRPC 流式 ChatCompletionStream" --> C
    C -- "④ 敏感词校验 Validate" --> D
    C -- "⑤ 语义缓存: /embed 嵌入<br/>→ VSEARCH 余弦<br/>→ /rerank 共享关键词" --> T
    C -- "⑤' VSEARCH 查 semcache" --> KV
    C -- "⑥ 上下文读写 GET/SETEX" --> KV
    C -- "⑦ token 预算裁剪" --> T
    C -- "⑧ 调大模型(流式)<br/>POST /v1/chat/completions" --> P
    P -- "⑨ 转发" --> RL
    C -- "⑩ 异步: 存上下文/写语义缓存<br/>写 chat_records" --> KV
    C -- "⑩' 异步落库" --> M
    C -- "⑪ 流式回复(gRPC 流)" --> B
    B -- "⑫ 计费扣减(打完算 token)" --> M
    B -- "⑬ 换行JSON流(NDJSON)" --> A
```

---

## 2. 完整时序图（含登录 + 语义缓存分支）

```mermaid
sequenceDiagram
    autonumber
    participant FE as 浏览器前端
    participant BE as ai-chat-backend<br/>(Gin :7080)
    participant KV as kvstore :5160
    participant SVC as ai-chat-service<br/>(gRPC :50055)
    participant SEN as sensitive<br/>(gRPC :50053)
    participant TK as tokenizer :3002
    participant PX as openai-api-proxy :8084
    participant DS as DeepSeek API
    participant MYSQL as MySQL :3306

    Note over FE,MYSQL: ==== 登录阶段（一次性） ====
    FE->>BE: ① POST /v1/sms/send/code {phone}
    BE->>KV: ② SETEX sms_code: 5min
    FE->>BE: ③ POST /v1/user/login {phone, code}
    BE->>KV: ④ 校验验证码 + 写 session:
    KV-->>BE: 验证码正确
    BE->>MYSQL: ⑤ upsert users(quota=100000)
    BE-->>FE: ⑥ access_token

    Note over FE,MYSQL: ==== 聊天阶段 ====
    FE->>BE: ⑦ POST /api/chat-process (Bearer token, prompt)
    BE->>KV: ⑧ 限流 + 验 session:
    KV-->>BE: phone
    BE->>MYSQL: ⑨ 计费预检 quota>0? (否则402)
    BE->>SVC: ⑩ gRPC ChatCompletionStream
    SVC->>SEN: ⑪ Validate(消息)
    alt 敏感词命中
        SVC-->>BE: ⑫ 流式返回「触发到了知识盲区」
    else 通过
        SVC->>TK: ⑬ /embed 嵌入问题(256维)
        TK-->>SVC: 向量
        SVC->>KV: ⑭ VSEARCH 余弦 top-k
        KV-->>SVC: (key, score)
        alt 语义缓存命中(score>0.15 且共享关键词)
            SVC-->>BE: ⑮ 直接流式返回缓存答案(不调大模型)
        else 未命中
            SVC->>KV: ⑯ GET 上下文历史(PID链, 若续聊)
            SVC->>TK: ⑰ token 预算裁剪
            SVC->>PX: ⑱ POST /v1/chat/completions(stream)
            PX->>DS: ⑲ 转发
            DS-->>PX: ⑳ 流式分片
            PX-->>SVC: 分片
            SVC-->>BE: ㉑ gRPC 分片逐块转发
        end
    end
    BE-->>FE: ㉒ NDJSON 流(逐字渲染)
    Note over BE,MYSQL: ㉓ 聊天后: 计费扣减 + 异步收尾
    BE->>MYSQL: ㉔ 按 prompt+回答 token 扣 quota
    SVC->>KV: ㉕ SETEX 上下文(1800s) + HSET 语义缓存
    SVC->>MYSQL: ㉖ 写 chat_records
```

---

## 3. 分步详解

### 3.0 登录（新增，前端 → backend → kvstore/MySQL）

```mermaid
flowchart LR
    U["打开页面"] --> A["POST /api/session<br/>→ auth:true<br/>（登录功能开启）"]
    A --> B["弹登录框: 输手机号"]
    B --> C["POST /v1/sms/send/code<br/>验证码存 kvstore(sms_code: 5min)<br/>+ 打印到后端日志"]
    C --> D["输验证码 → POST /v1/user/login"]
    D --> E["校验 kvstore 验证码<br/>→ upsert MySQL users(quota=100000)<br/>→ 写 kvstore session:<token>(7天)"]
    E --> F["返回 access_token<br/>→ 前端存 SECRET_TOKEN → 进聊天页"]
```

- 前端 `needPermission = session.auth && !token`：auth 开启且无 token → 弹登录框
- 验证码本地无短信商，打印到 `ai-chat-backend/runtime/logs/app.log`

### 3.1 前端发消息（浏览器 → backend）

```mermaid
flowchart LR
    U["用户在输入框打字, 回车"] --> V["前端 api/index.ts<br/>组装请求体"]
    V --> W["POST /api/chat-process<br/>Authorization: Bearer access_token<br/>Content-Type: application/json"]
    W --> X["请求体:<br/>{prompt, options:{parentMessageId}}"]
    X --> Y["ai-chat-backend :7080"]
```

- `parentMessageId` 为空 = 新对话；有值 = 多轮续聊
- Authorization 头携带登录拿到的 access_token

### 3.2 backend 协议转换（限流 → 鉴权 → 计费 → gRPC）

```mermaid
flowchart TB
    A["ai-chat-backend 收到 /api/chat-process"]
    B["入口限流: 10/s burst 10<br/>超限返回 429"]
    C["鉴权中间件: 查 kvstore<br/>session:<token> → 拿到 phone"]
    D["计费预检: users.quota > 0?<br/>否则 402「额度不足」"]
    E["BindJSON 解析 prompt / parentMessageId"]
    F["从连接池取 gRPC 连接"]
    G["构造 proto.ChatCompletionRequest<br/>Id=新uuid, Message=prompt, Pid,<br/>EnableContext=(Pid!=''), ChatParam=模型参数"]
    H["调 ai-chat-service ChatCompletionStream"]
    I["循环 stream.Recv() 收分片<br/>→ 转 JSON → 写一行+'\\n' → Flush"]
    A --> B --> C --> D --> E --> F --> G --> H --> I
```

- 限流/鉴权/计费都在 backend（HTTP 网关层），请求到 ai-chat-service 前已完成
- 鉴权是**每次请求**验凭证 + 认人（计费要扣到具体 phone）

### 3.3 ai-chat-service 入口（鉴权 + 敏感词）

```mermaid
flowchart LR
    A["gRPC 请求到达 :50055"] --> B["interceptor/auth.go<br/>校验 Bearer token<br/>(=配置的 accessToken)"]
    B --> C["server.go ChatCompletionStream"]
    C --> D["newApp(in, contextCache)<br/>合并默认参数与请求参数"]
    D --> E["app.sensitive(in) 敏感词校验<br/>👉 命中直接回固定文案"]
```

### 3.4 敏感词（内容安全）

```mermaid
flowchart TB
    A["app.sensitive(in)"] --> B{"gRPC Validate(消息)<br/>→ sensitive :50053"}
    B -- "命中" --> C["流式返回「触发到了知识盲区」<br/>🚫 不调大模型, 不落库"]
    B -- "通过" --> D["👉 3.5 语义缓存"]
```

> 词库：sensitive 用 `dict.txt`（50053），keywords 用 `keyword-dict.txt`（50054），AC 自动机。

### 3.5 语义缓存（kvstore VSEARCH + jieba，命中就"抄答案"）

```mermaid
flowchart TB
    A["semcache.CacheQuery(问题)"] --> B["/embed 把问题嵌入成 256 维<br/>（jieba 分词 + n-gram 哈希）"]
    B --> C["VSEARCH 256 <向量> 5<br/>→ kvstore 遍历 semcache: 条目算余弦"]
    C --> D{"score > 0.15 ?"}
    D -- "否" --> E["👉 3.6 正常走大模型"]
    D -- "是" --> F["/rerank 校验: 问题与缓存问题<br/>是否共享 ≥1 个实质关键词<br/>（allowPOS 取名词/动词）"]
    F -- "不共享" --> E
    F -- "共享" --> G["HGET semcache:<key><br/>→ 解出缓存答案"]
    G --> H["直接流式返回缓存答案<br/>✅ 秒回、不调大模型"]
```

- 缓存条目 `semcache:<fnv32哈希>`，HSET 存二进制记录 `{问题, 答案, 256维向量}`
- **共享关键词门禁**防误命中：jieba 稀疏嵌入下无关问题可能分数反超，须"分数达标 + 共享实词"双条件
- 聊天后异步把本轮问答写入缓存（CacheWrite）

### 3.6 构建上下文 + token 预算（kvstore + tokenizer）

```mermaid
flowchart TB
    A["buildChatCompletionRequest()"] --> B{"EnableContext?<br/>(多轮续聊)"}
    B -- "是" --> C["getContext(pid)<br/>从 kvstore 按消息ID链取历史<br/>(最多 context_len=4 条, GET)"]
    B -- "否" --> D["无历史, 只带当前消息"]
    C --> D
    D --> E["调 tokenizer :3002 数 token:<br/>人设bot_desc + 当前消息 + 每条历史"]
    E --> F{"当前消息超预算?<br/>max_tokens - min_response_tokens - 人设"}
    F -- "超了" --> G["拒绝: 「请求消息超限」"]
    F -- "没超" --> H["从最近到最远塞历史<br/>塞不下就丢更早的 → 最终 Messages"]
    H --> I["👉 3.7 调大模型"]
```

> 上下文 = 当前对话的**短期记忆**（存 kvstore，30 分钟 TTL）；语义缓存 = 跨对话的**问答复用**（长期）。

### 3.7 调大模型（真实 DeepSeek）

```mermaid
flowchart TB
    A["getOpenaiClient()<br/>go-openai<br/>base_url = http://localhost:8084/v1"]
    A --> B["CreateChatCompletionStream(req)<br/>POST /v1/chat/completions, stream=true"]
    B --> C["openai-api-proxy :8084<br/>中间件: 鉴权 + 限流(10/s)"]
    C --> D["ReverseProxy 转发到 base_url<br/>= https://api.deepseek.com/v1"]
    D --> E["DeepSeek API<br/>model: deepseek-v4-flash<br/>流式返回"]
    E --> F["响应回流<br/>DeepSeek → proxy → go-openai → server.go"]
    F --> G["server.go 把 OpenAI 分片<br/>转成 proto 分片, 逐块 Send()"]
```

- 模型名 `deepseek-v4-flash`（proxy 的 `chat.api_keys` 是真实 key）
- 之前 mock(8083) 已废弃；tokenizer 需把模型加进 `support_models` 才能数 token

> **openai-api-proxy 的角色定位**
> 单实例本地部署下，proxy 功能上**冗余**（ai-chat-service 改 `base_url` 就能直连 DeepSeek，backend 已做了用户鉴权+限流，proxy 的静态 token 鉴权与 10/s 限流只是第二层）。但它的价值：
> - **集中管真实 key**：真 DeepSeek key 只在 proxy 配置，ai-chat-service 用假 token，服务不碰真 key（"钥匙保管箱"）
> - **供应商适配层**：换模型/厂商只改 proxy 的 `base_url`+`api_keys`，编排服务零改动（mock → DeepSeek → 将来其他）
> - **扩展挂载点**：将来可加重试、超时、多 key 轮换、按模型成本统计、调用日志，都不用动 service
> - **LLM 调用边界兜底**：就算 backend 限流被绕过，proxy 在"花钱的 API 调用"这层再兜一道

### 3.8 流式回复回到客户端

```mermaid
flowchart LR
    A["ai-chat-service 每收一个分片<br/>立即 stream.Send(proto分片)"] --> B["backend stream.Recv()"]
    B --> C["包装成 ChatMessage<br/>{id, delta, text(累计), detail}"]
    C --> D["json.Marshal → 写一行 → '\\n' → Flush"]
    D --> E["浏览器逐行解析 NDJSON<br/>追加渲染, 打字机效果"]
```

### 3.9 计费 + 异步收尾（不阻塞回复）

```mermaid
flowchart TB
    A["聊天流结束后"] 
    A --> B0["计费(backend): tokenizer 数<br/>prompt+回答 token → 扣 users.quota"]
    A --> B["① 上下文存 kvstore<br/>请求对+回答对 各自 SETEX<br/>key=ai_chat_service_<id>, TTL=1800s"]
    A --> C["② 语义缓存写 kvstore<br/>HSET semcache:<hash><br/>={问题, 答案, 256维向量}"]
    A --> D["③ 对话记录存 MySQL chat_records<br/>user_msg / ai_msg / token数<br/>/ 关键词 / 时间"]
```

- 上下文/语义缓存/记录各自独立异步，互不影响
- **kvstore 管临时（会话/上下文/缓存），MySQL 管永久（用户额度/聊天历史）**

---

## 4. 回答路径速览（用户视角）


| 路径            | 触发条件                    | 耗时/成本  | 走过哪些节点                                                                             |
| --------------- | --------------------------- | ---------- | ---------------------------------------------------------------------------------------- |
| ① 敏感词拦截   | 消息命中敏感词              | 最快       | 前端→backend→service→sensitive→回                                                    |
| ② 语义缓存命中 | score>0.15 且共享实质关键词 | 秒回、免费 | 前端→backend→service→embed→VSEARCH→rerank→回                                       |
| ③ 大模型生成   | 都没命中                    | 慢、花钱   | 前端→backend→(限流/鉴权/计费)→service→(kvstore上下文/tokenizer)→proxy→DeepSeek→回 |

---

## 5. kvstore 与 MySQL 的分工


|            | kvstore :5160                           | MySQL :3306             |
| ---------- | --------------------------------------- | ----------------------- |
| 验证码     | `sms_code:<phone>`（5 分钟）            | —                      |
| 会话       | `session:<token>`（7 天）               | —                      |
| 多轮上下文 | `ai_chat_service_<id>`（30 分钟）       | —                      |
| 语义缓存   | `semcache:<hash>`（长期，HSET+VSEARCH） | —                      |
| 用户额度   | —                                      | `users`(phone, quota)   |
| 聊天历史   | —                                      | `chat_records`（永久）  |
| 特性       | 内存 + AOF，快、可过期                  | 磁盘，永久、可 SQL 查询 |

- 语义缓存和聊天历史**都每次对话写**，但前者是"机器检索的问答对+向量"（加速），后者是"人查的完整记录"（留痕）
