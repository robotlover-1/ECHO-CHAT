# gRPC → 自研 C zrpc v2：实现步骤与过程

分支 `zrpc-migration`（已合入 main，commit `ddc9447..58d69c0`）｜ 更新 2026-09-04
本文是迁移**过程**的纵向总览；细节分档见 `docs/zrpc-migration/01..12`。

## 1. 目标与前置决策

- 目标：用自研 **C zrpc v2（协议加固）+ NtyCo（server 协程）+ cgo bridge** 替换 ECHO-CHAT 4 条 gRPC 调用，
  业务逻辑零改动，展示完整跨语言改造链路。
- 前置授权（2026-09-04 用户确认）：zrpc 教学源码与 NtyCo 均可使用（后者已在 kvstore 使用）；
  但因原 zrpc 无 LICENSE，**只借鉴思想、代码从零编写**（`third_party/zrpc/`，含 LICENSE-NOTICE）。
- 选型：server 用 **NtyCo 协程**（方案 B）而非 pthread——经用户确认；帧/IO 仍按方案 §5 加固。
- 原则：双栈灰度，先 C 帧→cgo→filter→chat，观察期保留 gRPC，`transport: grpc|zrpc` 按链路切换。

## 2. 分阶段实现步骤（含证据）

| 阶段 | 做了什么 | 关键证据 |
|---|---|---|
| Task0 基线 | 方案入档、原 zrpc 参考快照、LICENSE-NOTICE、gRPC 功能基线 | docs/01 |
| Task1 C 帧/IO | `read/write_full`(EINTR/EAGAIN/poll/超时/EOF)、20B v2 帧(大端/CRC/max-frame/分配前校验)、去 assert | ASan/UBSan + C 单测全过 |
| Task2 C unary | C client/server ABI + 方法表 + JSON 信封 + Bearer 鉴权 + ping/pong；server 用 NtyCo 协程 | 纯 C 10 万次无泄漏(~25k/s) |
| Task3 cgo 桥 | `zrpc-go`：`Client.Unary`/`Server.RegisterUnary` + uint64 handle 注册表 + worker 分发 | 双向证据 + `go test -race` |
| Task4 filter 迁移 | keywords-filter 双栈(50053/54 gRPC + 50063/64 zrpc)；`zrpc-go/contract`；chat-service 可切换下游 | gRPC==zrpc==direct 8 黄金输入 |
| Task5 stream+取消 | C `send_stream_data/end` + `call_stream/cancel`(独占连接)；Go `Client.Stream/Recv`(有界channel)、`RegisterStream/StreamWriter`；断连→conn-close→cancelFD→handler ctx | C/Go 用例；取消→上游 3s 释放 |
| Task6 chat unary | `contract/chat.go`；chat-service zrpc listener(50065) 注册 `chat.completion`；contract↔proto 显式映射复用业务 | golden + round-trip + fake e2e |
| Task7 chat stream | 业务 `ChatStream` 抽象(grpcChatStream/zrpcChatStream)复用 SSE/重试/缓存逻辑零改动；backend `ChatStream` transport 切换 + `ctx.Request.Context()` | 管道测试；真栈 zrpc LLM 流 |
| Task8 工程 | 根 Makefile；start.sh libzrpc 增量感知；NtyCo 优雅停机(shutdown 唤醒 + join 无线程泄漏)；filter benchmark；Dockerfile 模板 | `make test-go/build` 绿；graceful_test；bench 报告 |
| Task9 文档 | 灰度/回滚/对比方法、交付 README、运行 runbook | docs/07、README、docs/06 |

## 3. 关键实现要点（取舍记录）

1. **协议保真**：业务 JSON 以 cJSON raw 逐字嵌入/取出，不做重序列化（保字段序/数字）；但最终响应仍会
   经 Go `json.Marshal` 重组（顺序归一），消费者按字段名取值，语义等价。
2. **int64 走数字**：显式 contract↔proto 映射，避免 protojson 把 int64 编码成字符串（与 OpenAI 风格 JSON 不一致）。
3. **NtyCo/ASan**：ucontext 切栈与 ASan 冲突，sanitizer 只跑纯 C（frame/io/json）；NtyCo 路径用
   10 万压测 fd/RSS canary + PROT_NONE 守卫页实验证实非真实越界。
4. **鉴权**：zrpc 信封统一 Bearer（filter/chat service 均校验，弥补原 gRPC stream 未鉴权的缺陷）。
5. **断连取消链**：backend `ctx.Request.Context()` → zrpc cancel → chat writer ctx → 上游 LLM HTTP 释放
   （真栈以长流假 LLM 掐断验证）。
6. **优雅停机**：`zrpc_server_shutdown` 用假连接唤醒 accept + `shutdown(fd)` 唤醒读协程 → 协程退尽 →
   `nty_schedule_run` 返回 → `zrpc_server_join`；Go `Server.Close` join（无线程泄漏）。

## 4. 验证矩阵（真栈 2026-09-04）

- filter parity（8 黄金输入）三路一致；backend 敏感命中两 transport 归一逐字节一致。
- `source` 分支：llm（真 DeepSeek + mock）/ cache（语义命中 `subject=avl_tree`）/ 敏感拦截。
- 断连取消全链路；NtyCo 优雅停机（task 数回基线）。
- `make test-go` / `make build` 全绿；filter unary 微基准 zrpc 不劣于 gRPC（docs/08）。

## 5. 尚未完成（需 docker/部署环境）

镜像构建与 compose healthz、容器 SIGTERM 演练、灰度放量与观察期、删 gRPC（清单 docs/07；模板 docs/09）。
