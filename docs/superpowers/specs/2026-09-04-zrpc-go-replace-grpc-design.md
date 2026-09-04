# ECHO-CHAT gRPC 换 zrpc-Go：自研 RPC 替换 gRPC 设计文档（v2）

- 日期：2026-09-04
- 状态：**有条件通过（P0 修订已完成，可进入实施规划）**
- 位置：ECHO-CHAT monorepo（`t1/ECHO-CHAT`）
- 参考：`proj/tmp/zrpc-main.zip`（C 版 zrpc 教学骨架——只借"注册式方法表 + CRC/长度帧 + JSON"的思想，**不移植代码、不与之互操作**；C 源码无许可证，Go 实现独立编写）
- 评审：`proj/tmp/t1/2026-09-04-zrpc-go-replace-grpc-review.md`（本版已吸收）
- 关联：`docs/superpowers/specs/` 系列既有 spec

## 背景、动机与决策门槛

ECHO-CHAT 三个独立 Go 模块经 gRPC 互连：ai-chat-backend → ai-chat-service（unary + 服务端流式）、ai-chat-service → keywords-filter（两类 unary）。本任务以**自研 Go RPC** 替换这三条链路。zrpc 仅作设计参考（帧 + 注册式方法表），**非**字节互操作目标。

动机定位为**学习 + 通信栈自主可控**（非延迟/吞吐类性能诉求）。因此设定决策门槛（阶段 0）：先建 gRPC 基线并定义收益目标；若自研实现无可测收益且无自主可控诉求，则保留 gRPC。允许以"受控试点 + 双栈可回退"方式推进，不直接全量删除 gRPC。

明确排除：不改前端契约、不改 OpenAI 契约、不改端口/业务配置键（但见 §4 构建与部署仍需改）、不引入 cgo。

## 决策摘要

| # | 决策 | 结论 |
|---|---|---|
| D1 | 落地形态 | 服务逻辑保留 Go；自研"照 zrpc 思路"的 RPC 库替换 gRPC 库 |
| D2 | 替换范围 | chat 流式 + chat unary + keywords-filter Validate/FindAll；含停用 gRPC 依赖与生成代码（清场仅在观察期后） |
| D3 | 协议兼容 | 不与 C 版 zrpc 字节级互通；协议自定（v2 采纳评审 §4 修订） |
| D4 | 代码组织 | 共享 `rpc/` 模块（独立 go.mod）；dev 用 `replace => ../rpc`；**镜像/CI 用仓库根 build context 或发布固定版本** |
| D5 | 并发模型 | **v1：unary 走长连接池多路复用；chat 流独占连接**（消除取消/队头阻塞冲突）；全多路复用留作后续有性能证据再上 |
| D6 | 交付方式 | **双栈灰度**：transport 可配置（grpc/zrpc），先 filter unary → chat unary → chat stream 逐链路切换，观察期后删 gRPC |
| D7 | 目标副本 | `t1/ECHO-CHAT` |

## 评审修订记录（2026-09-04）

| 级别 | 评审意见 | 处置 |
|---|---|---|
| P0 | "同连接多路复用"与"context 取消即断连"冲突 | 采纳：见 D5/§2——流独占连接、unary 多路复用 + `cancel(request_id)` 只取消目标请求 |
| P0 | 无流控、队列上限、串行写模型 | 采纳：§2——每连接单 reader/单 writer、有界发送队列、写/空闲超时、连接与全局在途上限 |
| P0 | `replace ../rpc` 与 Docker 构建上下文不兼容 | 采纳：§4——镜像改仓库根 build context（复制 `rpc/`+目标服务）；`go.sum` 先行以利用缓存 |
| P0 | `start.sh 零改动`不成立（增量构建不感知 `rpc/`） | 采纳：§4——启动脚本的"共享模块更新→重编三个消费者"依赖判断必须加入 |
| P0 | 帧读取缺最大长度/超时/异常处置 | 采纳：§1——`MaxFrameSize`（默认 4 MiB，可配）、读/空闲超时、坏帧断连策略 |
| P1 | 流式终止语义不完整 | 采纳：§1——每请求恰好一个终态（`stream_end`/`error`）；应用末包仍为普通 `stream_data` |
| P1 | 截止时间未跨进程传播 | 采纳：§1——请求携带 `deadline_unix_ms`；服务端据此派生可取消 context；支持 `cancel` |
| P1 | 错误无稳定模型 | 采纳：§1——稳定错误码 + 可重试属性；不向客户端回传内部细节 |
| P1 | health 替换未覆盖部署配置 | 采纳：§4——`rpc_health_probe`（或 HTTP `/healthz`），同步 Docker/compose/运行文档 |
| P1 | gRPC 指标中间件无迁移设计 | 采纳：§3.1/§4——metrics 中间件 transport 无关化；补请求/时延/在途/错误/连接指标 |
| P1 | 改动清单不完整 | 采纳：§7——纳入 `app.go`、`metrics-app`、Docker/compose、`start.sh`、文档、测试 |
| P1 | 无灰度与快速回滚 | 采纳：D6/§5——按链路双栈配置切换；每链路通过后 flip；观察期内保留 gRPC 镜像回滚 |
| P2 | CRC 价值高估 | 折中：保留为**可选格式校验**（crc32c，配置开关，默认关），非安全机制；跨不可信网络另用 TLS/HMAC |
| P2 | JSON 手写契约缺演进规则 | 采纳：§1/§3——统一 contract 包、黄金 JSON 用例、未知字段丢弃策略、版本化信封 `v`、兼容矩阵 |
| §3.3 | 改动面 > 原清单 | 采纳：§7 补全 `app.go`/`metrics-app`/`services/grpc-client`/`services/services.go`/Docker/compose/文档 |
| §3.4 | 流式当前无鉴权，统一校验属行为变化 | 采纳：§3.1 记录"stream 补鉴权 = 安全修复"，加兼容测试，不再描述为照搬 |

## 1. 线协议（v2）

### 1.1 帧头（评审 §4 修订）

```
magic(2B "zR") | version(1B) | flags(1B) | payload_len(4B, 大端) | [crc32c(4B, 可选)]
```
- `payload_len` **分配前校验**，默认上限 4 MiB，服务级可配；超限按协议错误断连。
- crc32c 为可选格式校验（`flags` 位标识），不承担认证；是否常开由 benchmark 决定。
- 坏 CRC / 坏 magic → 断连并记指标。
- 报文为长度切帧 UTF-8 JSON；读侧 bufio，处理粘包/半包。

### 1.2 信封与消息类型

```
message-type: request | response | stream_data | stream_end | error | cancel | ping | pong
```

| 字段 | 说明 |
|---|---|
| `v` | 信封版本（当前 1） |
| `type` | 上表之一 |
| `request_id` | 连接内唯一；注册 pending 前查重，冲突按错误处理 |
| `method` | 共享常量（`chat.completion(_stream)`/`filter.validate`/`filter.find_all`/`system.ping`） |
| `auth` | `Bearer <token>`；v1 按请求携带，连接握手认证留后续 |
| `deadline_unix_ms` | 无 deadline 为 0 |
| `params` / `result` | `json.RawMessage` 延迟解码，**不使用无类型 `map[string]any`** |
| `code` / `msg` | 稳定错误码 + 非内部细节消息（仅 unary/流终态 `error`） |

**终态状态机**：每个已接受请求只能以 `stream_end`（流式）或 `response`（unary）或 `error` 收尾一次；应用层末包（`finish_reason=stop`）仍作普通 `stream_data`，其后由框架发唯一 `stream_end`；收到终态即移除 pending；连接断开一次性失败该连接全部 pending；未知 `request_id` 记日志并丢弃（或按协议断连）。

**错误模型**：稳定错误码（如 `UNAUTHENTICATED`/`DEADLINE_EXCEEDED`/`CANCELLED`/`RESOURCE_EXHAUSTED`/`INTERNAL`）+ `retryable` 属性；鉴权/超时/取消/过载/内部错误可区分。

**消息体** = 现有 proto 逐字段搬为 Go struct（`rpc/chat`、`rpc/filter`），JSON 字段名对齐 proto `json_name`，前后端/OpenAI 契约零改动；序列化 `encoding/json`，不引 jsonpb。

## 2. 连接与并发模型（v1，D5）

| 调用 | 模型 |
|---|---|
| filter `Validate/FindAll` | 少量长连接，多路复用 unary（限制 pending、断线一次性失败全部 pending、可对未收到响应的幂等请求做一次受限重试） |
| chat unary `ChatCompletion` | unary 连接池，复用同一套 pending/超时机制 |
| chat stream `ChatCompletionStream` | **每在途流独占一条连接**（独立流式池，一连接一在途流）；客户端取消 = 关连接即传播取消，不影响其他请求，无跨流队头阻塞 |

公共约束（v1 即满足，为将来全多路复用打底）：
1. 每连接一个 reader 协程 + 一个 writer 协程；任何 handler 不得直接并发写 socket。
2. 每请求有界发送队列 + 连接级有界队列；满则 `RESOURCE_EXHAUSTED`。
3. unary 多路复用连接上 `cancel(request_id)` 只取消对应服务端 context，不关共享连接。
4. 每连接在途上限 + 服务端全局并发上限；慢流/慢客户端不饿死 unary。
5. 优雅停机：停接新请求 → 等在途到上限时间 → 关连接。

## 3. 三模块改造面（v2 补全）

### 3.1 ai-chat-service（Chat 服务端）
- `chat-server/main.go`：`grpc.NewServer` → `rpc.NewServer(rpc.TokenAuth(...))`，register 两方法；metrics `:8080` 不动。
- `chat-server/server/server.go` + **`app.go`（341 行，大量直接使用 proto 类型并创建 filter client，必须迁移）**：业务逻辑不动（敏感词/关键词、语义缓存、openai HTTP 流、tokens、saveContext、busMetrics）；入参/流写出从 proto/grpc.ServerStream → `rpc/chat` 契约 + 流 writer。
- `chat-server/metrics-app/`：由"gRPC stream interceptor"抽象为 **transport 无关中间件**（指标：请求数/时延/在途/错误码/连接数）；补 unary 指标（现 unary 无指标，属改进）。
- **鉴权行为变化**：现主链路只有 unary 鉴权、流式 `ChatCompletionStream` 服务端未校验；v2 统一校验全部业务方法（安全修复），配兼容测试，不再表述为"沿用现状"。
- 其内部两处 filter client（`services/keywords-filter/keywords.go`、`sensitive.go`，配 `services/grpc-client`、`services/services.go`）→ 新 client（sensitive=50053 / keywords=50054）。
- 删（观察期后）：`proto/`、`services/keywords-filter/proto/`、`interceptor/auth.go`、grpc 依赖。

### 3.2 keywords-filter（Filter 服务端）
- `filter-server/main.go` 同构替换（rpc.Server + TokenAuth）；50053/50054 两实例不变。
- `server/server.go`：实现不变，签名换 `rpc/filter`。
- 删（观察期后）：`proto/`、`interceptor/`、grpc。

### 3.3 ai-chat-backend（纯客户端）
- `services/grpc-client/*` → 新 client 池（Get/Put 语义保留），地址取 config `dependOn.ai-chat-service.address`。
- `services/services.go` metadata 鉴权 → 请求 `auth` 字段。
- `pkg/controllers/chat.go`：`ChatCompletionStream`+`Recv()` 循环 → `client.Stream(...)` 逐块回调；循环体逻辑原样搬（含 source/15-chunk token 刷新/自动重登/HTTP 流写出）。
- 删 vendored `services/ai-chat-service/proto/`。

## 4. 配置、构建、部署与运行（v2 修订）

- **端口与业务配置键不变**：50053/50054/50055、accessToken/address 均不动；**服务启动顺序不变**。
- **`start.sh` 必须改**：当前增量构建仅 `find <模块目录> -name '*.go' -newer <bin>`。新增规则：`rpc/` 下任意 `*.go` 更新 → 触发三个消费者（backend/chat-service/keywords-filter）重编（或统一根级构建入口）。
- **Dockerfile 必须改**：现有三个 Dockerfile 以各自模块为 build context（`ADD ./`），镜像内无 `../rpc`。改为：**从仓库根构建**，分别复制 `rpc/`（先 go.mod/go.sum 以利用层缓存）与目标服务目录；或发布固定版本 `echo-rpc@vX.Y.Z` 弃用 replace。镜像内容器的 grpc_health_probe 同步替换。
- **探活**：`system.ping`（unary）+ 独立 `rpc_health_probe` 二进制或 HTTP `/healthz`；替换 `ai-chat-stack/compose.yaml` 中三处 `grpc_health_probe` healthcheck 与 Dockerfile 里的 probe 拷贝。
- 三模块 go.mod：dev 加 `require echo-rpc` + `replace => ../rpc`（本机构建/start.sh 用）；镜像构建不受此 replace 影响（见上）。

## 5. 实施阶段（v2 门控式）

- **阶段 0 基线与决策门槛**：记录 gRPC 基线（filter unary / chat stream 的吞吐、P50/P95/P99、CPU/RSS/连接数/首 token/完整响应时延）；定义目标（功能零回归、P95 劣化 ≤5%、异常下无 goroutine/pending 泄漏）；无可测收益且无自控诉求则保留 gRPC。
- **阶段 1 RPC 库 + 故障测试**：frame、unary、错误码、deadline、cancel、探活、优雅停机；`net.Pipe`+真实 TCP 覆盖半包/粘包/短写/超长帧/坏 CRC/未知方法/重复 ID/断线/超时/并发写；`go test -race`、fuzz、泄漏测试。
- **阶段 2 filter unary 双栈试点**：keywords-filter 同开 grpc+zrpc（或可切换版本），chat-service 按配置选 transport；shadow 对比后 flip。
- **阶段 3 chat unary**：JSON 契约黄金测试（逐字段对比 proto 与新 contract 输出）；验鉴权/deadline/错误码/边界。
- **阶段 4 chat stream**：初版独占连接；逐块对比 chunk 顺序、`source`、`finish_reason`、终态、计费与缓存；验浏览器断连→取消传播到上游 LLM HTTP。
- **阶段 5 观测与清理**：transport 配置开关逐链路 flip；保留可即时切回 gRPC 的配置/镜像；观察期（本仓 local 单实例部署 → 以"配置开关切换 + 保留 gRPC 镜像"替代多实例 10→50→100% 灰度）后删 pb 与 gRPC 依赖，更新 Docker/compose/探活/文档。

## 6. 验收标准（v2）

**功能一致性**：Validate/FindAll/chat unary/chat stream 全有契约测试；流式 chunk 内容与顺序一致，stop 末包后仅一个终态；`source=cache/llm`、token 使用/节省、扣费、上下文保存、语义缓存写入不回归；未认证 unary 与 stream 均被拒；`ping` 是否免鉴权明确。

**稳定性与资源**：1B 拆包/随机粘包/短写正确收发；超长帧分配前拒、坏帧不 panic；客户端取消后服务端 handler 与上游 LLM 请求限时退出；断连后 pending 全释放；连续压测后 goroutine/FD/RSS 不增长；慢客户端不无界缓存、不饿死 unary。

**性能**：1/10/50/100 并发 ×（unary/短流/长流/缓存命中流）；报 P50/P95/P99、首包时延、完整时延、QPS、CPU、RSS、分配数、网络字节；与同机及跨机 gRPC 基线比；无证据不得宣称自研更高。

**工程化**：`go test ./...`、`-race`、`go vet ./...` 通过；三本机构建与三镜像构建通过；compose healthcheck/优雅停机/回滚演练通过。

## 7. 交付物清单（v2）

新增：`rpc/`（go.mod、`protocol.go`[版本/类型/错误码/限制]、`frame.go`/`msg.go`、`server.go`、`client.go`、`cancel.go`、`shutdown.go`、`metrics.go`、`health.go`、`chat/`、`filter/`、`*_test.go`/fuzz/bench）+ `rpc_health_probe` 或 `/healthz`。

改动：`start.sh`（共享模块依赖重建）；三个 Dockerfile（根 build context）+ `ai-chat-stack/compose.yaml`（healthcheck）；`ai-chat-service/chat-server/{main,server/app}.go`、`chat-server/metrics-app/`、`services/grpc-client/`、`services/services.go`、`go.mod`；`keywords-filter/filter-server/{main,server/server}.go`、`go.mod`；`ai-chat-backend/pkg/controllers/chat.go`、`services/services.go`、`go.mod`。

删除（观察期后）：两处 chat proto 生成、两处 filter proto 生成、`interceptor/*`、`services/grpc-client/*`、容器内 `grpc_health_probe`。

文档：运行手册、协议说明、契约兼容策略、回滚手册、架构文档中 gRPC 描述更新。

## 8. 风险与开放项

- 评审环境未运行 Go 工具链：三模块 `go test`/race/benchmark/Docker 构建须在阶段 0/1 补齐作为准入证据。
- unary 与 stream 连接模型不同 → client 库须清晰区分两套池；避免误用。
- `encoding/json` 字段对齐、未知字段丢弃策略、信封 `v` 升级矩阵需在阶段 3 黄金用例固化。
- 优雅停机与在途流断连时间窗需在阶段 1 用真实负载校准。
