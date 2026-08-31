# ai-chat 收尾优化（入口限流 + 余额显示 + 死代码清理）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 ai-chat 补三件小事：#3 backend `/api` 挂入口限流（10/s burst 10）、#4 前端 Header 显示余额并随聊天刷新、#5 删前端死代码 + `/api/config` 返回真实模型。

**Architecture:** backend 改 `cmd/main.go`（挂限流、/config 加 model）；frontend 改 auth store 类型、Header 徽标、chat 发送完成后刷新 session。改动小且互相独立，分 3 个任务。

**Tech Stack:** Go（gin、golang.org/x/time/rate）、Vue3/pnpm、已运行的 backend :7080

## Global Constraints

- 限流参数：`RateLimitMiddleware(rate.Limit(10), 10)`（10 req/s、burst 10，全局单限流器，与 proxy 对齐）；超限返回 429
- `/api/config` 保留 `apiModel:"ChatGPTAPI"`（API 类型）与 `socksProxy:""`，新增 `"model": chat.config.Chat.Model`
- 前端余额：`authStore.session?.quota`（/session 已返回 quota）；`SessionResponse` 加 `quota?: number`；Header 徽标仅在有 quota 时显示
- 聊天完成刷新：`chat/index.vue` 的 `handleSend` 里 `await fetchChatAPIOnce()` 之后调 `authStore.getSession()`
- 删除前端未使用导出：`fetchChatAPI`（`/chat`）、`fetchVerify`（`/verify`）
- 提交只在 ai-chat 仓库 master，只加任务文件，**不提交 `www/` 构建产物**（用户工作区既有状态）；不动用户其他未提交改动
- 后端重启用正确 CWD：`( cd ai-chat-backend && nohup ../bin/ai-chat-backend --config=dev.config.yaml ... )`（保证 `www` 静态目录与相对日志路径正确）

---

### Task 1: #3 入口限流（backend）

**Files:**
- Modify: `ai-chat-backend/cmd/main.go`

- [ ] **Step 1: main.go 挂限流**

`ai-chat-backend/cmd/main.go`：
1) import 块加 `"golang.org/x/time/rate"`
2) 在 `chat := entry.Group("/api")` 之后（`chat.POST(...)` 之前）加一行：
```go
	chat.Use(middlewares.RateLimitMiddleware(rate.Limit(10), 10))
```

- [ ] **Step 2: 构建 + 重启 + 验证 429**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
fuser -k -TERM 7080/tcp 2>/dev/null; sleep 2
cd ai-chat-backend && export GOPROXY=https://goproxy.cn,direct && go build -o ../bin/ai-chat-backend ./cmd/ && cd ..
( cd ai-chat-backend && nohup ../bin/ai-chat-backend --config=dev.config.yaml > /tmp/backend-polish.log 2>&1 & echo $! > /tmp/backend.pid )
sleep 3
echo "=== 快速连发 20 个请求（burst 10 → 前~10 通过，之后 429）==="
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:7080/api/session)
  printf "%s " "$code"
done
echo
echo "=== 首页仍 200 ==="
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7080/
```

Expected: 前约 10 个 `200`，之后出现 `429`；首页 `200`。**8 个服务与 kvstore 不受影响。**

- [ ] **Step 3: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add ai-chat-backend/cmd/main.go
git commit -m "feat: ai-chat backend /api 挂入口限流（10/s burst 10）"
```

---

### Task 2: #4 前端余额显示（frontend）

**Files:**
- Modify: `ai-chat-web/src/store/modules/auth/index.ts`
- Modify: `ai-chat-web/src/views/chat/components/Header/index.vue`
- Modify: `ai-chat-web/src/views/chat/index.vue`

- [ ] **Step 1: auth store 加 quota 字段**

`ai-chat-web/src/store/modules/auth/index.ts` 的 `SessionResponse` 接口加：
```ts
interface SessionResponse {
  auth: boolean
  model: 'ChatGPTAPI' | 'ChatGPTUnofficialProxyAPI'
  quota?: number
}
```

- [ ] **Step 2: Header 加余额徽标**

`ai-chat-web/src/views/chat/components/Header/index.vue`：
1) `import { useAppStore, useChatStore } from '@/store'` → 加 `useAuthStore`：
```ts
import { useAppStore, useAuthStore, useChatStore } from '@/store'
```
2) script 里加：`const authStore = useAuthStore()`
3) template 右侧按钮区（`<div class="flex items-center space-x-2">` 内、上下文开关 `<HoverButton @click="toggleUsingContext">` 之前）加徽标：
```vue
        <span
          v-if="authStore.session?.quota !== undefined"
          class="text-xs whitespace-nowrap text-[#4f555e] dark:text-white"
        >
          额度 {{ authStore.session?.quota }}
        </span>
```

- [ ] **Step 3: 聊天完成后刷新 session**

`ai-chat-web/src/views/chat/index.vue`：
1) import 加 `useAuthStore`（`import { useAuthStore } from '@/store'`，若未引入），script 加 `const authStore = useAuthStore()`
2) `handleSend` 里 `await fetchChatAPIOnce()`（约 156 行）之后加一行：
```ts
    await fetchChatAPIOnce()
    authStore.getSession()   // 刷新余额
```
3) 若存在重试分支（另一个 `await fetchChatAPIProcess` 完成点，约 242 行），同样在其完成后加 `authStore.getSession()`

- [ ] **Step 4: 构建 + 端到端验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/ai-chat-web
pnpm build-only 2>&1 | tail -4
cp -r dist/. ../ai-chat-backend/www/
```

Expected: 构建成功无错。随后浏览器/curl 验证：
- 登录后 `GET/POST /api/session` 返回 `quota`（后端已有）
- 前端 Header 显示 `额度 100000`（需用户浏览器确认；或至少确认 session 响应含 quota）

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
curl -s -X POST http://localhost:7080/api/session -H "Authorization: <登录token>" | python3 -m json.tool 2>/dev/null | grep quota
```

- [ ] **Step 5: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add ai-chat-web/src/store/modules/auth/index.ts ai-chat-web/src/views/chat/components/Header/index.vue ai-chat-web/src/views/chat/index.vue
git commit -m "feat: ai-chat 前端 Header 余额显示 + 聊天后刷新"
```

---

### Task 3: #5 死代码清理 + /config 真实模型

**Files:**
- Modify: `ai-chat-web/src/api/index.ts`
- Modify: `ai-chat-backend/cmd/main.go`

- [ ] **Step 1: 删前端死代码**

`ai-chat-web/src/api/index.ts` 删除两个未使用导出：
- `fetchChatAPI`（请求 `/chat`，约 5-15 行）
- `fetchVerify`（请求 `/verify`，约 46-51 行）

删除后确认无其它文件 import 这两个函数（`grep -rn "fetchChatAPI\b\|fetchVerify" ai-chat-web/src --include='*.ts' --include='*.vue' | grep -v "api/index.ts"` 应为空）。

- [ ] **Step 2: /api/config 加真实 model**

`ai-chat-backend/cmd/main.go` 的 `/api/config` handler 的 data map 加一行（注意：该闭包里 `chat` 是 gin 路由组，**用 `config.GetConfig().Chat.Model`**，config 包 main.go 已 import）：
```go
			"model":      config.GetConfig().Chat.Model,
```
（保留 `apiModel` 与 `socksProxy` 两键。data 类型 `map[string]string`，model 为 string，无需改类型。）

- [ ] **Step 3: 构建验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
# 后端编译
cd ai-chat-backend && export GOPROXY=https://goproxy.cn,direct && go build ./... && cd ..
# 前端编译（顺带验证删死代码后无残留引用）
cd ai-chat-web && pnpm build-only 2>&1 | tail -3 && cd ..
```

Expected: 后端 `go build ./...` 通过；前端 `pnpm build-only` 通过（无 `fetchChatAPI is not defined` 之类错误）。

- [ ] **Step 4: 重启后端验证 /config**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
fuser -k -TERM 7080/tcp 2>/dev/null; sleep 2
cd ai-chat-backend && go build -o ../bin/ai-chat-backend ./cmd/ && cd ..
( cd ai-chat-backend && nohup ../bin/ai-chat-backend --config=dev.config.yaml > /tmp/backend-polish.log 2>&1 & echo $! > /tmp/backend.pid )
sleep 3
curl -s -X POST http://localhost:7080/api/config
```

Expected: `/api/config` 返回含 `"model":"gpt-3.5-turbo"`（真实模型名）+ `apiModel` + `socksProxy`。

- [ ] **Step 5: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add ai-chat-web/src/api/index.ts ai-chat-backend/cmd/main.go
git commit -m "feat: 删前端死代码 + /api/config 返回真实模型"
```

---

## 自审结论

- **Spec 覆盖**：#3 限流→Task1；#4 余额显示+刷新→Task2；#5 死代码+/config model→Task3；验收(429/余额/build/回归)→各任务验证步骤。
- **无占位符**：每处编辑给出精确 anchor（main.go 组挂载行、auth store 接口、Header 模板位置、handleSend 完成点）。
- **类型一致**：`quota?: number` 与后端 /session 返回的 `quota`（int）一致；`RateLimitMiddleware(rate.Limit(10), 10)` 与既有签名 `func RateLimitMiddleware(r rate.Limit, b int)` 匹配；`model` string 与 `map[string]string` 匹配。
- **风险标注**：Task1/3 会重启 backend(:7080)，不影响其余 7 个服务与 kvstore；前端 `www/` 构建产物不提交。
