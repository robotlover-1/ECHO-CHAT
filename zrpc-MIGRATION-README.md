# ECHO-CHAT gRPC → 自研 C zrpc v2 迁移 · 交付说明

状态：**迁移完成，gRPC 传输已删，全链路仅自研 zrpc v2**。详见 `docs/zrpc-migration/01..12`。

## 迁移内容

用自研 **C zrpc v2 + NtyCo + cgo bridge** 替换 ECHO-CHAT 4 条 gRPC 调用，业务逻辑零改动：

| 链路 | 原 gRPC | 现 zrpc method（`zrpc-go/contract`） |
|---|---|---|
| backend → chat-service | ChatCompletion / ChatCompletionStream | `chat.completion` / `chat.completion_stream` |
| chat-service → keywords-filter | Validate / FindAll | `filter.validate` / `filter.find_all` |

## 技术栈与结构

- `third_party/zrpc/`：从零加固的 C 协议库（v2 帧/安全 IO/JSON 信封/方法表/鉴权/ping），server 用 **NtyCo**；产出 `build/libzrpc.a`。
- `zrpc-go/`：cgo bridge（`Client.Unary/Stream`、`Server.RegisterUnary/RegisterStream`、断连取消、优雅停机）；`contract/` 共享契约。
- 三个 Go 服务 `go.mod` `replace echo-zrpc-go => ../zrpc-go`（需 `CGO_ENABLED=1`）。

## 端口（单 zrpc）

| 服务 | 端口(zrpc) | HTTP 健康 |
|---|---|---|
| keywords-filter sensitive / keywords | 50053 / 50054 | 18081 / 18082（`/healthz` `/readyz`） |
| ai-chat-service | 50055 | 8080（`/healthz` `/readyz`，与 metrics 共用） |
| ai-chat-backend | 7080（HTTP） | `GET /api/health` |

## 使用

```bash
./stop.sh; ./start.sh     # 增量编译(含 libzrpc) + 拉起全部服务
```
- 服务端各自监听 zrpc 端口（见上）；compose healthcheck 已改 HTTP（需重建镜像后生效）。
- 日志：各服务 `runtime/logs/<名>.log`；chat-service 自身 logrus 在 `ai-chat-service/runtime/logs/app.log`（`semcache_` 决策行）。

## 已验证

- filter parity（gRPC 时代）与真栈：命中/语义缓存 `source=cache`/LLM `source=llm`/敏感拦截一致。
- 断连取消全链路（浏览器断开 → 上游 LLM 释放）；NtyCo 优雅停机（无线程泄漏）；`make test-go/build` 绿。
- gRPC 删除后单 zrpc：三模块编译与用例通过（proto 结构体保留为内部 DTO，gRPC-go 依赖仅供其编译）。

## 文档索引（docs/zrpc-migration/）

01 基线 · 02 filter · 03 chat unary · 04 backend · 05 构建 · 06 本地 runbook（含语义缓存要点）·
07 灰度历史 · 08 benchmark · 09 镜像模板 · 10 交付说明 · 11 实现步骤 · 12 zrpc 实现与使用。
