# zrpc 迁移 · Task 7b 记录：ai-chat-backend 流式调用 transport 切换

日期：2026-09-04 ｜ 分支：zrpc-migration

## 目标与做法

backend → chat-service 的 ChatCompletionStream 调用支持 `grpc|zrpc` 配置切换，并把父 ctx 从
`context.Background()` 改为 Gin 请求 ctx（浏览器断开 → ctx 取消 → zrpc 断流 → chat-service 取消上游 LLM）。

- 后端新抽象 `ChatStream{Recv(); Close()}`（services/ai-chat-service/chat_stream.go）：
  - gRPC 路径：原共享连接池 + NewChatClient + Bearer metadata，行为不变；
  - zrpc 路径：`zrpc.Client.Stream(contract.MethodChatCompletionStream)`，chunk 用共享 contract，
    经映射转回后端 proto —— **controller 的循环体/计费/source/计分逻辑一行未改**。
- controller `ChatProcess`：按 `dependOn.ai-chat-service.transport` 取地址（gRPC 50055 / zrpc 50065），
  用 `ctx.Request.Context()` 作父 ctx。
- config/dev.config.yaml：补 `zrpcAddress` 与 `transport`（默认 grpc，观察期不变）。

## 验证

| 项 | 结果 |
|---|---|
| backend zrpc 管道测试（fake zrpc chat stream server） | `TestOpenChatStreamZRPC`：3 块顺序拼接、source/created 透传、EOF 收尾、错 token 报 Unauthenticated ✅ |
| 构建 | ai-chat-backend `go build ./...` 通过（controller/chat-service 联动编译） |

## 边界（如实）

- **真实业务端到端**（SSE 顺序/source=cache|llm/重试/扣费/断连取消）仍需把整栈跑起来：mock-openai-api +
  mysql + redis + keywords-filter(双栈) + ai-chat-service(双栈) + ai-chat-backend，用同一请求分别打
  gRPC/zrpc 对比 —— 归入 Task 8 的运维闭环跑真栈验证（含灰度开关 transport: zrpc）。
- backend 仅消费流式；ChatCompletion unary 未被 backend 使用（契约已备，服务端已暴露）。
