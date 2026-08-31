# ai-chat 登录鉴权 + 额度计费 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ai-chat-backend 实现手机号+验证码登录鉴权（验证码/session 存 kvstore、用户存 MySQL），并在 `/chat-process` 挂鉴权中间件 + 按 token 扣减额度（quota），同时解锁前端登录门。

**Architecture:** 全部改动集中在 ai-chat-backend（方案 A）：
- kvstore 存验证码（`sms_code:<phone>` TTL 300s）和会话（`session:<token>` TTL 7天），用 go-redis（版本对齐 ai-chat-service 的 v9.6.1）
- MySQL 存 `users` 表（phone/quota），用 go-sql-driver/mysql v1.8.1
- 三个新接口：发验证码、登录、session；`/chat-process` 挂鉴权中间件
- 计费：聊天前查 quota>0（否则 402），聊天完成后用 backend 自带的 tokenizer（修复 URL 从配置读）数 prompt+响应 token 扣减
- 前端解锁：Layout.vue 恢复 Permission 门控、Permission.vue 移除 setToken('helloxx')、token 流统一到 localStorage、中性化 0voice 401 跳转

**Tech Stack:** Go 1.19（gin、viper、go-redis v9.6.1、go-sql-driver/mysql v1.8.1）、kvstore:5160、MySQL:3306、Vue3/pnpm

## Global Constraints

- kvstore 连接：127.0.0.1:5160、pwd 123456（与 ai-chat-service 一致）；MySQL DSN：`root:123456@tcp(127.0.0.1:3306)/ai_chat?charset=utf8mb4`
- kvstore key：验证码 `sms_code:<phone>`（TTL 300s）、会话 `session:<token>`（TTL 604800s）
- `users` 表：`id INT AUTO_INCREMENT PK, phone VARCHAR(20) UNIQUE, quota INT DEFAULT 100000, created_at TIMESTAMP`
- 新用户初始额度 100000（config `init_quota`）；额度不足返回 **402**
- 鉴权开关：config `auth_enabled`（true 强制，false 回到现状）；Authorization 头**裸 token**（不带 "Bearer " 前缀，前端拦截器如此）
- 手机号正则：`^1\d{10}$`（与前端 `1\d{10}$` 一致）
- 登录返回**顶层** `access_token`（前端 `data.access_token` 读取）
- `/session` 返回 `{status:"Success", data:{auth, model:"ChatGPTAPI", phone?, quota?}}`
- 验证码"发送"= 打印到日志（无真实短信商）
- 不修改 ai-chat-service / gRPC 协议 / kvstore 源码
- 构建命令：`GOPROXY=https://goproxy.cn,direct go build ./...`（新依赖经 goproxy.cn 拉取）
- 提交只在 ai-chat 仓库 master；`git add` 只加任务文件，不动用户其他未提交改动

---

### Task 1: 后端依赖 + 配置 + kvstore/MySQL 客户端 + users 数据层

**Files:**
- Modify: `ai-chat-backend/go.mod`（+go-redis v9.6.1、+go-sql-driver/mysql v1.8.1）
- Modify: `ai-chat-backend/pkg/config/config.go`（+Redis/Mysql/Auth/Tokenizer 结构体）
- Modify: `ai-chat-backend/dev.config.yaml`（+redis/mysql/auth_enabled/init_quota/tokenizer 段）
- Create: `ai-chat-backend/pkg/db/redis/redis.go`
- Create: `ai-chat-backend/pkg/db/mysql/mysql.go`
- Create: `ai-chat-backend/pkg/users/users.go`
- Modify: `ai-chat-backend/cmd/main.go`（初始化 users 表）

**Interfaces:**
- Produces: `pkg/db/redis.GetPool()/SetEx(ctx,key,val,ttl)/Get(ctx,key)`、`pkg/db/mysql.GetDB()/InitUsersTable()`、`pkg/users.GetByPhone/UpgetByPhone/DeductQuota`、config 新字段 `Redis.Host/Port/Pwd`、`Mysql.DSN/MaxOpenConn/MaxIdleConn`、`Auth.Enabled/InitQuota`、`Tokenizer.Address`
- Consumes: 无（地基）

- [ ] **Step 1: 拉取依赖**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/ai-chat-backend
export GOPROXY=https://goproxy.cn,direct
go get github.com/redis/go-redis/v9@v9.6.1
go get github.com/go-sql-driver/mysql@v1.8.1
go mod tidy
```

Expected: `go.mod` require 块新增两行；`go.sum` 更新；无报错。

- [ ] **Step 2: 扩展 config.go**

在 `Config` 结构体 `DependOn` 之后追加（`ai-chat-backend/pkg/config/config.go`）：
```go
	Redis struct {
		Host string
		Port int
		Pwd  string `mapstructure:"pwd"`
	}
	Mysql struct {
		DSN         string
		MaxOpenConn int
		MaxIdleConn int
	}
	Auth struct {
		Enabled   bool `mapstructure:"enabled"`
		InitQuota int  `mapstructure:"init_quota"`
	}
	Tokenizer struct {
		Address string
	}
```

- [ ] **Step 3: 扩展 dev.config.yaml**

在 `ai-chat-backend/dev.config.yaml` 末尾（`dependOn` 段之后）追加：
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

- [ ] **Step 4: 创建 kvstore 客户端** `pkg/db/redis/redis.go`

```go
package redis

import (
	"context"
	"fmt"
	"sync"
	"time"

	"ai-chat-backend/pkg/config"

	"github.com/redis/go-redis/v9"
)

var (
	pool *redis.Client
	once sync.Once
)

func GetPool() *redis.Client {
	once.Do(func() {
		cnf := config.GetConfig()
		pool = redis.NewClient(&redis.Options{
			Addr:     fmt.Sprintf("%s:%d", cnf.Redis.Host, cnf.Redis.Port),
			Password: cnf.Redis.Pwd,
		})
	})
	return pool
}

func SetEx(ctx context.Context, key, value string, ttl time.Duration) error {
	return GetPool().SetEx(ctx, key, value, ttl).Err()
}

func Get(ctx context.Context, key string) (string, error) {
	return GetPool().Get(ctx, key).Result()
}
```

- [ ] **Step 5: 创建 MySQL 客户端 + users 表** `pkg/db/mysql/mysql.go`

```go
package mysql

import (
	"database/sql"
	"fmt"
	"sync"

	"ai-chat-backend/pkg/config"

	_ "github.com/go-sql-driver/mysql"
)

var (
	db   *sql.DB
	once sync.Once
)

func GetDB() *sql.DB {
	once.Do(func() {
		cnf := config.GetConfig()
		var err error
		db, err = sql.Open("mysql", cnf.Mysql.DSN)
		if err != nil {
			panic(fmt.Sprintf("mysql open: %v", err))
		}
		db.SetMaxOpenConns(cnf.Mysql.MaxOpenConn)
		db.SetMaxIdleConns(cnf.Mysql.MaxIdleConn)
	})
	return db
}

func InitUsersTable() error {
	_, err := GetDB().Exec(`CREATE TABLE IF NOT EXISTS users (
		id INT AUTO_INCREMENT PRIMARY KEY,
		phone VARCHAR(20) NOT NULL UNIQUE,
		quota INT NOT NULL DEFAULT 100000,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	)`)
	return err
}
```

- [ ] **Step 6: 创建 users 数据层** `pkg/users/users.go`

```go
package users

import (
	"database/sql"
	"errors"

	db "ai-chat-backend/pkg/db/mysql"
)

type User struct {
	ID    int64
	Phone string
	Quota int
}

func GetByPhone(phone string) (*User, error) {
	u := &User{}
	err := db.GetDB().QueryRow(
		"SELECT id, phone, quota FROM users WHERE phone = ?", phone,
	).Scan(&u.ID, &u.Phone, &u.Quota)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return u, nil
}

// UpsertByPhone: 已存在返回现有行；不存在插入新用户（initQuota 初始额度）
func UpsertByPhone(phone string, initQuota int) (*User, error) {
	u, err := GetByPhone(phone)
	if err != nil {
		return nil, err
	}
	if u != nil {
		return u, nil
	}
	if _, err := db.GetDB().Exec(
		"INSERT INTO users (phone, quota) VALUES (?, ?)", phone, initQuota,
	); err != nil {
		return nil, err
	}
	return GetByPhone(phone)
}

func DeductQuota(phone string, tokens int) error {
	if tokens <= 0 {
		return nil
	}
	_, err := db.GetDB().Exec(
		"UPDATE users SET quota = quota - ? WHERE phone = ?", tokens, phone,
	)
	return err
}
```

- [ ] **Step 7: main.go 初始化 users 表**

在 `ai-chat-backend/cmd/main.go` 的 `httpServer` 函数开头（`chatService, err := controllers.NewChatService(...)` 之前）插入：
```go
	if err := mysqlpkg.InitUsersTable(); err != nil {
		r.log.FatalF("init users table: %v", err)
	}
```
并在 import 块加：`mysqlpkg "ai-chat-backend/pkg/db/mysql"`

- [ ] **Step 8: 构建 + 冒烟测试**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/ai-chat-backend
export GOPROXY=https://goproxy.cn,direct
go build ./... && echo "build OK"
mysql -h127.0.0.1 -uroot -p123456 ai_chat -e "DESC users;" 2>/dev/null || echo "（表由后端启动时创建）"
```

Expected: `build OK`。users 表可经后端启动创建（Step 8 不动服务，可在 Task 2 启动后验证）。

- [ ] **Step 9: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add ai-chat-backend/go.mod ai-chat-backend/go.sum ai-chat-backend/pkg/config/config.go ai-chat-backend/dev.config.yaml ai-chat-backend/pkg/db ai-chat-backend/pkg/users ai-chat-backend/cmd/main.go
git commit -m "feat: ai-chat 登录鉴权地基——kvstore/mysql 客户端 + users 表 + 配置"
```

---

### Task 2: 鉴权接口（发验证码 / 登录 / session）

**Files:**
- Create: `ai-chat-backend/pkg/controllers/auth.go`
- Modify: `ai-chat-backend/cmd/main.go`（注册路由，替换内联 /session）

**Interfaces:**
- Consumes: Task 1 的 `pkg/db/redis`、`pkg/users`、`config.Auth/Redis`
- Produces: `ChatService.SendCode/Login/Session` gin 处理器；路由 `POST /api/v1/sms/send/code`、`POST /api/v1/user/login`、`POST /api/session`

- [ ] **Step 1: 创建 auth 控制器** `pkg/controllers/auth.go`

```go
package controllers

import (
	"context"
	"crypto/rand"
	"fmt"
	"math/big"
	"regexp"
	"time"

	"ai-chat-backend/pkg/config"
	kredis "ai-chat-backend/pkg/db/redis"
	"ai-chat-backend/pkg/users"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

var phoneRe = regexp.MustCompile(`^1\d{10}$`)

func (chat *ChatService) SendCode(ctx *gin.Context) {
	var req struct {
		Phone string `json:"phone"`
	}
	if err := ctx.BindJSON(&req); err != nil {
		ctx.JSON(400, gin.H{"status": "Fail", "message": "参数错误", "data": nil})
		return
	}
	if !phoneRe.MatchString(req.Phone) {
		ctx.JSON(400, gin.H{"status": "Fail", "message": "手机号格式不正确", "data": nil})
		return
	}
	code := rand6()
	if err := kredis.SetEx(context.Background(), "sms_code:"+req.Phone, code, 5*time.Minute); err != nil {
		chat.log.Error(err)
		ctx.JSON(500, gin.H{"status": "Fail", "message": "验证码发送失败", "data": nil})
		return
	}
	chat.log.InfoF("验证码已发送 phone=%s code=%s", req.Phone, code)
	ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": nil})
}

func (chat *ChatService) Login(ctx *gin.Context) {
	var req struct {
		UserName string `json:"user_name"`
		Pwd      string `json:"pwd"`
		Type     int    `json:"type"`
	}
	if err := ctx.BindJSON(&req); err != nil {
		ctx.JSON(400, gin.H{"status": "Fail", "message": "参数错误", "data": nil})
		return
	}
	phone := req.UserName
	if !phoneRe.MatchString(phone) {
		ctx.JSON(400, gin.H{"status": "Fail", "message": "手机号格式不正确", "data": nil})
		return
	}
	saved, err := kredis.Get(context.Background(), "sms_code:"+phone)
	if err != nil || saved != req.Pwd {
		ctx.JSON(400, gin.H{"status": "Fail", "message": "验证码错误或已过期", "data": nil})
		return
	}
	user, err := users.UpsertByPhone(phone, config.GetConfig().Auth.InitQuota)
	if err != nil {
		chat.log.Error(err)
		ctx.JSON(500, gin.H{"status": "Fail", "message": "登录失败", "data": nil})
		return
	}
	_ = kredis.GetPool().Del(context.Background(), "sms_code:"+phone)
	token := uuid.New().String()
	if err := kredis.SetEx(context.Background(), "session:"+token, phone, 7*24*time.Hour); err != nil {
		chat.log.Error(err)
		ctx.JSON(500, gin.H{"status": "Fail", "message": "登录失败", "data": nil})
		return
	}
	ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"quota": user.Quota}, "access_token": token})
}

func (chat *ChatService) Session(ctx *gin.Context) {
	cnf := config.GetConfig()
	if !cnf.Auth.Enabled {
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": false}})
		return
	}
	token := ctx.GetHeader("Authorization")
	phone, err := kredis.Get(context.Background(), "session:"+token)
	if err != nil {
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI"}})
		return
	}
	user, err := users.GetByPhone(phone)
	if err != nil || user == nil {
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI"}})
		return
	}
	ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI", "phone": phone, "quota": user.Quota}})
}

func rand6() string {
	n, _ := rand.Int(rand.Reader, big.NewInt(1000000))
	return fmt.Sprintf("%06d", n.Int64())
}
```

> 说明：`chat.log` 的类型 `log.ILogger` 定义在 pkg/log，本文件不直接引用 `log.` 符号，故**不 import `pkg/log`**（避免未使用 import 编译错误）。

- [ ] **Step 2: main.go 注册路由**

`ai-chat-backend/cmd/main.go` 的 `chat` 路由组内，把内联 `/session` handler 替换为控制器方法，并新增两条路由：
```go
	chat.POST("/chat-process", chatService.ChatProcess)
	chat.POST("/config", func(ctx *gin.Context) { /* 保持不变 */ })
	chat.POST("/session", chatService.Session)
	chat.POST("/v1/sms/send/code", chatService.SendCode)
	chat.POST("/v1/user/login", chatService.Login)
	chat.GET("/health", func(c *gin.Context) {})
```
（`/config` 与 `/health` 保持原样。）

- [ ] **Step 3: 构建 + 启动 backend 验证接口**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
# 重启 backend（先用 fuser 停旧实例，再起新编译的）
fuser -k -TERM 7080/tcp 2>/dev/null; sleep 2
cd ai-chat-backend && go build -o ../bin/ai-chat-backend ./cmd/ && cd ..
./bin/ai-chat-backend --config=ai-chat-backend/dev.config.yaml > /tmp/backend-auth-test.log 2>&1 &
sleep 3

echo "=== 发验证码 ==="
curl -s -X POST http://localhost:7080/api/v1/sms/send/code -H 'Content-Type: application/json' -d '{"phone":"13800138000"}'
echo
echo "=== 从日志取验证码 ==="
CODE=$(grep -oE 'code=[0-9]{6}' /tmp/backend-auth-test.log | tail -1 | cut -d= -f2)
echo "验证码: $CODE"
echo "=== 登录 ==="
TOKEN=$(curl -s -X POST http://localhost:7080/api/v1/user/login -H 'Content-Type: application/json' -d "{\"user_name\":\"13800138000\",\"pwd\":\"$CODE\",\"type\":1}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
echo "access_token: $TOKEN"
echo "=== session（带 token）==="
curl -s -X POST http://localhost:7080/api/session -H "Authorization: $TOKEN"
echo
echo "=== session（不带 token，应 auth:true 无用户）==="
curl -s -X POST http://localhost:7080/api/session
```

Expected: 发码返回 Success 且日志出现 6 位码；登录返回顶层 access_token；带 token 的 session 返回 `auth:true`+phone+quota；不带 token 返回 `auth:true`（无 phone）。**期间 8 个服务与 kvstore 保持运行。**

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add ai-chat-backend/pkg/controllers/auth.go ai-chat-backend/cmd/main.go
git commit -m "feat: ai-chat 登录鉴权接口（验证码/登录/session）"
```

> 注意：Task 2 之后 backend 进程可能被任务里重启过。Task 3 会再重启；期间不依赖 backend 常驻。

---

### Task 3: 鉴权中间件 + 计费 + tokenizer 修复

**Files:**
- Create: `ai-chat-backend/pkg/middlewares/auth.go`
- Modify: `ai-chat-backend/pkg/controllers/chat.go`（ChatProcess 计费）
- Modify: `ai-chat-backend/pkg/tokenizer/tokenizer.go`（URL 从配置读）
- Modify: `ai-chat-backend/cmd/main.go`（/chat-process 挂中间件）

**Interfaces:**
- Consumes: Task 2 的 session 存储（`session:<token>`→phone）、Task 1 的 `pkg/users.DeductQuota`、config `Auth.Enabled`、`Tokenizer.Address`
- Produces: `middlewares.AuthMiddleware()` gin 中间件（c.Set("phone", phone)）；ChatProcess 计费逻辑

- [ ] **Step 1: 创建鉴权中间件** `pkg/middlewares/auth.go`

```go
package middlewares

import (
	"context"
	"net/http"

	"ai-chat-backend/pkg/config"
	kredis "ai-chat-backend/pkg/db/redis"

	"github.com/gin-gonic/gin"
)

func AuthMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		if !config.GetConfig().Auth.Enabled {
			c.Next()
			return
		}
		token := c.GetHeader("Authorization")
		phone, err := kredis.Get(context.Background(), "session:"+token)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"status": "Fail", "message": "未登录或登录已过期", "data": nil})
			return
		}
		c.Set("phone", phone)
		c.Next()
	}
}
```

- [ ] **Step 2: main.go 挂中间件**

`ai-chat-backend/cmd/main.go` 路由：
```go
	chat.POST("/chat-process", middlewares.AuthMiddleware(), chatService.ChatProcess)
```
（import 已含 `middlewares` 包。）

- [ ] **Step 3: 修复 tokenizer URL** `pkg/tokenizer/tokenizer.go`

`GetTokenCount` 内硬编码 URL 改为从配置读：
```go
func GetTokenCount(message openai.ChatCompletionMessage, model string) (int, error) {
	base := config.GetConfig().Tokenizer.Address
	url := fmt.Sprintf("%s/tokenizer/%s", base, model)
	...
}
```
（文件顶部加 `"ai-chat-backend/pkg/config"` import。）

- [ ] **Step 4: ChatProcess 加计费**

`ai-chat-backend/pkg/controllers/chat.go`：

1) import 加：
```go
	"ai-chat-backend/pkg/tokenizer"
	"ai-chat-backend/pkg/users"
	"github.com/sashabaranov/go-openai"
```
（`openai` 已 import，确认存在。）

2) `ChatProcess` 开头（`payload := ChatMessageRequest{}` 之后、`messageID := uuid.New().String()` 之前）插入额度检查：
```go
	phone, _ := ctx.Get("phone")
	if chat.config.Auth.Enabled {
		user, err := users.GetByPhone(phone.(string))
		if err != nil || user == nil || user.Quota <= 0 {
			ctx.JSON(402, gin.H{"status": "Fail", "message": "额度不足，请充值后再试", "data": nil})
			return
		}
	}
```

3) 在 `for { ... }` 循环的 **EOF 分支**（`if errors.Is(err, io.EOF) { return }`）里、`return` 之前插入计费扣减：
```go
		if errors.Is(err, io.EOF) {
			// 正常流结束 → 计费扣减（chat.go 的 ChatProcess 里）
			if chat.config.Auth.Enabled {
				promptMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleUser, Content: payload.Prompt}
				respMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleAssistant, Content: result.Text}
				pt, err1 := tokenizer.GetTokenCount(promptMsg, chat.config.Chat.Model)
				rt, err2 := tokenizer.GetTokenCount(respMsg, chat.config.Chat.Model)
				if err1 == nil && err2 == nil {
					if err := users.DeductQuota(phone.(string), pt+rt); err != nil {
						chat.log.Error(err)
					}
				} else {
					chat.log.ErrorF("计费 token 统计失败: %v / %v", err1, err2)
				}
			}
			return
		}
```

> 关键：ChatProcess 的正常流结束是 `for` 循环里 EOF 分支的 `return`（循环后无代码）。计费块**必须插在该 `return` 之前**，否则永远不执行。错误分支（非 EOF 的 err、Writer.Write err）不扣费，保持原样。

- [ ] **Step 5: 构建 + 端到端计费验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
fuser -k -TERM 7080/tcp 2>/dev/null; sleep 2
cd ai-chat-backend && go build -o ../bin/ai-chat-backend ./cmd/ && cd ..
./bin/ai-chat-backend --config=ai-chat-backend/dev.config.yaml > /tmp/backend-auth-test.log 2>&1 &
sleep 3

echo "=== 未带 token 访问 chat-process → 401 ==="
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:7080/api/chat-process \
  -H 'Content-Type: application/json' -d '{"prompt":"你好","options":{}}'

echo "=== 登录拿 token ==="
curl -s -X POST http://localhost:7080/api/v1/sms/send/code -H 'Content-Type: application/json' -d '{"phone":"13800138000"}' > /dev/null
CODE=$(grep -oE 'code=[0-9]{6}' /tmp/backend-auth-test.log | tail -1 | cut -d= -f2)
TOKEN=$(curl -s -X POST http://localhost:7080/api/v1/user/login -H 'Content-Type: application/json' -d "{\"user_name\":\"13800138000\",\"pwd\":\"$CODE\",\"type\":1}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

echo "=== 登录前 quota ==="
mysql -h127.0.0.1 -uroot -p123456 ai_chat -e "SELECT phone, quota FROM users WHERE phone='13800138000';" 2>/dev/null

echo "=== 带 token 聊天 ==="
curl -s -X POST http://localhost:7080/api/chat-process -H "Authorization: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"prompt":"你好","options":{}}' | head -c 100; echo

sleep 1
echo "=== 聊天后 quota（应减少）==="
mysql -h127.0.0.1 -uroot -p123456 ai_chat -e "SELECT phone, quota FROM users WHERE phone='13800138000';" 2>/dev/null

echo "=== 额度清零后再聊 → 402 ==="
mysql -h127.0.0.1 -uroot -p123456 ai_chat -e "UPDATE users SET quota=0 WHERE phone='13800138000';" 2>/dev/null
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:7080/api/chat-process \
  -H "Authorization: $TOKEN" -H 'Content-Type: application/json' -d '{"prompt":"你好","options":{}}'

echo "=== 恢复额度 ==="
mysql -h127.0.0.1 -uroot -p123456 ai_chat -e "UPDATE users SET quota=100000 WHERE phone='13800138000';" 2>/dev/null
```

Expected: 无 token 401；聊天后 quota 减少；quota=0 时返回 402；结束后恢复 quota=100000。

- [ ] **Step 6: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add ai-chat-backend/pkg/middlewares/auth.go ai-chat-backend/pkg/controllers/chat.go ai-chat-backend/pkg/tokenizer/tokenizer.go ai-chat-backend/cmd/main.go
git commit -m "feat: ai-chat chat-process 鉴权中间件 + 额度计费 + tokenizer 配置化"
```

---

### Task 4: 前端解锁 + 重建

**Files:**
- Modify: `ai-chat-web/src/views/chat/layout/Layout.vue`（恢复 Permission 门控）
- Modify: `ai-chat-web/src/views/chat/layout/Permission.vue`（去绕过 + token 写 store）
- Modify: `ai-chat-web/src/utils/request/axios.ts`（读 localStorage 而非 cookie）
- Modify: `ai-chat-web/src/utils/request/index.ts`（401 不再跳 0voice）
- 重建前端并复制到 `ai-chat-backend/www/`

**Interfaces:**
- Consumes: 后端 `POST /api/session` 返回 `{status, data:{auth, model}}`；`POST /api/v1/sms/send/code`、`POST /api/v1/user/login` 返回顶层 `access_token`；`/api/chat-process` 读 `Authorization` 裸 token

- [ ] **Step 1: Layout.vue 恢复登录门控**

`ai-chat-web/src/views/chat/layout/Layout.vue`：
1) 取消注释 import：
```ts
import Permission from './Permission.vue'
import { useAuthStore } from '@/store'
```
2) 取消注释 store 引用：`// const authStore = useAuthStore()` → `const authStore = useAuthStore()`
3) 在 `<script setup>` 末尾（`getContainerClass` 之后）恢复 needPermission 并在 onBeforeMount 拉 session：
```ts
const needPermission = computed(() => !!authStore.session?.auth && !authStore.token)

onBeforeMount(() => {
  authStore.getSession().catch(() => {})
})
```
（`onBeforeMount` 需从 `vue` import；`authStore.session?.auth` 依赖 auth store 的 `getSession()` 填充 session。）
4) 模板取消注释：`<!-- <Permission :visible="needPermission" /> -->` → `<Permission :visible="needPermission" />`

> 说明：`needPermission` 用「auth 开启 && 无 token」→ 显示登录弹窗。登录成功后 token 写入 → reload → needPermission 变 false。

- [ ] **Step 2: Permission.vue 去绕过 + token 写 store**

`ai-chat-web/src/views/chat/layout/Permission.vue`：
1) 删除第 16 行 `authStore.setToken('helloxx')`，删除 17-19 行被注释的 getSession（保留无关代码）
2) `handleLogin` 内把 `localStorage.access_token = data.access_token` 换成 store 写入并同步 token ref：
```ts
    const data = await login(phone.value.trim(), code.value.trim())
    authStore.setToken(data.access_token)
    token.value = data.access_token
    ms.success('登录成功')
    window.location.reload()
```
（删除 `// authStore.setToken(data.token)` 注释行。）
3) catch 分支里 `localStorage.access_token = ''` 改为 `authStore.removeToken()`：
```ts
  catch (error: any) {
    ms.error(error.message ?? 'error')
    authStore.removeToken()
    token.value = ''
  }
```

- [ ] **Step 3: axios.ts 读 SECRET_TOKEN（与 auth store 一致）**

`ai-chat-web/src/utils/request/axios.ts` 请求拦截器（第 10-12 行）改为：
```ts
    const access_token = ss.get('SECRET_TOKEN')
    if (access_token)
      config.headers.Authorization = access_token
```
（不再读 cookie `sso_0voice_access_token`；`ss` 是 `@/utils/storage` 的 localStorage 封装，与 auth store 的 `getToken()`（`SECRET_TOKEN`）一致。文件顶部 `import { ss } from '@/utils/storage'`。响应拦截器里 401 分支的 `deleteCookieByKey('sso_0voice_access_token')` + 跳 `VITE_USER_CENTER` 改为 `ss.remove('SECRET_TOKEN')` + `window.location.reload()`。）

- [ ] **Step 4: index.ts 中性化 401 跳转**

`ai-chat-web/src/utils/request/index.ts` 的 `failHandler` 里，把跳转 0voice 改成清除本地 token + 刷新回登录页：
```ts
  const failHandler = (error: Response<Error>) => {
    if (error?.response?.status === 401) {
      ss.remove('SECRET_TOKEN')
      window.location.reload()
    }
    afterRequest?.()
    throw new Error(error?.message || 'Error')
  }
```
（`ss` 从 `@/utils/storage` import；保留原有 `deleteCookieByKey` 调用与否不影响——本改动只移除 `window.location.href = 'https://user.0voice.com...'` 外部跳转。`successHandler` 里 `status === 'Unauthorized'` 分支的 `authStore.removeToken()` 已正确，保留。）

- [ ] **Step 5: 重建前端 + 验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/ai-chat-web
pnpm build-only 2>&1 | tail -5
cp -r dist/. ../ai-chat-backend/www/
```

Expected: 构建成功。`dist/index.html` 与 `ai-chat-backend/www/index.html` 存在。

- [ ] **Step 6: 端到端登录流程验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
# backend 已在跑（Task 3 重启过）；确认 7080 监听
ss -tln | grep 7080 || (fuser -k -TERM 7080/tcp 2>/dev/null; sleep 2; cd ai-chat-backend && go build -o ../bin/ai-chat-backend ./cmd/ && cd .. && ./bin/ai-chat-backend --config=ai-chat-backend/dev.config.yaml > /tmp/backend-auth-test.log 2>&1 &)
sleep 3

echo "=== 未登录时 /session（auth_enabled=true → auth:true）==="
curl -s -X POST http://localhost:7080/api/session
echo
echo "=== 页面可访问 ==="
curl -s -o /dev/null -w "首页: %{http_code}\n" http://localhost:7080/
```

Expected: `/session` 返回 `auth:true`（前端据此弹登录框）；首页 200。**浏览器验证留待用户**：打开 http://localhost:7080 应出现登录框，输手机号→拿验证码（看 backend 日志）→登录→进入聊天页；聊天正常且额度扣减。

- [ ] **Step 7: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add ai-chat-web/src/views/chat/layout/Layout.vue ai-chat-web/src/views/chat/layout/Permission.vue ai-chat-web/src/utils/request/axios.ts ai-chat-web/src/utils/request/index.ts
git commit -m "feat: ai-chat 前端解锁登录门 + token 流统一 + 401 本地化"
```

> 注意：`ai-chat-backend/www/` 重建产物是**用户工作区的未提交改动**（已有前端构建产物在 git 里被跟踪为 M 状态）。本任务只提交 `ai-chat-web/src` 的源码改动，**不提交 www/ 产物**（保持与仓库现状一致，由用户自行决定是否提交构建产物）。

---

## 自审结论

- **Spec 覆盖**：kvstore 验证码/session + MySQL users 表→Task1；发验证码/登录/session 接口→Task2；鉴权中间件 + 计费(quota/402) + tokenizer 配置化→Task3；前端解锁/去绕过/401 本地化→Task4；`auth_enabled` 开关→Task3 中间件 + Task2 Session。
- **无占位符**：所有新文件给出完整 Go/TS 代码；现有文件给出精确修改点。
- **类型/命名一致**：`sms_code:`/`session:` key 前缀、`access_token` 顶层返回、`data:{auth,model}` 包装、`Auth.Enabled/InitQuota`、`Tokenizer.Address`、`users.GetByPhone/UpsertByPhone/DeductQuota` 在 Task1-3 全局一致；手机号正则 `^1\d{10}$` 与前端一致。
- **依赖版本对齐**：go-redis v9.6.1、go-sql-driver v1.8.1（与 ai-chat-service 一致）。
- **风险标注**：Task2/3 会重启 backend（7080）但不影响 8 个服务与 kvstore；前端 www/ 构建产物不提交（用户工作区既有状态）。
