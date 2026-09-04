# zrpc 迁移 · Docker / compose / 运行镜像收尾（需 docker 环境验证）

日期：2026-09-04。当前环境无 docker daemon，下述为**可直接套用的模板**，未在本机验证；
应用后请在有 docker 的机器跑 `docker build` + compose up + `ldd` 确认。

## 1. 关键改动点（相比现在）

1. **三个服务 Dockerfile 的 build context 改仓库根**：原 Dockerfile 各自 `ADD ./ ...`，
   无法解析 `replace echo-zrpc-go => ../zrpc-go`（镜像内没有相邻 `zrpc-go/`）。
   → 统一从仓库根构建：`docker build -f ai-chat-service/Dockerfile -t echo/ai-chat-service .`
2. 镜像构建阶段**先 build libzrpc.a**（`make -C third_party/zrpc`）再 `go build`（CGO_ENABLED=1）。
3. 运行阶段若 `libzrpc.a` 全静态链接则无需复制 `.so`；用 `ldd` 验证最终依赖。
4. **healthcheck 从 grpc_health_probe 换 HTTP `/healthz` `/readyz`**：
   - keywords-filter：本分支已内置 HTTP health（dev 配置 `server.healthPort`，compose 里给两实例各开一个端口，如 18081/18082）。
   - ai-chat-service：镜像里加 HTTP health 服务（仿 keywords-filter main.go，端口可配置）。
5. compose 移除 `grpc_health_probe` binary 的 `ADD`（Dockerfile 内）。

## 2. ai-chat-service/Dockerfile 模板（仓库根 context）

```dockerfile
FROM golang:1.20-alpine AS build
RUN apk add --no-cache build-base linux-headers git
WORKDIR /src
# 先复制 zrpc C 源码并编译静态库（层缓存）
COPY third_party/zrpc ./third_party/zrpc
RUN make -C third_party/zrpc
# 复制共享 go 模块与服务（monorepo replace ../zrpc-go 在镜像内可解析）
COPY zrpc-go ./zrpc-go
COPY ai-chat-service ./ai-chat-service
RUN cd ai-chat-service && CGO_ENABLED=1 go build -o /out/ai-chat-service ./chat-server

FROM alpine:3.18
RUN apk add --no-cache libgcc ca-certificates tzdata
COPY --from=build /out/ai-chat-service /app/ai-chat-service
WORKDIR /app
EXPOSE 50055 50065
ENTRYPOINT ["/app/ai-chat-service", "--config=dev.config.yaml"]
```

keywords-filter / ai-chat-backend 同理（包路径 `./filter-server` / `./cmd/`，把 `zrpc-go`+`keywords-filter`
或 `zrpc-go`+`ai-chat-backend` 拷入）。

## 3. compose healthcheck 片段（替换 grpc_health_probe）

```yaml
  sensitive:
    image: <reg>/keywords-filter:1.0.0
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "-", "http://127.0.0.1:18081/readyz"]
      interval: 5s
      timeout: 2s
      retries: 3
      start_period: 5s
```

## 4. 应用前核对清单

- [ ] 三镜像从仓库根 `docker build -f <svc>/Dockerfile .` 能构建；`ldd` 确认无宿主 .so 依赖
- [ ] compose 起来后 `/healthz` `/readyz` 正常；`docker compose up` 滚动更新/回滚演练
- [ ] SIGTERM 优雅停机（NtyCo 调度线程 join，Task8b 已实现）在容器 stop 时 ≤ 宽限期退出
- [ ] 与 Task9 收尾清单合并执行（删 gRPC 前）
