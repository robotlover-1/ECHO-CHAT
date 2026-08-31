# ai-chat 收尾优化（入口限流 + 余额显示 + 死代码清理）设计文档

- 日期：2026-08-16
- 状态：已批准
- 位置：`ai-chat/`（backend + frontend）

## 背景

登录鉴权 + 计费已上线。本设计收尾三个小项：#3 backend 入口限流未挂、#4 前端不显示余额、#5 死代码与 /config 硬编码占位。

## 已确认决策

- #3：给 backend `/api` 路由组挂 `RateLimitMiddleware`（10 req/s、burst 10，与 openai-api-proxy 对齐）；全局单限流器（本地单用户足够）
- #4：前端 Header 加余额徽标（读 `authStore.session?.quota`），聊天完成后刷新 session 让余额跟着走
- #5：删除前端未使用的 `fetchChatAPI`(`/chat`)、`fetchVerify`(`/verify`)；`/api/config` 新增真实 `model` 字段（保留 `apiModel` 作为 API 类型）
- 不触碰知识库（腾讯云不可达，流程自动降级）、不触碰 kvstore exporter（#7 不做）

## #3 入口限流（backend）

`ai-chat-backend/cmd/main.go`：
- 给 `/api` 路由组 `chat` 挂中间件：`chat.Use(middlewares.RateLimitMiddleware(rate.Limit(10), 10))`
- import 加 `"golang.org/x/time/rate"`（backend 已是依赖 v0.5.0）
- `RateLimitMiddleware`（已实现于 `pkg/middlewares/rate_limit.go`）超过限额返回 429

## #4 前端余额显示（frontend）

- `ai-chat-web/src/store/modules/auth/index.ts`：`SessionResponse` 加 `quota?: number`（/session 已返回 quota）
- `ai-chat-web/src/views/chat/components/Header/index.vue`：右侧按钮区（上下文开关旁）加余额徽标 `额度 {{ authStore.session?.quota }}`（登录有 quota 时才显示），import `useAuthStore`
- `ai-chat-web/src/views/chat/index.vue`：聊天流完成后调 `authStore.getSession()` 刷新 quota（发完一条消息后余额减少）

## #5 死代码清理 + /config 真实模型

- `ai-chat-web/src/api/index.ts`：删除 `fetchChatAPI`（`/chat`）、`fetchVerify`（`/verify`）两个未使用导出
- `ai-chat-backend/cmd/main.go` 的 `/api/config`：data 增加 `"model": chat.config.Chat.Model`（真实模型名），保留 `apiModel:"ChatGPTAPI"`、`socksProxy:""`

## 验收标准

1. #3：连发超过 10/s 的请求 → 429；正常频率请求正常
2. #4：登录后 Header 显示额度；发一条消息后额度减少（前端刷新）
3. #5：`pnpm build-only` 通过（无死 import）；About 页显示真实模型名
4. 回归：登录/聊天/计费/401/402 流程不受影响
5. 提交只在 ai-chat 仓库 master，只加任务文件，不动用户其他未提交改动（含 www/ 构建产物）

## 明确不做（Out of scope）

- 知识库打通（腾讯云向量库不可达，需换本地方案，另立项）
- kvstore Prometheus exporter（#7）
- 前端充值页（无充值流）
- 修改 ai-chat-service / kvstore 源码
