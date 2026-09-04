# ECHO-CHAT gRPC → 自研 C zrpc v2 跨语言迁移 · 交付说明

分支：`zrpc-migration` ｜ 更新：2026-09-04 ｜ 详见 `docs/zrpc-migration/01..09`

## 迁移内容

用自研 **C zrpc v2 + NtyCo + cgo bridge** 替换 ECHO-CHAT 的 4 条 gRPC 调用，业务逻辑零改动：

| 链路 | 原 gRPC | 现 zrpc method（`zrpc-go/contract`） |
|---|---|---|
| backend → chat-service | ChatCompletion (unary) | `chat.completion` |
| backend → chat-service | ChatCompletionStream | `chat.completion_stream` |
| chat-service → keywords-filter | Validate | `filter.validate` |
| chat-service → keywords-filter | FindAll | `filter.find_all` |

## 技术栈与结构

- `third_party/zrpc/`：从零加固的 C 协议库（v2 帧/安全 IO/JSON 信封/方法表/鉴权/ping），
  server 用 **NtyCo 协程**调度；产出 `build/libzrpc.a`。
- `zrpc-go/`：cgo bridge（`Client.Unary/Stream`、`Server.RegisterUnary/RegisterStream`、handle 注册表、
  断连取消、优雅停机）；`contract/` 为共享契约（chat/filter）。
- 三个 Go 服务 `go.mod` `replace echo-zrpc-go => ../zrpc-go`。

## 端口与配置（默认 = gRPC 观察期，双栈并存）

| 服务 | gRPC | zrpc | HTTP health |
|---|---|---|---|
| keywords-filter sensitive / keywords | 50053 / 50054 | 50063 / 50064 | 18081 / 18082（`/healthz` `/readyz`） |
| ai-chat-service | 50055 | 50065 | — |
| ai-chat-backend | 7080（HTTP） | — | — |

灰度开关（每服务独立）：
- backend→chat：`ai-chat-backend/dev.config.yaml` → `dependOn.ai-chat-service.transport: grpc|zrpc`（zrpc 时地址 50065）。
- chat→filter：`ai-chat-service/dev.config.yaml` → `dependOn.sensitive|keywords.transport` + 地址 50063/50064。

## 使用（与之前一致）

```bash
./stop.sh     # 先停（幂等，旧 pid 不存在会跳过）
./start.sh    # 增量编译(含 libzrpc)+拉起全部服务；前端 dist 已在则不动 pnpm
```
- 日常不用管 zrpc：默认全 gRPC，行为与迁移前一致。
- 想压测/验证某段 zrpc：把对应 `transport` 翻成 `zrpc` 并重启该服务。
- 日志：各服务 `runtime/logs/<名>.log`；chat-service 自身的 logrus 在 `ai-chat-service/runtime/logs/app.log`
  （semcache 决策行前缀 `semcache_` 在此）。

## 已验证（真栈，2026-09-04）

- 四方法 gRPC==zrpc 语义一致：filter parity（8 黄金输入）、backend 敏感命中归一逐字节一致、LLM 流正常。
- `source` 分支：`llm`（真 DeepSeek + mock）、`cache`（语义缓存命中，`subject=avl_tree` 例）、敏感拦截。
- 浏览器断开 → 全链路取消（上游 LLM HTTP 释放、handler 释放、服务健康）。
- NtyCo 优雅停机（`Server.Close` join，无线程泄漏）；`make test-go` / `make build` 全绿；
  C 层普通套件与 sanitizer（纯 C）通过；filter unary 微基准 zrpc 不劣于 gRPC（详见 docs/08）。

## 尚未完成（需 docker / 部署环境）

- 三镜像仓库根 build context + compose healthz 换 HTTP + 容器 SIGTERM 演练（模板 docs/09）。
- 灰度实际放量（shadow→10%→50%→100%→观察期）与删 gRPC 收尾（清单 docs/07）。
- 更大样本 benchmark（跨机/多场景）。

## 文档索引（docs/zrpc-migration/）

01 基线 · 02 filter 迁移 · 03 chat unary · 04 backend stream · 05 构建缝合 · 06 本地 runbook（含语义缓存验证要点）·
07 Task9 灰度/回滚 · 08 filter benchmark · 09 Docker/compose 模板
