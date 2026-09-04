# zrpc 迁移 · Task 6 记录：ChatCompletion（unary）服务端接入

日期：2026-09-04 ｜ 分支：zrpc-migration

## 范围与做法

- 新增共享契约 `echo-zrpc-go/contract/chat.go`：ChatCompletionRequest/Response 等，字段与
  proto json_name 对齐；JSON golden 测试锁定（`created` 等保持数字，不用 protojson 的 int64 字符串）。
- ai-chat-service 新增 zrpc listener（`server.zrpcPort` 50065），注册 `chat.completion`。
  **业务零改动**：ChatCompletion 方法本就 transport 无关（ctx+proto 出入参），zrpc handler 只做
  contract↔proto 的显式字段适配（避免 protojson int64→string 分歧），再复用既有方法；鉴权走 zrpc envelope。
- 双栈：gRPC(50055) 保留，观察期两套并存。

## 验证

| 项 | 结果 |
|---|---|
| contract golden | 请求/响应 JSON 形状与字段名锁定（zrpc-go/contract） |
| contract↔proto 类型等价（typed round-trip） | 字段值、created/tokens 数字无损 |
| zrpc unary 端到端（fake chat，无 infra） | `chat.completion` 经 zrpc 返回合同一致响应；错误→StatusError |
| 构建 | zrpc-go / ai-chat-service `go build` 通过 |

## 边界与下一步

- 本 Task 只做**服务端 unary 暴露**。backend 侧切换与 `chat.completion_stream` 合并到 Task 7 一并做
  （backend 反正要同时切 unary+stream，客户端接线一次完成）；届时补 mock-LLM 端到端 + 缓存命中/未命中回归。
- `RegisterChatZRPC(..., streamOK=false)` 预留；Task 7 填 stream adapter 并置 true。
