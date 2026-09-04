# zrpc 迁移 · Task 8 记录：根构建缝合（libzrpc 增量感知）

日期：2026-09-04 ｜ 分支：zrpc-migration

## 已完成（本地可验证）

- **根 Makefile**（§9.1）：`zrpc` / `zrpc-test` / `zrpc-sanitize` / `test-go` / `build` /
  `ccli`。`test-go` = 四模块关键用例（race）；`build` = 三个服务真实二进制（cgo 链接 libzrpc）。
  验证：`make test-go` 全绿；`make build` 产出 bin/{keywords-filter,ai-chat-service,ai-chat-backend}。
- **start.sh 增量感知**（§9.3）：在 GO_BUILDS 循环前新增 libzrpc 增量判断 ——
  `third_party/zrpc/{src,include,ntyco}` 或 `zrpc-go` 的 `.c/.h` 新于 `build/libzrpc.a` 时重建
  lib，并删除三个服务二进制强制重编（弥补原"只看各服务 *.go"的盲区）；`--rebuild` 先 `make clean`。
  `bash -n` 通过。

## 未完成（需在你环境跑真栈/镜像）

- 三个 Dockerfile 改为**仓库根 build context** 的多阶段构建（先 build libzrpc 再 go build）；
  运行镜像依赖 `ldd` 验证。需要本机 docker daemon。
- `ai-chat-stack/compose.yaml`：healthcheck 从 grpc_health_probe 换成 HTTP /readyz（端口已在
  keywords-filter dev 配置 18081/18082；chat-service health 端口未加）。
- NtyCo **优雅停机**：`nty_schedule_run` 无协程即退、`zrpc_server_shutdown` 现有 best-effort；
  跨线程唤醒在途 conn 协程的 stop-pipe/连接关闭编排仍需在真栈 SIGTERM 演练中收尾。
- **真栈端到端**：mock-openai-api + mysql + redis + 双栈三服务，同一批请求 gRPC vs zrpc 逐包对比
  （SSE/source/cache/扣费/断连），作为 Task 9 灰度门槛。运行方法与 §12 记录在案。

## 运行方式（用户环境）

```bash
make -j2 test-go          # 关键用例（含 C 与四模块，race）
make build                # 三个服务二进制
make zrpc-sanitize        # 纯 C sanitizer
# 真栈：起 mysql/redis/mock-openai-api 后 ./start.sh，再把对应 transport 置 zrpc 观察
```
