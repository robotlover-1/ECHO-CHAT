# ECHO-CHAT gRPC 换 zrpc-Go：自研 RPC 替换 gRPC 设计文档

- 日期：2026-09-04
- 状态：已确认（brainstorming 分节过审）
- 位置：ECHO-CHAT monorepo（`t1/ECHO-CHAT`）
- 参考：`proj/tmp/zrpc-main.zip`（C 版 zrpc：TCP + 6B 头 CRC32/len + cJSON，教学级骨架，**非**互操作目标）
- 关联：`docs/superpowers/specs/2026-08-31-ai-chat-kvstore-split-design.md` 等历史 spec

## 背景与目标

ECHO-CHAT 三个独立 Go 模块通过 gRPC 互连：ai-chat-backend（web 后端）→ ai-chat-service（Chat 服务）、ai-chat-service → keywords-filter（敏感词/关键词过滤）。本任务把这三条 gRPC 链路整体替换为**自研 Go RPC**——借 zrpc 的骨架思路（注册式方法表 + CRC/长度帧 + JSON 报文），按需扩展，**不与 C 版 zrpc 字节级互通**（已确认）。

非目标 / 明确排除：
- 不迁移业务逻辑；只在 transport 层替换。
- 不与 C zrpc 互操作；不引入 cgo。
- 不改前端契约、不改 web 流式输出格式、不改 OpenAI 侧契约。
- 不改端口/配置键（见 §5），start.sh 零改动。

## 决策摘要（brainstorming 已确认）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 落地形态 | 服务逻辑保留 Go，自研"照 zrpc 思路"的 RPC 库替换 gRPC 库 |
| D2 | 替换范围 | chat 流式 ChatCompletionStream + chat 非流式 ChatCompletion + keywords-filter Validate/FindAll |
| D3 | 协议兼容 | 不需要与 C 版 zrpc 字节级互通；协议自定可扩展 |
| D4 | 代码组织 | 新建共享 `rpc/` 模块（独立 go.mod），三模块 `replace => ../rpc`；不做拷贝 |
| D5 | 目标副本 | `t1/ECHO-CHAT`（在跑、已推送 github 的副本） |

## 1. 线协议（TCP + JSON，沿用 zrpc 骨架）

帧格式（zrpc 的 CRC+长度思路，长度升 32 位，去掉 2B 上限；纯 Go 无每请求建连）：
```
[4B CRC32-LE] [4B payload-len LE] [payload UTF-8 JSON]
```

报文信封（zrpc 的 namespace/config/method/callerid 拍平成单层，补流式与鉴权）：
```json
{
  "v": 1,
  "type": "req | resp | err | end",
  "id": "<uuid>",
  "method": "chat.completion | chat.completion_stream | filter.validate | filter.find_all | system.ping",
  "auth": "Bearer <token>",
  "params": {},
  "result": {},
  "code": 0,
  "msg": ""
}
```

消息语义：
- unary：`req` → `resp` / `err`。
- 服务端流式：`req` → `resp`×N（每帧一个增量 chunk）→ `end`。
- 客户端按 `id` 收齐；连接断开即视为请求取消（客户端 abort → 关连接）。

消息体 = 现有 proto 结构**逐字段照搬**为 Go struct；JSON 字段名与原 proto `json_name` 一致，前后端/OpenAI 契约零改动。序列化 `encoding/json`；不引 jsonpb。

鉴权：沿用现状 `interceptor/auth.go` 的 Bearer token 比对逻辑，token 放 `auth` 字段，服务端统一 TokenAuth 中间件处理。

## 2. rpc 库结构与 API（新模块 `rpc/`）

```
rpc/
  go.mod        // module echo-rpc
  frame.go      // §1 帧编解码（bufio）
  msg.go        // 信封类型
  server.go     // 注册式方法表、listen/accept、连接分发、TokenAuth 中间件
  client.go     // 长连接池 + 按 id 多路复用路由；Unary()/Stream()
  health.go     // system.ping（替代 grpc health）
  chat/         // contract：chat.completion(_stream) 方法常量 + 请求/流式 chunk structs
  filter/       // contract：filter.validate/find_all 方法常量 + structs
```

要点：
- 库保持通用，不感知 chat/filter 业务；业务消息放子包（契约）。
- 方法表 = zrpc `zrpc_register.json` 的 Go 版：`srv.Register(chat.MethodCompletionStream, handler, Stream)`；client 端同常量防字符串漂移。
- Client 连接模型：默认 N 条长连接复用（对标 `grpc_client_pool.go` 的 Get/Put），同连接多路复用（读协程按 `id` 分发到各 pending 调用）；`ChatCompletionStream` 走 `Stream()`，每 `resp` 帧回调一次 → 直接映射 backend 现有 `stream.Recv()` 循环。
- 错误：统一 envelope `err`（code+msg），库层透出 Go error。
- 超时/取消：支持 context 取消 → 断连终止流。

## 3. 三模块改造面

### 3.1 ai-chat-service（Chat 服务端）
- `chat-server/main.go`：`grpc.NewServer + RegisterChatServer + health` → `rpc.NewServer(rpc.TokenAuth(cnf.Server.AccessToken))` + register 两方法 + `Serve(lis)`；metrics `:8080` 不动。
- `chat-server/server/server.go`：`ChatService` 不再实现 `proto.ChatServer`，改方法 handler；**业务逻辑不动**（敏感词/关键词、语义缓存、openai HTTP 流、tokens 统计、saveContext、busMetrics）；入参与流写出从 proto/grpc.ServerStream → `rpc/chat` 契约 + 流式 writer（`Resp`/`End`/`Err`）。
- 其内部两处 grpc client（`services/keywords-filter/keywords.go`、`sensitive.go`）→ 新 client（sensitive=50053 / keywords=50054）。
- 删：`proto/`、`services/keywords-filter/proto/`、`interceptor/auth.go`、grpc 依赖。

### 3.2 keywords-filter（Filter 服务端）
- `filter-server/main.go` 同构替换（rpc.Server + TokenAuth）；同一二进制跑 50053/50054 两实例配置天然不变。
- `filter-server/server/server.go`：实现不变，签名换 `rpc/filter` 类型。
- 删：`proto/`、`interceptor/`、grpc 依赖。

### 3.3 ai-chat-backend（纯客户端）
- 删 `services/grpc-client/*` → 新 client 长连接池（Get/Put 语义保留），地址取 config `dependOn.ai-chat-service.address`。
- `services/services.go` metadata 鉴权 → 请求 `auth` 字段。
- `pkg/controllers/chat.go`：`ChatCompletionStream` + `stream.Recv()` 循环 → `client.Stream(ctx, method, req, chunkHandler)`；循环体逻辑原样搬。
- 删 vendored `services/ai-chat-service/proto/`、grpc 依赖。

## 4. 配置 / 启动 / 构建

- 端口与配置键不变：50053（sensitive）、50054（keywords）、50055（chat）；accessToken/address 键不变 → **start.sh 零改动**。
- 三模块 go.mod 增加 `require echo-rpc` + `replace echo-rpc => ../rpc`（不引入 go.work 维护面）；根 Makefile/docker 若有按模块 build 处补 rpc 模块。
- 探活：`system.ping`（目前 start.sh 无 health 探活依赖；`grpc_health_probe` 为遗留）。

## 5. 实施阶段与验证

1. 搭 `rpc/` 库：frame/msg + server(unary) + client 池 + TokenAuth + system.ping。
2. 样板打通最小无状态 unary 链：keywords-filter `Validate`/`FindAll` + ai-chat-service 端 client（验帧/鉴权/池）。
3. 替换 chat unary `ChatCompletion`。
4. 替换核心流式 `ChatCompletionStream` + backend 逐块回调。
5. 清场与全量验证：删 grpc 依赖与 pb 生成文件；`go vet`、各模块既有 test、端到端（mock-openai-api 流式回放：逐字一致、source 缓存命中/公有大模型、tokens 计费不回归）。

## 6. 风险与开放项

- 三 go.mod 独立构建链路：replace 路径在 start.sh/CI 各构建方式下均须解析（实施前先各试 `go build` 一次）。
- 流式背压/半包：帧按长度切，读协程需处理粘包（bufio 已有）；长流慢客户端的内存占用按现状评估。
- `ChatCompletion` unary 当前无人调用（主链路只用流式），仍按要求替换以对齐 proto 契约；后续可删（见 D2 保留接口面）。
- jsonpb→encoding/json 字段映射需逐字段核对（proto 有 `json_name` 别名）。

## 7. 交付物清单（改动文件，实施时锁定）

- 新增：`rpc/`（go.mod + 库 + chat/filter 契约）
- 改：`ai-chat-service/chat-server/main.go`、`chat-server/server/server.go`、`ai-chat-service/go.mod`
- 改：`keywords-filter/filter-server/main.go`、`filter-server/server/server.go`、`keywords-filter/go.mod`
- 改：`ai-chat-backend/pkg/controllers/chat.go`、`services/services.go`、`ai-chat-backend/go.mod`
- 删：两处 chat proto（backend 与 service）、两处 filter proto、`interceptor/*`、`services/grpc-client/*`
