# zrpc v2：实现与在项目中的使用

状态：gRPC 已删，全链路仅自研 zrpc v2（单传输）。目录：`third_party/zrpc/`（C 库）、
`zrpc-go/`（cgo bridge + contract）、三个服务内使用点。

## 1. 分层与一次调用的旅程

```text
Go 业务(backend/chat-service)
   │  contract(chunk 结构体)   zrpc-go: Client.Unary / Client.Stream
   ▼
zrpc-go (cgo bridge)  ── import "C" → libzrpc.a
   │  C ABI: zrpc_client_new/call_unary/call_stream / zrpc_server_*send_*
   ▼
C libzrpc.a (third_party/zrpc)
   ├─ 帧层: 20B header(magic/ver/type/req_id/len/crc32) + JSON payload
   ├─ IO: read/write_full（poll + 超时 / NtyCo 协程内 yield）
   ├─ client: 普通线程阻塞 IO（NtyCo 链接期接管 recv/send，非协程线程自动回退 libc）
   └─ server: NtyCo 协程 accept/读，每连接读协程；Bearer 校验；方法表分发
   ▼
TCP（流用独占连接；unary 可复用）
```

## 2. C 库实现要点

- **协议**：v2 帧 `magic 'ZR' | ver=2 | type | req_id(8B BE) | len(4B BE) | crc32(payload)`，
  分配前校验 `MAX_FRAME_SIZE(4MiB)`；错误码 `zrpc_status_t` 稳定。
- **安全 IO**：`EINTR/EAGAIN`、poll 超时→`DEADLINE`、对端关闭→`UNAVAILABLE`、写 0 字节视为异常、
  `MSG_NOSIGNAL`；无任何网络输入可触发的 assert。
- **JSON 信封**：`{"method","auth":"Bearer ..","deadline_unix_ms","payload":<业务原始JSON>}`——
  业务 JSON 用 cJSON raw 逐字嵌入，不做重序列化；响应/流块包 `{"payload": …}`，客户端先 unwrap。
- **NtyCo server**：accept 与每连接读协程跑在调度线程；跨线程回写走"每连接写锁 + 非协程路径"；
  客户端断连 → conn-close 回调 → 取消在途流 handler 的 ctx（上游 LLM HTTP 随之释放）。
- **优雅停机**：`zrpc_server_shutdown`(假连接唤醒 accept + shutdown 各连接) → 协程退尽 →
  `nty_schedule_run` 返回 → `zrpc_server_join`（Go `Server.Close` 不泄漏线程）。
- 已知边界：NtyCo 用 ucontext，与 ASan 不兼容（sanitizer 只覆盖纯 C）；单 Client 串行一条连接
  （流每条独占连接）；流事件有界 channel 满时丢事件（背压细化是后续项）。

## 3. zrpc-go：项目里怎么用

模块 `echo-zrpc-go`；三个 Go 服务 `go.mod` 里 `replace echo-zrpc-go => ../zrpc-go`。

### 服务端（chat-service / keywords-filter）
```go
srv, _ := zrpc.NewServer(zrpc.ServerOptions{Address: "0.0.0.0:50055", AccessToken: cfg.Server.AccessToken})
srv.RegisterUnary(contract.MethodFilterValidate, func(ctx context.Context, raw json.RawMessage) (any, error) {
    var req contract.FilterRequest
    _ = json.Unmarshal(raw, &req)
    return contract.ValidateResponse{OK: ok, Keyword: w}, nil
})
srv.RegisterStream(contract.MethodChatCompletionStream, chat.ServeChatStreamZRPC) // handler 里用 *StreamWriter
_ = srv.Serve()
// 退出: srv.Close()  // 优雅停(join 调度线程) + 清 handle/worker
```
- UnaryHandler / StreamHandler 签名见 `zrpc-go`；stream 用 `w.Send(v)`/`w.End()`/`w.Error(err)`，
  断连时 handler ctx 自动取消。
- Chat 业务不依赖 cgo：统一走 `ChatStream` 接口，zrpc 侧由 `zrpcChatStream` 适配（gRPC 适配器已随 gRPC 删除）。

### 客户端（backend / chat-service 下游）
```go
cli, _ := zrpc.NewClient(zrpc.ClientOptions{Host:"127.0.0.1", Port:50055, Token:tok})
var resp contract.ChatCompletionResponse
err := cli.Unary(ctx, contract.MethodChatCompletion, &req, &resp)     // 错误为 *StatusError{Code}
st, _ := cli.Stream(ctx, contract.MethodChatCompletionStream, &req)    // ctx 取消即断流
for { var c contract.ChatCompletionStreamResponse; err := st.Recv(&c); if err==io.EOF {break} }
```
- 超时/取消：ctx 带 deadline 会转成信封 `deadline_unix_ms`；浏览器断开用 `ctx.Request.Context()` 作父 ctx。
- backend 用 `ai_chat_service.OpenChatStream(ctx, addr, token, protoReq)`（zrpc only），返回的 `Recv()`
  供 controller 计费/流逻辑零改动使用。
- chat-service 下游敏感词/关键词直接 `keywords_filter.ZRPCValidate / ZRPCFindAll(ctx, addr, token, text)`。

### 共享契约 `zrpc-go/contract/`
`methods.go` 方法常量、`chat.go`（unary+stream，含 source）、`filter.go`。
新增一个 RPC：contract 加字段（json 名与 proto json_name 一致）→ server `RegisterUnary/Stream`
→ 需要 gRPC 侧做 contract↔proto 显式映射（勿用 protojson，int64 会变字符串）→ client 调用。

## 4. 构建 / 测试 / 压测

```bash
make -C third_party/zrpc            # libzrpc.a
make test-go                         # 四模块关键用例(race)
make zrpc-test / make zrpc-sanitize # C 层普通 / sanitizer(纯C)
make build                           # 三个服务二进制
# 压测 filter Validate（zrpc，gRPC 已删）：
cd ai-chat-service && GOFLAGS=-mod=mod CGO_ENABLED=1 go run ./cmd/benchf zrpc 8 5000
# C unary 客户端(打任意 zrpc): make -C third_party/zrpc ccli → tests/bin/ccli <host> <port> <token> <method> <json>
```

## 5. 端口速查（单 zrpc；gRPC 已删）

| 服务 | zrpc 端口 | HTTP 健康 |
|---|---|---|
| keywords-filter sensitive / keywords | 50053 / 50054 | 18081 / 18082（`/healthz` `/readyz`） |
| ai-chat-service | 50055 | 8080（`/healthz` `/readyz`，与 metrics 共用） |
