# zrpc 迁移 · Benchmark：filter Validate gRPC vs zrpc（首轮快照）

日期：2026-09-04 ｜ 分支：zrpc-migration

## 场景与口径

- 被测：keywords-filter 实例 A（sensitive）—— 同一业务 `IFilter.Validate`，文本"完全正常的句子，讨论天气"（未命中）。
- 端口：gRPC `50053` / zrpc `50063`（同进程双栈，同一服务二进制）。
- 客户端：临时工具 `ai-chat-service/cmd/benchf`（gRPC 走共享连接 + Bearer metadata；zrpc 每 worker 一条专用连接）。
- 本机：单盒开发机，dev 构建（无 `-ldflags` 优化裁剪），NtyCo 协程 server。
- 做法：预热 4×1000 后取 8×5000；`p50/p95/p99` 单位 µs。

## 结果

| 项 | gRPC(50053) | zrpc(50063) |
|---|---|---|
| QPS（并发8，5000次） | 26 605 | **33 631** |
| avg | 297 µs | **230 µs** |
| p50 | 254 µs | 202 µs |
| p95 | 459 µs | 437 µs |
| p99 | 1542 µs | **689 µs** |

预热（4×1000）另见：gRPC qps 17 877 / p50 191 / p99 1149；zrpc qps 17 303 / p50 160 / p99 667。

## 结论与边界（如实）

- 首轮快照里 zrpc **不劣于 gRPC**（QPS 高约 26%，p99 明显更低），但**这是单盒、单一 unary、dev 构建**的微基准，
  不作为"必然更快"的定论（方案 §1.2 也明确不以性能为预设结论）。
- 口径差异：zrpc 每 worker 专用连接（更接近生产连接池），gRPC 单连接复用；均未引入外部依赖（不经过 LLM/mock）。
- 需要的更全画像（§12.4 / Task9）：同机 vs 跨机、并发 1/10/50/100、chat 缓存命中短流/LLM 长流、
  1KiB-1MiB payload、CPU/RSS/FD/goroutine、cgo 调用次数与均耗、每请求网络字节、错误/超时/取消率。
- 回滚/灰度门槛按 §14.2 判据（错误率、P95 首 chunk）在观察期用线上流量或更大样本复核。

复现：`cd ai-chat-service && GOFLAGS=-mod=mod CGO_ENABLED=1 go run ./cmd/benchf grpc 8 5000`（zrpc 同理）。
