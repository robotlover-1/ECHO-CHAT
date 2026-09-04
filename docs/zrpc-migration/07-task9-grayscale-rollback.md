# zrpc 迁移 · Task 9 记录：双栈灰度、回滚与对比方法

日期：2026-09-04 ｜ 分支：zrpc-migration ｜ 相关：Task 4(04)、Task 7b(04)、本地真栈(06)

## 1. 灰度顺序（按链路，逐段翻）

```text
filter Validate → filter FindAll → chat unary → chat stream（浏览器断开取消链）
```

代码迁移顺序与本库一致：keywords-filter 双栈(50053/54 gRPC + 50063/64 zrpc)先落，
chat-service 双栈(50055 gRPC + 50065 zrpc)后落；backend 只消费 stream。

**每段切换动作**（本机测试配置，勿改默认 dev 配置）：
1. `ai-chat-service/dev.ztest.yaml` → `dependOn.sensitive.transport: "zrpc"` + address 50063；
   `dependOn.keywords.transport: "zrpc"` + address 50064（翻 chat→filter 段）。
2. `ai-chat-backend/dev.ztest.yaml` → `dependOn.ai-chat-service.transport: "zrpc"`（address 50065，翻 backend→chat 段）。
3. 每改一处重启对应服务：`kill $(pgrep -x <名>)` 后按 docs/zrpc-migration/06 的 nohup 命令重起。
4. 验证：同一批请求（见 §4）两 transport 结果一致后再放量。

放量节奏：shadow 对比 → 10% → 50% → 100% → 观察期（如 1 周）→ 删 gRPC。
观察期**保留**：gRPC 代码路径、`proto` 契约与生成代码、上一版镜像、`transport: grpc|zrpc` 开关。

## 2. 回滚触发条件（满足任一立即回 gRPC）

- zrpc 错误率连续 5 分钟高于 gRPC 基线 1 个百分点；
- P95 首 chunk 时延劣化 >20%（首 chunk 尤其受 NtyCo/cgo 分发影响，优先盯）；
- 进程崩溃 / C 内存持续增长 / fd 泄漏（可复用 `test_unary load` 的 fd/RSS canary 思路）;
- 重复扣费、漏扣费、内容缺失或乱序；
- 健康检查连续失败，滚动更新无法完成。

## 3. 回滚操作

1. 配置切回 `transport: grpc`（地址改回 50055/50053/50054）或直接回滚到保留镜像；
2. 不删 zrpc 日志与指标，保留故障现场；
3. 用同一批 mock 请求回放，按层定位：C frame → zrpc-go bridge → handler/业务。
   - C 层自检：`make zrpc-test`、`make zrpc-sanitize`、`./tests/bin/test_unary load 100000 1`；
   - Go 层自检：`make test-go`（各模块关键用例）。

## 4. 对比 / benchmark 方法（无 gRPC 客户端工具的快速替代）

**确定性对比**（mock 回复随机时不可比文本）：用"敏感词命中"分支（固定拦截文案）或固定长流 SSE
（docs/zrpc-migration/06 的方法：临时把 chat-service base_url 指向可控假 LLM），两 transport 各跑一次，
归一化（去 id/created）后逐字节 diff。真栈已验：gRPC == zrpc 命中路径一致、断连取消链生效。

**性能对比**（§12.4 口径）：
- 打 gRPC：`grpcurl`/自写并发 client；打 zrpc：用 C `tests/bin/ccli`（unary，`make -C third_party/zrpc ccli`）
  或多并发进程 + 同款请求。
- 记录：QPS、首 chunk P50/P95/P99、完整响应 P50/P95/P99、CPU/RSS/FD、cgo 次数均耗、每请求网络字节、错误/超时/取消率。
- 场景：filter unary、chat 缓存命中短流、LLM 长流；并发 1/10/50/100；payload 1KiB/16KiB/64KiB/1MiB。
- 结论须由数据给出，不以"必然比 gRPC 快"为预设。

## 5. 收尾清单（删 gRPC 前）

- [ ] 观察期无触发 §2 回滚条件
- [ ] 三条链路 transport=zrpc 全量 + 浏览器断连取消复验（见 §4）
- [ ] 删除/归档：`proto` 生成代码、gRPC server/client 注册、interceptor 迁移为通用 wrapper（metrics 保留指标名）
- [ ] compose healthcheck 换 HTTP /readyz；删除 grpc_health_probe 依赖
- [ ] Dockerfile 仓库根构建 + `ldd` 验证；NtyCo 优雅停机 SIGTERM 演练通过
- [ ] 输出 gRPC/zrpc benchmark 报告与本档案一并归档
