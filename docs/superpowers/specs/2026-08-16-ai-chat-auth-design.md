# ai-chat 登录鉴权 + 额度计费 设计文档

- 日期：2026-08-16
- 状态：已批准
- 位置：`ai-chat/` 内，全部在 **ai-chat-backend** 实现（方案 A）

## 背景与目标

ai-chat 目前**无用户体系**：前端留了手机号+验证码登录 UI（Permission.vue）但被硬编码绕过（`setToken('helloxx')`），后端无 `/v1/sms/send/code`、`/v1/user/login` 路由，`/api/session` 硬编码 `auth:false`。本设计补齐：登录鉴权（手机号+验证码）+ 额度计费（参考 InazumaPlasma 的 quota 模式），全部在 ai-chat-backend 实现，验证码/session 存 kvstore、用户存 MySQL。

## 已确认决策

- 用户存 MySQL（`users` 表），验证码/session 存 kvstore
- **加额度计费**：新用户初始额度 100000，聊天按 token 扣减，额度不足拒绝
- **开关可控**：`auth_enabled` 为 true 时 `/chat-process` 强制鉴权，false 回到现状（不登录也能聊）
- 全部在 ai-chat-backend 实现（不动 ai-chat-service / gRPC 协议）
- 计费 token 数由 backend 用自身 tokenizer（修复 URL 从配置读）估算
- 额度不足返回 **402**；初始额度 **100000**
- 验证码"发送"= 打印到日志/控制台（本地无真实短信服务商）

## 数据模型

### kvstore（SETEX/GET，go-redis 已验证兼容）
| Key | TTL | 值 |
|---|---|---|
| `sms_code:<phone>` | 300s | 6 位数字验证码 |
| `session:<token>` | 604800s(7天) | phone |

### MySQL（`ai_chat` 库，新建 `users` 表）
```sql
CREATE TABLE IF NOT EXISTS users (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  phone       VARCHAR(20) NOT NULL UNIQUE,
  quota       INT NOT NULL DEFAULT 100000,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 接口（ai-chat-backend，gin，baseURL=/api）

| 接口 | 逻辑 |
|---|---|
| `POST /v1/sms/send/code` `{phone}` | 校验手机号格式 → 生成 6 位码 → `SETEX sms_code:<phone> 300 <code>` → `log` 打印验证码 → 返回 `{status:"Success"}` |
| `POST /v1/user/login` `{user_name:phone, pwd:code, type:1}` | `GET sms_code:<phone>` 校验码（错/过期→400）→ upsert `users`（新用户 quota=100000）→ 生成随机 token → `SETEX session:<token> 604800 <phone>` → 返回 `{status:"Success", access_token:<token>}`（**access_token 放顶层**，前端 `data.access_token` 读取） |
| `POST /session` | `auth_enabled` 关 → `{status:"Success", data:{auth:false}}`；开 → 校验请求头 token（`GET session:<token>`）→ 有效 `{status:"Success", data:{auth:true, model:"ChatGPTAPI", phone, quota}}` / 无效 `{status:"Success", data:{auth:true, model:"ChatGPTAPI"}}`（前端据此显示登录页） |
| `POST /chat-process` | 挂鉴权中间件（见下）+ 计费（见下） |

## 鉴权中间件（`/chat-process`）

- `auth_enabled` 关：直接放行（回到现状）
- 开：读 `Authorization` 头（兼容**不带 "Bearer " 前缀**，前端 axios 拦截器就是裸 token）→ `GET session:<token>` → 命中放行并附带 phone；未命中 `401 {"status":"Fail","message":"未登录或登录已过期"}`

## 计费流

```
登录 → users.quota=100000
发消息 → 鉴权拿 phone → SELECT quota WHERE phone=? → quota<=0 → 402 "额度不足"
      → 转发 gRPC 流式收答案（chat.go 已累积 result.Text）
      → 完成后：tokenizer(prompt) + tokenizer(累计响应文本)
      → UPDATE users SET quota=quota-Σ WHERE phone=?
```

- backend `pkg/tokenizer` 的 URL 从硬编码 `192.168.239.161:3002` 改为读配置（`tokenizer.address`，默认 `127.0.0.1:3002`）
- 计费失败不阻塞回复（记录日志即可）；额度不足在**聊天前**拦截

## 前端改动（ai-chat-web）

1. 删 `views/chat/layout/Permission.vue` 的 `authStore.setToken('helloxx')`，恢复 `getSession()` 门控（`auth:true` 时显示登录页）
2. 理顺 token 流：axios 拦截器（`utils/request/axios.ts`）读 **cookie** `sso_0voice_access_token` → 登录成功改写这个 cookie（而非 `localStorage.access_token`）；Authorization 头保持裸 token，后端兼容
3. 中性化 401 兜底：`utils/request/index.ts` 的 `failHandler` 现在 401 会跳转 `https://user.0voice.com?sys=ai`（原版 SSO 遗留）——本地登录场景改为「清除 cookie + 回到登录页」（`window.location.reload()`），避免跳到外部站点
4. 登录页 UI 已存在，不改样式

## 配置（ai-chat-backend/dev.config.yaml 新增）

```yaml
redis:
  host: "127.0.0.1"
  port: 5160
  pwd: "123456"
mysql:
  dsn: "root:123456@tcp(127.0.0.1:3306)/ai_chat?charset=utf8mb4"
  maxOpenConn: 10
  maxIdleConn: 10
auth:
  enabled: true
  init_quota: 100000
tokenizer:
  address: "http://127.0.0.1:3002"
```

## 新增依赖（ai-chat-backend/go.mod）

- `github.com/redis/go-redis/v9`（kvstore 客户端，版本对齐 ai-chat-service）
- `github.com/go-sql-driver/mysql`（MySQL 驱动）

## 验收标准

1. `auth_enabled:true`：未带/无效 token 访问 `/chat-process` → 401；登录后可正常聊天
2. 发验证码（日志出现 6 位码）→ 登录（返回 access_token）→ `/session` 返回 auth:true+phone/quota
3. 计费：聊几次后 `SELECT quota` 减少；把 quota UPDATE 成 0 → 再聊返回 402
4. `auth_enabled:false`：不登录也能聊（回到现状）
5. 前端：登录页正常显示、验证码/登录流程通、登录后进聊天页；Authorization 头带上 token
6. 新增依赖编译通过（`go build` 无错）

## 明确不做（Out of scope）

- 真实短信服务商接入（验证码仅日志输出）
- 前端额度展示/充值页面（计费仅后端强制，前端不显示余额）
- 知识库检索打通、真实大模型接入、入口限流挂载（均为之前发现的其它缺口，本次不涉及）
- 修改 ai-chat-service / gRPC 协议
