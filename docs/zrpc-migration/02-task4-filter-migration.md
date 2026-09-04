# zrpc 迁移 · Task 4 记录：keywords-filter 双栈 + chat-service 可切换下游

日期：2026-09-04 ｜ 分支：zrpc-migration

## 目标与做法

把 filter 两条 unary 链路（chat-service → sensitive:50053 / keywords:50054）迁移到 zrpc，
采用双栈 + 配置切换，黄金一致后再切流。

- **keywords-filter**：新增 zrpc v2 listener（`server.zrpcPort`，50063/50064），与原 gRPC
  （50053/50054）并存。注册 `filter.validate` / `filter.find_all`（共享 contract），复用同一业务
  `IFilter`，Bearer token 鉴权由 zrpc server envelope 统一处理。
- **共享契约**：`echo-zrpc-go/contract`（FilterRequest / ValidateResponse / FindAllResponse +
  方法常量），JSON 字段与 proto json_name 对齐（golden 测试锁定）。
- **chat-service**：`dependOn.sensitive.Transport` / `dependOn.keywords.Transport` = `grpc`(默认)
  | `zrpc`。zrpc 时走新 client（单连接串行，连接池为后续阶段），超时 800ms。
  错误策略不变：sensitive 失败→Chat 报错（fail-closed）；keywords 失败→空列表（fail-open）。
- **healthz/readyz**：keywords-filter 加 HTTP `/healthz` `/readyz`（`server.healthPort`，18081/18082），
  替代 grpc_health_probe（compose 侧 Task 8 统一改）。

## 验证证据

| 项 | 结果 |
|---|---|
| contract golden（`zrpc-go/contract`） | JSON 形状与 proto json_name 一致，含空 repeated=`[]` |
| transport parity（keywords-filter `filter-server/server/parity_test.go`） | 真实 filter 上 **gRPC == zrpc == direct**（8 条黄金输入：命中/未命中/空格/CJK/空串） |
| chat-service zrpc 管道（`services/keywords-filter/zrpc_test.go`） | stub zrpc server 上 ZRPCValidate/FindAll 结果正确；错 token 报错 |
| 构建 | keywords-filter、ai-chat-service、zrpc-go 三模块 `go build` 通过 |

## 现状与切换

- 两端 `dev.config.yaml` 已给 zrpc 端口与 health 端口；chat-service 的 `transport` 未显式配置
  （默认 `grpc`），保持观察期基线行为不变。
- 切流动作：把 `ai-chat-service/dev.config.yaml` 的
  `dependOn.sensitive.transport: zrpc`、`dependOn.keywords.transport: zrpc` 打开，并把下游地址
  指向 50063/50064。灰度顺序按方案 §14：先 shadow 对比 → 10% → 50% → 100%。
- 待办：chat-service zrpc unary 连接池（当前单连接串行）；compose/镜像（Task 8）。
