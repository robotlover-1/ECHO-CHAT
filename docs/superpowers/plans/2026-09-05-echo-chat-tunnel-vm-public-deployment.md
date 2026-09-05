# ECHO-CHAT 公网 tunnel 部署 —— 仓库落地 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ECHO-CHAT 仓库（main，工作树 `tmp/t1/ECHO-CHAT`）落地公网 tunnel 部署的可静态交付制品：`deploy/{app,edge,scripts}`、`docker/config` 模板化、`docker/compose.yaml` 端口收紧、ai-chat-backend 最小安全改动（可信代理 + 真就绪检查），使主方案可直接在真实 VM 上执行。

**Architecture:** 双节点拓扑 —— 应用节点跑 ECHO-CHAT 主 compose + 独立 frpc 栈（host 网络连 `127.0.0.1:7080`）；公网入口节点跑 frps + Nginx（host 网络，TLS 由 edge 脚本两阶段引导）。密钥全部走 `.env` + 受限 envsubst 部署时渲染；git 只保留无真实凭据模板。backend 增加 gin 可信代理装配与 `/api/readyz`。

**Tech Stack:** docker compose v2、FRP 0.62.1（frps/frpc）、nginx、certbot、Go(gin) 、GNU envsubst、bash。

**Spec:** `docs/superpowers/specs/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`（rev2）。主方案：`docs/deploy/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`。

## Global Constraints

- **提交纪律**：工作树中 `openai-api-proxy/dev.config.yaml` 处于 `M`（本地真实 DeepSeek key），**任何 `git add` 不得包含它**；提交一律用精确路径 `git add <本任务列出的路径>`。根目录另有未跟踪 `kvstore/`，忽略不动。提交信息用仓库既有风格（`feat|fix|docs|refactor(scope): ...`，中文）。
- **本机无 docker daemon/compose 插件**：不得运行 `docker compose up/config`。Compose/FRP/Nginx 校验只能用：YAML 语法（python3+pyyaml）、`bash -n`、受限 envsubst 渲染冒烟、本机 `nginx -t`（nginx 1.18，仅语法，配临时 wrapper + 自签证书）。真实端到端留目标机。
- **渲染机制（P0-1）**：模板只用纯 `${VAR}`，禁止 `${VAR:-def}`；默认值在 `.env.example`；`REQUIRED` 变量缺失或等于占位（`CHANGE_ME*`/`REPLACE_ME*`/`<...>`/`sk-placeholder*`）→ 渲染脚本非零退出；渲染产物不得残留 `$\{`。
- **envsubst 受限白名单**：只替换显式列出的变量；nginx 模板绝不能让 `$host`/`$remote_addr`/`$proxy_add_x_forwarded_for` 等被吞。产物一律经 `umask 077` + 原子写；`.env`/产物权限 `600`。
- **XFF（P0-3）**：edge Nginx 覆盖式 `proxy_set_header X-Forwarded-For $remote_addr;`，不使用 `$proxy_add_x_forwarded_for`。
- **就绪（P0-4）**：`/api/readyz` 检查 mysql/redis(kvstore)/tokenizer/ai-chat-service(zrpc)；`deploy-app.sh` 等待 `/api/readyz`（200 才算就绪）。
- 后端编译/测试命令：在 `ai-chat-backend/` 下执行（Go 1.20，模块缓存齐，`go build ./...` 与 `go test` 均已验证可离线跑通）。
- 文档路径固定：`docs/superpowers/specs/…design.md`（rev2）、`docs/superpowers/plans/…md`（本文件）、`docs/deploy/…design.md`（主方案，只读参考，不修改）。

---

### Task 1: backend 可信代理装配 + 配置脱敏打印

**Files:**
- Modify: `ai-chat-backend/pkg/config/config.go`
- Create: `ai-chat-backend/pkg/middlewares/engine.go`
- Create: `ai-chat-backend/pkg/middlewares/engine_test.go`
- Modify: `ai-chat-backend/cmd/main.go`

**Interfaces:**
- Consumes: 现有 `middlewares.Cors()`（`pkg/middlewares/cors.go`）。
- Produces: `middlewares.NewEngine(configured []string) (*gin.Engine, error)` —— configured 为空时内部回退默认 `["127.0.0.1","::1"]`，非法输入返回 error。config.Http 新增字段 `TrustedProxies []string`（yaml `trusted_proxies`）。main 用它替换 `gin.Default()`。

- [ ] **Step 1: 先写失败测试**

创建 `ai-chat-backend/pkg/middlewares/engine_test.go`：

```go
package middlewares

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/gin-gonic/gin"
)

// 每注册一个 /ip{n} 路由（同一 engine 多次注册相同 path 会 panic）。
var ipRouteID struct {
	sync.Mutex
	n int
}

// clientIP 请求 "X-Forwarded-For" 场景下 gin.ClientIP 的解析结果。
func ipOf(t *testing.T, engine *gin.Engine, remoteAddr string, xff string) string {
	t.Helper()
	gin.SetMode(gin.TestMode)
	ipRouteID.Lock()
	ipRouteID.n++
	path := fmt.Sprintf("/ip%d", ipRouteID.n)
	ipRouteID.Unlock()
	engine.GET(path, func(c *gin.Context) {
		c.JSON(200, gin.H{"ip": c.ClientIP()})
	})
	req := httptest.NewRequest(http.MethodGet, path, nil)
	if remoteAddr != "" {
		req.RemoteAddr = remoteAddr
	}
	if xff != "" {
		req.Header.Set("X-Forwarded-For", xff)
	}
	w := httptest.NewRecorder()
	engine.ServeHTTP(w, req)
	var body struct {
		IP string `json:"ip"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal body %q: %v", w.Body.String(), err)
	}
	return body.IP
}

func TestNewEngineDefaultsToLoopbackTrust(t *testing.T) {
	engine, err := NewEngine(nil) // 默认信任 127.0.0.1/::1
	if err != nil {
		t.Fatalf("NewEngine(nil) err = %v", err)
	}
	// 可信回环一跳 + XFF → 采用 XFF（真实客户端，由 Nginx 覆盖写入）
	if got := ipOf(t, engine, "127.0.0.1:5555", "203.0.113.7"); got != "203.0.113.7" {
		t.Fatalf("loopback trusted: got %s, want 203.0.113.7", got)
	}
	// 非可信对端 + 伪造 XFF → 忽略 XFF，采用对端地址
	if got := ipOf(t, engine, "10.9.9.9:9999", "6.6.6.6"); got != "10.9.9.9" {
		t.Fatalf("untrusted peer spoof: got %s, want 10.9.9.9", got)
	}
	// 多级代理（右起第一个非可信地址为真实客户端）
	if got := ipOf(t, engine, "127.0.0.1:5555", "203.0.113.7, 10.1.1.1"); got != "10.1.1.1" {
		t.Fatalf("multi proxy: got %s, want 10.1.1.1", got)
	}
	// IPv6 回环可信
	if got := ipOf(t, engine, "[::1]:5555", "2001:db8::1"); got != "2001:db8::1" {
		t.Fatalf("ipv6 loopback: got %s, want 2001:db8::1", got)
	}
}

func TestNewEngineExplicitTrust(t *testing.T) {
	engine, err := NewEngine([]string{"10.0.0.0/8"})
	if err != nil {
		t.Fatalf("NewEngine err = %v", err)
	}
	// 10.x 对端可信 → 采用 XFF
	if got := ipOf(t, engine, "10.1.2.3:1234", "198.51.100.1"); got != "198.51.100.1" {
		t.Fatalf("explicit trust: got %s, want 198.51.100.1", got)
	}
}

func TestNewEngineRejectsInvalid(t *testing.T) {
	if _, err := NewEngine([]string{"not-an-ip"}); err == nil {
		t.Fatal("NewEngine invalid proxy should error")
	}
}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ai-chat-backend && go test ./pkg/middlewares/ -run 'TestNewEngine' -v`
Expected: FAIL（`NewEngine` undefined，编译失败）。这是预期的红。

- [ ] **Step 3: 实现 `NewEngine`**

创建 `ai-chat-backend/pkg/middlewares/engine.go`：

```go
package middlewares

import (
	"github.com/gin-gonic/gin"
)

// defaultTrustedProxies：公网部署后端只被同机 frpc(host 网络)访问，回环一跳可信；
// XFF 由 edge Nginx 覆盖式写入（见 deploy/edge/nginx/echo-chat.conf.envsubst）。
var defaultTrustedProxies = []string{"127.0.0.1", "::1"}

// NewEngine 构造 gin 引擎并显式设置可信代理。configured 为空 → 回退默认回环；
// 配置非法 → 返回 error（调用方 fatal，不静默信任全部）。
func NewEngine(configured []string) (*gin.Engine, error) {
	trusted := configured
	if len(trusted) == 0 {
		trusted = defaultTrustedProxies
	}
	engine := gin.New()
	engine.Use(gin.Logger(), gin.Recovery(), Cors())
	if err := engine.SetTrustedProxies(trusted); err != nil {
		return nil, err
	}
	return engine, nil
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd ai-chat-backend && go test ./pkg/middlewares/ -v`
Expected: 3 个 `TestNewEngine*` PASS（同时全包无回归）。

- [ ] **Step 5: config 增加 `TrustedProxies`**

修改 `ai-chat-backend/pkg/config/config.go`，`Http` 结构体内 `Port int` 之后加一行：

```go
		TrustedProxies []string `mapstructure:"trusted_proxies"`
```

（含注释 `// 可信代理；空=回环默认，见 middlewares.NewEngine`。）

- [ ] **Step 6: main.go 换引擎 + 脱敏启动日志**

修改 `ai-chat-backend/cmd/main.go`：

1. 第 45-46 行 `entry := gin.Default(); entry.Use(middlewares.Cors())` 替换为：

```go
	engine, err := middlewares.NewEngine(cnf.Http.TrustedProxies)
	if err != nil {
		r.log.Fatal(err)
	}
	entry := engine
```

2. 删除第 95 行 `fmt.Printf("%+v\n", cnf)`，替换为脱敏摘要（不可含 token/password/dsn/key）。用 pkg/log 的**包级** `log.InfoF`（该 import 别名 `log` 指向 `ai-chat-backend/pkg/log`，无 Printf）：

```go
	log.InfoF("config: http=%s:%d model=%s auth=%v log.level=%s",
		cnf.Http.IP, cnf.Http.Port, cnf.Chat.Model, cnf.Auth.Enabled, cnf.Log.Level)
```

确认 `fmt` 仍被使用（`fmt.Sprintf("%s:%d", ...)` 在 `httpServer` 中存在），保留 import。

- [ ] **Step 7: 编译 + 全量测试**

Run: `cd ai-chat-backend && go build ./... && go test ./...`
Expected: build 0 错误；所有包 PASS（含新的 3 个 trusted-proxy 测试）。

- [ ] **Step 8: Commit**

```bash
git add ai-chat-backend/pkg/config/config.go ai-chat-backend/pkg/middlewares/engine.go ai-chat-backend/pkg/middlewares/engine_test.go ai-chat-backend/cmd/main.go
git commit -m "feat(backend): gin 可信代理显式装配(默认回环) + 启动日志脱敏"
```

---

### Task 2: backend 真就绪检查 `/api/readyz`

**Files:**
- Create: `ai-chat-backend/services/ai-chat-service/ping.go`
- Create: `ai-chat-backend/pkg/controllers/readyz.go`
- Create: `ai-chat-backend/pkg/controllers/readyz_test.go`
- Modify: `ai-chat-backend/cmd/main.go`

**Interfaces:**
- Consumes: `services/ai-chat-service/chat_stream.go` 内同包函数 `hostOf(string) string`、`portOf(string) int`（已存在）；zrpc `echo-zrpc-go` 的 `NewClient(opts)` / `Client.Ping(ctx)` / `Client.Close()`。
- Produces: `ai_chat_service.Ping(ctx context.Context) error`（用 config 的 `DependOn.AiChatService.{Address,AccessToken}` 建立 zrpc 连接并 Ping）；`controllers.ReadyzWith(checkers map[string]func(context.Context) error) gin.HandlerFunc`（可注入，供单测）与 `controllers.ReadyzHandler() gin.HandlerFunc`（生产实现，内部用 `log.NewLogger()` 打探针错误，无参数）。

- [ ] **Step 1: 先写失败测试（readyz 核心逻辑，注入 fake checker）**

创建 `ai-chat-backend/pkg/controllers/readyz_test.go`：

```go
package controllers

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestReadyzAllOK(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	engine.GET("/api/readyz", ReadyzWith(map[string]func(context.Context) error{
		"mysql":         func(context.Context) error { return nil },
		"kvstore":       func(context.Context) error { return nil },
		"tokenizer":     func(context.Context) error { return nil },
		"ai-chat-service": func(context.Context) error { return nil },
	}))
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/readyz", nil)
	engine.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("code = %d, want 200; body=%s", w.Code, w.Body.String())
	}
}

func TestReadyzFailsWhenDepDown(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	engine.GET("/api/readyz", ReadyzWith(map[string]func(context.Context) error{
		"mysql": func(context.Context) error { return nil },
		"kvstore": func(context.Context) error { return errors.New("down") },
	}))
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/readyz", nil)
	engine.ServeHTTP(w, req)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("code = %d, want 503; body=%s", w.Code, w.Body.String())
	}
	if got := w.Body.String(); !strings.Contains(got, "kvstore") {
		t.Fatalf("body should mention failing dep; got %s", got)
	}
	// 不得泄露内部地址/凭据
	for _, banned := range []string{"token", "dsn", "tcp(", "50055"} {
		if strings.Contains(w.Body.String(), banned) {
			t.Fatalf("body leaks %q: %s", banned, w.Body.String())
		}
	}
}

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ai-chat-backend && go test ./pkg/controllers/ -run TestReadyz -v`
Expected: FAIL（`ReadyzWith` undefined）。

- [ ] **Step 3: 实现 zrpc Ping helper**

创建 `ai-chat-backend/services/ai-chat-service/ping.go`：

```go
package ai_chat_service

import (
	"context"

	"ai-chat-backend/pkg/config"
	zrpc "echo-zrpc-go"
)

// Ping 探测 ai-chat-service(zrpc) 连通性，供 /api/readyz 使用。
// 复用 OpenChatStream 同款地址解析（config.DependOn.AiChatService.Address）。
func Ping(ctx context.Context) error {
	cnf := config.GetConfig()
	dep := cnf.DependOn.AiChatService
	cli, err := zrpc.NewClient(clientOptionsFromAddress(dep.Address, dep.AccessToken))
	if err != nil {
		return err
	}
	defer cli.Close()
	return cli.Ping(ctx)
}
```

- [ ] **Step 4: 实现 readyz 控制器**

创建 `ai-chat-backend/pkg/controllers/readyz.go`：

```go
package controllers

import (
	"context"
	"net/http"
	"sort"
	"sync"
	"time"

	"ai-chat-backend/pkg/log"
	"ai-chat-backend/pkg/config"
	mysqlpkg "ai-chat-backend/pkg/db/mysql"
	redisclient "ai-chat-backend/pkg/db/redis"
	ai_chat_service "ai-chat-backend/services/ai-chat-service"

	"github.com/gin-gonic/gin"
)

// depCheck 每次只输出 ok/degraded/fail 三态，细粒度错误只写日志，不暴露地址/凭据。
type depCheck struct {
	status string
}

func depProbe(name string, fn func(context.Context) error, logger log.ILogger) string {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := fn(ctx); err != nil {
		logger.ErrorF("readyz %s: %v", name, err)
		return "fail"
	}
	return "ok"
}

func defaultReadyzCheckers() map[string]func(context.Context) error {
	return map[string]func(context.Context) error{
		"mysql": func(ctx context.Context) error {
			return mysqlpkg.GetDB().PingContext(ctx)
		},
		"kvstore": func(ctx context.Context) error {
			return redisclient.GetPool().Ping(ctx).Err()
		},
		"tokenizer": func(ctx context.Context) error {
			return tokenizerReachable(ctx)
		},
		"ai-chat-service": ai_chat_service.Ping,
	}
}

// ReadyzWith 可注入 checker 的通用 handler（便于单测）。
func ReadyzWith(checkers map[string]func(context.Context) error) gin.HandlerFunc {
	logger := log.NewLogger()
	return func(c *gin.Context) {
		results := make(map[string]string, len(checkers))
		var mu sync.Mutex
		var wg sync.WaitGroup
		for name, fn := range checkers {
			wg.Add(1)
			go func(name string, fn func(context.Context) error) {
				defer wg.Done()
				st := depProbe(name, fn, logger)
				mu.Lock()
				results[name] = st
				mu.Unlock()
			}(name, fn)
		}
		wg.Wait()

		names := make([]string, 0, len(results))
		allOK := true
		for n, st := range results {
			names = append(names, n)
			if st != "ok" {
				allOK = false
			}
		}
		sort.Strings(names)

		data := make([]gin.H, 0, len(names))
		for _, n := range names {
			data = append(data, gin.H{"name": n, "status": results[n]})
		}
		if !allOK {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "Fail", "data": data})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "Success", "data": data})
	}
}

// ReadyzHandler 生产实现：全依赖并行探测。
func ReadyzHandler() gin.HandlerFunc {
	return ReadyzWith(defaultReadyzCheckers())
}

// tokenizerReachable：tokenizer 无 /health 路由，做 HTTP 可达性探测即可。
func tokenizerReachable(ctx context.Context) error {
	addr := config.GetConfig().Tokenizer.Address
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, addr, nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}
```

> 注意：`config.GetConfig()` 返回 nil 且 `GetDB()`/`GetPool()` 依赖已 `InitConfig` 的全局配置；main 在 build routes 前已调用 `config.InitConfig`。`ReadyzWith` 单测只注入 fake，不触碰全局。

- [ ] **Step 5: 接线 main.go**

修改 `ai-chat-backend/cmd/main.go`，在既有 `/health` 路由之后加一行（`chat` group 内）：

```go
	chat.GET("/readyz", controllers.ReadyzHandler())
```

/health 保持存活语义（空 200）。

- [ ] **Step 6: 编译 + 测试**

Run: `cd ai-chat-backend && go build ./... && go test ./...`
Expected: build 通过；`TestReadyzAllOK`、`TestReadyzFailsWhenDepDown` PASS。

- [ ] **Step 7: Commit**

```bash
git add ai-chat-backend/services/ai-chat-service/ping.go ai-chat-backend/pkg/controllers/readyz.go ai-chat-backend/pkg/controllers/readyz_test.go ai-chat-backend/cmd/main.go
git commit -m "feat(backend): /api/readyz 真就绪检查(mysql/kvstore/tokenizer/zrpc) + zrpc Ping helper"
```

---

### Task 3: `docker/config` 模板化 + 渲染脚本（P0-1/P1-7）

**Files:**
- Create: `docker/config/backend.yaml.envsubst`
- Create: `docker/config/service.yaml.envsubst`
- Create: `deploy/app/.env.example`
- Create: `deploy/edge/.env.example`
- Create: `deploy/scripts/lib.sh`
- Create: `deploy/scripts/render-config.sh`
- Modify: `.gitignore`
- Run (git rm): `docker/config/backend.yaml`、`docker/config/service.yaml`

**Interfaces:**
- Produces: `deploy/scripts/lib.sh` 导出函数：`die`, `require_env <file>`, `render_restricted <template> <out> <var1 var2 ...>`, `guard_no_residue <file>`, `guard_required <envfile> <var...>`, `preflight_host <min_mem_mb> <min_disk_mb>`；`render-config.sh {app|edge}`。后续 deploy 脚本 source `lib.sh` 并调用这些函数。

> 模板 = 「把当前已跟踪的 `docker/config/*.yaml`（Task 3 开始仍在工作树）复制为 `.envsubst`，再把敏感叶子替换为纯 `${VAR}`」——**不要**照记忆重敲整份 yaml。

- [ ] **Step 1: 生成 backend 模板并做替换**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
cp docker/config/backend.yaml docker/config/backend.yaml.envsubst
```

用 Read/Edit 把 `docker/config/backend.yaml.envsubst` 中下列行替换为纯 `${VAR}`（值取自 `.env`，无 `:-` 默认）：

| 原行 | 替换为 |
| --- | --- |
| `address: "service:50055"` | 保留不变（内部固定） |
| `accessToken: "me256487ang1chubdpdialoud22sev1ozhoguumyqca"` | `accessToken: "${CHAT_SERVICE_TOKEN}"` |
| `host: "kvstore"` | `host: "${KVSTORE_HOST}"` |
| `port: 5160` | `port: "${KVSTORE_PORT}"` |
| `dsn: "root:123456@tcp(host.docker.internal:3306)/ai_chat?charset=utf8mb4"` | `dsn: "${MYSQL_DSN}"` |

文件顶部加注释块：

```yaml
# 部署渲染模板（勿直接使用）：docker compose 前先跑 deploy/scripts/render-config.sh app。
# 变量由 deploy/app/.env 提供（.env.example 含本地开发默认，生产必须覆盖）。
```

- [ ] **Step 2: 生成 service 模板并做替换**

```bash
cp docker/config/service.yaml docker/config/service.yaml.envsubst
```

`docker/config/service.yaml.envsubst` 需替换（余下内容原样保留）：

| 原行 | 替换为 |
| --- | --- |
| `  accessToken: "me256487ang1chubdpdialoud22sev1ozhoguumyqca"`（server 段） | `  accessToken: "${CHAT_SERVICE_TOKEN}"` |
| `  api_key: "i0jey84SdkFdw5u43780yjr3h7se8nth0yi295nr94ksDngKprEh"` | `  api_key: "${PROXY_API_KEY}"`（本地默认见 .env.example，勿改值语义——它是 service→proxy 共享的内网 key） |
| 两处 `accessToken: "ang1chubdev1ozhome256487d22sapguuv1ozhom"`（dependOn.sensitive/keywords） | `accessToken: "${FILTER_SERVICE_TOKEN}"` |
| redis `host: "kvstore"` / `port: 5160` | `host: "${KVSTORE_HOST}"` / `port: "${KVSTORE_PORT}"` |
| `dsn: "root:123456@tcp(host.docker.internal:3306)/ai_chat?collation=utf8mb4_unicode_ci&charset=utf8mb4"` | `dsn: "${MYSQL_DSN}"` |
| vectorDB 整段：`url:` / `username:` / `pwd:` / `database:` / 其余保留 | `url: "${VECTOR_DB_URL}"` / `username: "${VECTOR_DB_USER}"` / `pwd: "${VECTOR_DB_PWD}"` / `database: "${VECTOR_DB_NAME}"` |

`api_key`（backend 呈现给 proxy 的 key）替换为 `${CHAT_SERVICE_TOKEN}` 的理由：它只是 backend→service 的既有权标复用，非外部凭据；如需独立可后续加变量。

- [ ] **Step 3: 写 `.env.example`（app 与 edge）**

创建 `deploy/app/.env.example`：

```dotenv
# ECHO-CHAT 应用节点 .env 示例。cp 为 .env 后按需修改；生产必须覆盖标注「生产必改」项。
# 密钥只存 .env(600)；不要提交 .env。FRP_AUTH_TOKEN 两端必须一致。

# ---- common ----
PUBLIC_DOMAIN=chat.example.com
FRP_AUTH_TOKEN=CHANGE_ME_random_hex_64

# ---- app: frpc / compose ----
FRP_SERVER_ADDR=203.0.113.10
FRP_BIND_PORT=39000
FRPC_IMAGE=snowdreamtech/frpc:0.62.1
DEEPSEEK_API_KEY=CHANGE_ME_sk-xxx

# ---- app: 内部 token（本地开发默认与仓库 dev 一致；生产必改）----
CHAT_SERVICE_TOKEN=me256487ang1chubdpdialoud22sev1ozhoguumyqca
FILTER_SERVICE_TOKEN=ang1chubdev1ozhome256487d22sapguuv1ozhom
PROXY_API_KEY=i0jey84SdkFdw5u43780yjr3h7se8nth0yi295nr94ksDngKprEh

# ---- app: kvstore/mysql（默认与 docker/config 原 service.yaml dsn 对齐；backend 原先无 collation，统一用此值）----
KVSTORE_HOST=kvstore
KVSTORE_PORT=5160
MYSQL_DSN=root:123456@tcp(host.docker.internal:3306)/ai_chat?collation=utf8mb4_unicode_ci&charset=utf8mb4

# ---- app: 向量库（生产必填；无 committed 默认）----
VECTOR_DB_URL=CHANGE_ME_vector_db_url
VECTOR_DB_USER=CHANGE_ME_vector_db_user
VECTOR_DB_PWD=CHANGE_ME_vector_db_pwd
VECTOR_DB_NAME=ai-chat
```

创建 `deploy/edge/.env.example`：

```dotenv
# 公网入口节点 .env 示例。cp 为 .env；不要提交 .env。FRP_AUTH_TOKEN 与应用节点一致。

PUBLIC_DOMAIN=chat.example.com
ADMIN_EMAIL=CHANGE_ME_admin@example.com
FRP_AUTH_TOKEN=CHANGE_ME_random_hex_64
FRP_BIND_PORT=39000
FRP_VHOST_HTTP_PORT=39001
FRP_DASHBOARD_PASSWORD=CHANGE_ME_dashboard_pwd
FRPS_IMAGE=snowdreamtech/frps:0.62.1
```

- [ ] **Step 4: 写 lib.sh**

创建 `deploy/scripts/lib.sh`：

```bash
#!/usr/bin/env bash
# ECHO-CHAT deploy 共享函数。source 本文件后使用。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

die() { echo "[deploy] ERROR: $*" >&2; exit 1; }
info() { echo "[deploy] $*"; }

# 加载 .env：若缺失则提示 cp .env.example。不做 set -x、不回显内容。
require_env() {
  local envfile="$1" example="${2:-}"
  [[ -f "${envfile}" ]] || {
    [[ -n "${example}" ]] \
      && die "缺少 ${envfile}。先执行: cp ${example} ${envfile} 并填写。" \
      || die "缺少 ${envfile}。"
  }
  # shellcheck disable=SC1090
  set -a; source "${envfile}"; set +a
}

# 受限 envsubst：只替换显式白名单变量，防误吞 nginx $host/$remote_addr 等。
render_restricted() {
  local template="$1" out="$2" varlist="$3"
  umask 077
  local tmp
  tmp="$(mktemp "${out}.XXXXXX")"
  trap 'rm -f "${tmp}"' RETURN
  # shellcheck disable=SC2086
  envsubst "${varlist}" < "${template}" > "${tmp}"
  guard_no_residue "${tmp}"
  mv "${tmp}" "${out}"
  chmod 600 "${out}"
  info "rendered: ${out}"
}

# 渲染产物不得残留未替换变量。
guard_no_residue() {
  if grep -qE '\$\{?[A-Za-z_][A-Za-z0-9_]*' "$1"; then
    die "残留未替换变量: $(grep -oE '\$\{?[A-Za-z_][A-Za-z0-9_]*' "$1" | sort -u | tr '\n' ' ')"
  fi
}

# 必填项守卫：为空或以 CHANGE_ME/REPLACE_ME/<...>/sk-placeholder 开头 → 退出。
guard_required() {
  local envfile="$1"; shift
  require_env "${envfile}"
  local v
  for v in "$@"; do
    local val="${!v:-}"
    if [[ -z "${val}" ]] || [[ "${val}" == CHANGE_ME* ]] \
       || [[ "${val}" == REPLACE_ME* ]] || [[ "${val}" == '<'*'>' ]] \
       || [[ "${val}" == sk-placeholder* ]]; then
      die "必填变量 ${v} 未设置或仍是占位(见 ${envfile}.example)"
    fi
  done
}

# 基础预检：x86_64 + docker + 资源阈值（内存 MB、磁盘 MB）。
preflight_host() {
  local min_mem_mb="$1" min_disk_mb="$2"
  [[ "$(uname -m)" == x86_64 ]] || die "仅支持 x86_64 (当前 $(uname -m))"
  command -v docker >/dev/null || die "缺少 docker CLI"
  docker version >/dev/null 2>&1 || die "docker daemon 不可用"
  command -v envsubst >/dev/null || die "缺少 envsubst (gettext-base)"
  local mem_kb disk_kb
  mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)
  [[ $(( mem_kb / 1024 )) -ge "${min_mem_mb}" ]] \
    || die "内存不足: ${min_mem_mb}MB 需要, 实际 $(( mem_kb / 1024 ))MB"
  disk_kb=$(df -Pk . | awk 'NR==2{print $4}')
  [[ $(( disk_kb / 1024 )) -ge "${min_disk_mb}" ]] \
    || die "磁盘不足: ${min_disk_mb}MB 需要, 实际 $(( disk_kb / 1024 ))MB"
}

# 端口未占用检查（可选调用）。
assert_port_free() {
  local port="$1"
  if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -qE "[:.]${port}\b"; then
    die "端口 ${port} 已被占用"
  fi
}
```

- [ ] **Step 5: 写 render-config.sh**

创建 `deploy/scripts/render-config.sh`：

```bash
#!/usr/bin/env bash
# 部署前渲染：把 .env 注入模板，产出 gitignore 的最终配置。
# 用法: render-config.sh app | edge
#   app  : docker/config/{backend,service}.yaml + deploy/app/tunnel/frpc.yaml
#   edge : deploy/edge/tunnel/frps.yaml + deploy/edge/nginx/echo-chat.conf
# 说明: nginx 完整/引导 conf 由 deploy-edge.sh 按证书阶段决定渲染哪个模板到 echo-chat.conf。
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

case "${1:-}" in
  app)
    APP_ENV="${REPO_ROOT}/deploy/app/.env"
    guard_required "${APP_ENV}" \
      PUBLIC_DOMAIN FRP_AUTH_TOKEN FRP_SERVER_ADDR \
      VECTOR_DB_URL VECTOR_DB_USER VECTOR_DB_PWD
    render_restricted "${REPO_ROOT}/docker/config/backend.yaml.envsubst" \
      "${REPO_ROOT}/docker/config/backend.yaml" \
      '${CHAT_SERVICE_TOKEN} ${KVSTORE_HOST} ${KVSTORE_PORT} ${MYSQL_DSN}'
    render_restricted "${REPO_ROOT}/docker/config/service.yaml.envsubst" \
      "${REPO_ROOT}/docker/config/service.yaml" \
      '${CHAT_SERVICE_TOKEN} ${FILTER_SERVICE_TOKEN} ${PROXY_API_KEY} ${KVSTORE_HOST} ${KVSTORE_PORT} ${MYSQL_DSN} ${VECTOR_DB_URL} ${VECTOR_DB_USER} ${VECTOR_DB_PWD} ${VECTOR_DB_NAME}'
    ;;
  edge)
    EDGE_ENV="${REPO_ROOT}/deploy/edge/.env"
    guard_required "${EDGE_ENV}" \
      PUBLIC_DOMAIN ADMIN_EMAIL FRP_AUTH_TOKEN FRP_DASHBOARD_PASSWORD
    render_restricted "${REPO_ROOT}/deploy/edge/tunnel/frps.yaml.envsubst" \
      "${REPO_ROOT}/deploy/edge/tunnel/frps.yaml" \
      '${FRP_BIND_PORT} ${FRP_VHOST_HTTP_PORT} ${FRP_AUTH_TOKEN} ${FRP_DASHBOARD_PASSWORD}'
    # echo-chat.conf 由 deploy-edge.sh 决定写入哪个模板，此处不渲染。
    ;;
  *) die "用法: render-config.sh app|edge" ;;
esac
```

> 注意顺序：先建 Task 5/6 的 `frpc.yaml.envsubst`/`frps.yaml.envsubst` 后，再回来把这两条渲染调用补进本文件（见 Task 6 末尾 Step）。为避免中间态提交缺文件，Task 3 的 app 渲染可先只渲染 docker/config 两条（如上），frp/nginx 渲染在对应 Task 补入。

- [ ] **Step 6: gitignore + 移除跟踪**

`.gitignore` 末尾追加：

```gitignore
# ============================================
# ECHO-CHAT 公网部署：渲染产物与密钥（勿提交）
# ============================================
docker/config/backend.yaml
docker/config/service.yaml
deploy/app/.env
deploy/edge/.env
deploy/app/tunnel/frpc.yaml
deploy/edge/tunnel/frps.yaml
deploy/edge/nginx/echo-chat.conf
```

```bash
git rm --cached docker/config/backend.yaml docker/config/service.yaml
```

（文件仍留在工作树，作为后续 render 输出的对照——先不删除磁盘文件，等 Task 3 渲染验证后再删。）

- [ ] **Step 7: 渲染冒烟（app）**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
cp deploy/app/.env.example deploy/app/.env
# 将必填占位填上假值以便测试：
sed -i 's|CHANGE_ME_random_hex_64|1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff|; s|CHANGE_ME_sk-xxx|sk-dummy-for-render-test|; s|CHANGE_ME_vector_db_url|http://vdbtest:60000|; s|CHANGE_ME_vector_db_user|u|; s|CHANGE_ME_vector_db_pwd|p|' deploy/app/.env
bash deploy/scripts/render-config.sh app
```

Expected:
- 打印 `rendered: .../docker/config/backend.yaml`、`rendered: .../docker/config/service.yaml`；
- `grep -c '\$\{' docker/config/backend.yaml docker/config/service.yaml` 输出 `0`；
- `python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ['docker/config/backend.yaml','docker/config/service.yaml']]; print('yaml OK')"` → `yaml OK`；
- 产物 `stat -c '%a'` 为 600。

- [ ] **Step 8: 守卫自测**

```bash
echo 'PUBLIC_DOMAIN=chat.example.com' > /tmp/bad.env
bash -c 'source deploy/scripts/lib.sh && guard_required /tmp/bad.env PUBLIC_DOMAIN FRP_AUTH_TOKEN' ; echo "exit=$?"
```

Expected: 打印 `必填变量 FRP_AUTH_TOKEN ...` 且 `exit=1`（非零）。

- [ ] **Step 9: 清理测试 .env 与产物**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
rm -f deploy/app/.env docker/config/backend.yaml docker/config/service.yaml
```

（此时磁盘上的 `backend.yaml/service.yaml` 已删除；它们本就是渲染产物。）

- [ ] **Step 10: Commit**

```bash
git add .gitignore docker/config/backend.yaml.envsubst docker/config/service.yaml.envsubst deploy/app/.env.example deploy/edge/.env.example deploy/scripts/lib.sh deploy/scripts/render-config.sh
git commit -m "feat(deploy): docker/config 模板化 + 受限 envsubst 渲染脚本 + .env.example
- 移除 docker/config/{backend,service}.yaml 跟踪(含云凭据)，改 .envsubst 模板+渲染产物
- guard_required/无残留/原子写 0600；render-config app|edge 分离"
```

---

### Task 4: `docker/compose.yaml` 端口收紧 + docker/README

**Files:**
- Modify: `docker/compose.yaml`
- Modify: `docker/README.md`

- [ ] **Step 1: 7080 回环 + 删 8084**

`docker/compose.yaml`：
1. `ai-chat-backend` 的 `ports: - "7080:7080"` → `- "127.0.0.1:7080:7080"`。
2. `proxy` 服务整个 `ports:` 块（`- "8084:8084"`）删除（仅网内 `proxy:8084` 可达，攻击面最小）。
3. 文件头注释（第 1-9 行区域）追加三行：

```yaml
#  - 安全：7080 仅绑宿主机回环(127.0.0.1:7080:7080)，proxy 不暴露宿主端口；
#    公网入口走 deploy/edge 的 Nginx→frps→frpc→127.0.0.1:7080。
#  - docker compose 前必须先运行 ../deploy/scripts/render-config.sh app（生成 docker/config/*.yaml）。
```

- [ ] **Step 2: 更新 docker/README**

在「二、启动」小节 `docker compose up -d --build` 之前插入：

```markdown
> **重要**：`docker/config/*.yaml` 现在是**渲染产物**（不入库）。启动前先：
> ```bash
> cp ../deploy/app/.env.example ../deploy/app/.env   # 并按需填写(生产必填向量库/FRP/DeepSeek 等)
> ../deploy/scripts/render-config.sh app
> ```
> 宿主开发/CI 路径 `./start.sh`、`ai-chat-stack/` 不受影响；本仓库 docker compose 曾未验证（无 daemon），属安全收紧要件的取舍。
```

并把 `curl -s http://localhost:7080` 注释改为提示仍可用（回环）。`8084` 相关说明若有，标记"不再暴露宿主端口"。

- [ ] **Step 3: 校验**

Run: `python3 -c "import yaml; yaml.safe_load(open('docker/compose.yaml')); print('compose yaml OK')"` → `compose yaml OK`。确认 `grep -n "8084\|7080" docker/compose.yaml` 只出现回环 `7080`，无 `8084` 宿主映射。

- [ ] **Step 4: Commit**

```bash
git add docker/compose.yaml docker/README.md
git commit -m "refactor(docker): 7080 收紧回环、proxy 不暴露宿主端口、README 注明 render 前置
- compose 需先 render-config app(破坏性变更，README 明示)"
```

---

### Task 5: 应用节点 frpc 独立栈 + `deploy-app.sh`

**Files:**
- Create: `deploy/app/tunnel/frpc.yaml.envsubst`
- Create: `deploy/app/compose.yaml`
- Create: `deploy/scripts/deploy-app.sh`

**Interfaces:**
- Consumes: Task 3 的 `lib.sh` 函数；`docker/config/*.yaml` 渲染产物；`/api/readyz`（Task 2）。
- Produces: `deploy/app/tunnel/frpc.yaml`（渲染产物，gitignore）；部署顺序：预检→render app→主 compose up→等 readyz→frp verify→frpc 栈 up。

- [ ] **Step 1: frpc 模板**

创建 `deploy/app/tunnel/frpc.yaml.envsubst`（对齐主方案 §5.5，字段一致）：

```yaml
serverAddr: "${FRP_SERVER_ADDR}"
serverPort: ${FRP_BIND_PORT}

auth:
  method: token
  token: "${FRP_AUTH_TOKEN}"

transport:
  protocol: tcp
  tcpMux: true
  poolCount: 2
  heartbeatInterval: 30
  heartbeatTimeout: 90
  tls:
    enable: true

loginFailExit: false

log:
  to: console
  level: info
  maxDays: 7

proxies:
  - name: echo-chat-web
    type: http
    localIP: 127.0.0.1
    localPort: 7080
    customDomains:
      - "${PUBLIC_DOMAIN}"
```

> FRP YAML 中端口/字段均为纯数字/字符串；`serverPort` 无引号亦可，为一致给 `FRP_BIND_PORT` 数字无引号。

- [ ] **Step 2: frpc compose**

创建 `deploy/app/compose.yaml`：

```yaml
# frpc 独立栈（应用节点）。ECHO-CHAT 主栈见 docker/compose.yaml（deploy-app.sh 串起两者）。
name: echo-chat-frpc

services:
  frpc:
    image: ${FRPC_IMAGE:-snowdreamtech/frpc:0.62.1}
    restart: unless-stopped
    network_mode: host
    command: ["-c", "/app/config.yaml"]
    volumes:
      - ./tunnel/frpc.yaml:/app/config.yaml:ro
```

（host 网络 → `localIP: 127.0.0.1` 可达宿主机回环 publish 的 7080。image tag 如与实际不符，目标机 `docker image inspect` 后改 `.env` 覆盖 `FRPC_IMAGE`。）

- [ ] **Step 3: deploy-app.sh**

创建 `deploy/scripts/deploy-app.sh`：

```bash
#!/usr/bin/env bash
# 应用节点一键部署：预检 → 渲染 → 主 compose build/up → 等 /api/readyz → frpc。
# 用法: sudo bash deploy/scripts/deploy-app.sh
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

APP_ENV="${REPO_ROOT}/deploy/app/.env"
APP_ENV_EXAMPLE="${REPO_ROOT}/deploy/app/.env.example"
COMPOSE_MAIN="${REPO_ROOT}/docker/compose.yaml"
COMPOSE_FRPC="${REPO_ROOT}/deploy/app/compose.yaml"
HEALTH_URL="http://127.0.0.1:7080/api/readyz"

[[ $EUID -eq 0 ]] || info "提示：非 root 运行时部分 docker 命令可能需 sudo/组权限"

require_env "${APP_ENV}" "${APP_ENV_EXAMPLE}"
info "预检主机资源..."
preflight_host 8192 40960

info "渲染配置..."
bash "${REPO_ROOT}/deploy/scripts/render-config.sh" app

info "启动 ECHO-CHAT 主栈..."
docker compose --env-file "${APP_ENV}" -f "${COMPOSE_MAIN}" up -d --build

info "等待 /api/readyz ..."
for i in $(seq 1 60); do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    info "readyz OK (第 ${i} 次探测)"
    break
  fi
  [[ $i -eq 60 ]] && die "等待 /api/readyz 超时 60s"
  sleep 2
done

info "FRP 配置自检（best-effort，P1-4）..."
if docker run --rm --entrypoint frpc "${FRPC_IMAGE:-snowdreamtech/frpc:0.62.1}" verify -c /app/config.yaml >/dev/null 2>&1; then
  info "frpc verify OK"
else
  info "frpc verify 不可用（镜像 ENTRYPOINT 不同/无此子命令）——跳过；请在目标机 docker image inspect 后核对 FRPC_IMAGE"
fi

info "启动 frpc 独立栈..."
docker compose --env-file "${APP_ENV}" -f "${COMPOSE_FRPC}" up -d
docker compose -f "${COMPOSE_FRPC}" logs --tail=20 frpc

info "完成。继续: bash deploy/scripts/smoke-test.sh app"
```

- [ ] **Step 4: 本地静态校验**

```bash
bash -n deploy/scripts/deploy-app.sh && bash -n deploy/scripts/lib.sh && bash -n deploy/scripts/render-config.sh && echo "bash syntax OK"
python3 -c "import yaml; yaml.safe_load(open('deploy/app/compose.yaml')); yaml.safe_load(open('deploy/app/tunnel/frpc.yaml.envsubst')); print('yaml OK')"
```

Expected: `bash syntax OK`、`yaml OK`。

- [ ] **Step 5: 补 render-config app 分支的 frpc 渲染**

修改 `deploy/scripts/render-config.sh` 的 `app)` 分支，在两条 docker/config 渲染后追加：

```bash
    render_restricted "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml.envsubst" \
      "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml" \
      '${FRP_SERVER_ADDR} ${FRP_BIND_PORT} ${FRP_AUTH_TOKEN} ${PUBLIC_DOMAIN}'
```

再次 `bash -n deploy/scripts/render-config.sh` 通过。

- [ ] **Step 6: Commit**

```bash
git add deploy/app/tunnel/frpc.yaml.envsubst deploy/app/compose.yaml deploy/scripts/deploy-app.sh deploy/scripts/render-config.sh
git commit -m "feat(deploy): 应用节点 frpc 独立栈(host网络) + deploy-app.sh(等 /api/readyz 后起 frpc)"
```

---

### Task 6: 公网入口节点 frps + 两阶段 Nginx + `deploy-edge.sh`（P0-2/P0-3）

**Files:**
- Create: `deploy/edge/tunnel/frps.yaml.envsubst`
- Create: `deploy/edge/compose.yaml`
- Create: `deploy/edge/nginx/echo-chat.bootstrap.conf.envsubst`
- Create: `deploy/edge/nginx/echo-chat.conf.envsubst`
- Create: `deploy/scripts/deploy-edge.sh`
- Modify: `deploy/scripts/render-config.sh`

**Interfaces:**
- Produces: `deploy/edge/tunnel/frps.yaml`、`deploy/edge/nginx/echo-chat.conf`（渲染产物）；edge 部署两阶段：cert 缺失→bootstrap Nginx→certbot webroot→全量 HTTPS；已有 cert→直接全量。

- [ ] **Step 1: frps 模板**

创建 `deploy/edge/tunnel/frps.yaml.envsubst`（对齐主方案 §5.3，含安全收紧）：

```yaml
bindAddr: 0.0.0.0
bindPort: ${FRP_BIND_PORT}

# HTTP 虚拟主机只供本机 Nginx 访问（compose 将端口 publish 到 127.0.0.1）。
vhostHTTPPort: ${FRP_VHOST_HTTP_PORT}

auth:
  method: token
  token: "${FRP_AUTH_TOKEN}"

transport:
  tcpMux: true
  maxPoolCount: 5

webServer:
  addr: 127.0.0.1
  port: 7500
  user: "frpadmin"
  password: "${FRP_DASHBOARD_PASSWORD}"

log:
  to: console
  level: info
  maxDays: 7
```

- [ ] **Step 2: edge compose**

创建 `deploy/edge/compose.yaml`：

```yaml
# 公网入口节点：frps + nginx。运行目录=deploy/edge（compose 自动读同目录 .env）。
name: echo-chat-edge

services:
  frps:
    image: ${FRPS_IMAGE:-snowdreamtech/frps:0.62.1}
    restart: unless-stopped
    command: ["-c", "/app/config.yaml"]
    volumes:
      - ./tunnel/frps.yaml:/app/config.yaml:ro
    ports:
      - "${FRP_BIND_PORT:-39000}:${FRP_BIND_PORT:-39000}"
      - "127.0.0.1:${FRP_VHOST_HTTP_PORT:-39001}:${FRP_VHOST_HTTP_PORT:-39001}"
      - "127.0.0.1:7500:7500"

  nginx:
    image: nginx:1.27-alpine
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./nginx/echo-chat.conf:/etc/nginx/conf.d/echo-chat.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - /var/www/certbot:/var/www/certbot:ro
    depends_on:
      - frps
```

> 说明：`echo-chat.conf` 是渲染产物，先由 deploy-edge.sh 写好再 `up -d nginx`，保证 nginx 首次启动即引用已存在的有效 conf（无证书时是 bootstrap）。

- [ ] **Step 3: bootstrap Nginx 模板**

创建 `deploy/edge/nginx/echo-chat.bootstrap.conf.envsubst`（无 ssl，仅 80/acme/503）：

```nginx
# 首次引导：证书不存在时的最小 80 监听，只为 acme webroot。由 deploy-edge.sh 写入 echo-chat.conf。
server {
    listen 80;
    listen [::]:80;
    server_name ${PUBLIC_DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 503;
    }
}
```

- [ ] **Step 4: 全量 Nginx 模板**

创建 `deploy/edge/nginx/echo-chat.conf.envsubst`（对齐主方案 §5.6；XFF 覆盖式 + 流式禁缓冲 + 安全头）：

```nginx
# ECHO-CHAT 公网 HTTPS 正式配置（渲染产物）。模板变量仅 PUBLIC_DOMAIN / FRP_VHOST_HTTP_PORT。
# 与主方案 §5.6 的有意差异：X-Forwarded-For 用 $remote_addr 覆盖，杜绝外部伪造链首(评审 P0-3)。
limit_req_zone $binary_remote_addr zone=echo_api:10m rate=10r/s;

server {
    listen 80;
    listen [::]:80;
    server_name ${PUBLIC_DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name ${PUBLIC_DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${PUBLIC_DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${PUBLIC_DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;

    client_max_body_size 2m;

    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location /api/ {
        limit_req zone=echo_api burst=30 nodelay;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;

        proxy_http_version 1.1;
        proxy_set_header Connection "";

        # ECHO-CHAT 流式响应：禁止一切缓冲。
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        add_header X-Accel-Buffering no;

        proxy_connect_timeout 10s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;

        proxy_pass http://127.0.0.1:${FRP_VHOST_HTTP_PORT};
    }

    location / {
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_connect_timeout 10s;
        proxy_read_timeout 60s;
        proxy_pass http://127.0.0.1:${FRP_VHOST_HTTP_PORT};
    }
}
```

- [ ] **Step 5: deploy-edge.sh（两阶段）**

创建 `deploy/scripts/deploy-edge.sh`：

```bash
#!/usr/bin/env bash
# 公网入口节点一键部署（两阶段 TLS）：
#   证书缺失 → bootstrap Nginx(80) → certbot webroot → 原子换全量 HTTPS conf → reload
#   证书已有 → 直接全量。幂等：不重复申请、不中断服务。
# 用法: sudo bash deploy/scripts/deploy-edge.sh
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

EDGE_DIR="${REPO_ROOT}/deploy/edge"
EDGE_ENV="${EDGE_DIR}/.env"
NGINX_CONF="${EDGE_DIR}/nginx/echo-chat.conf"
BOOT_TMPL="${EDGE_DIR}/nginx/echo-chat.bootstrap.conf.envsubst"
FULL_TMPL="${EDGE_DIR}/nginx/echo-chat.conf.envsubst"
FRPS_YAML="${EDGE_DIR}/tunnel/frps.yaml"

[[ $EUID -eq 0 ]] || die "deploy-edge 需要 root（写 /etc/letsencrypt 与 nginx reload）"

require_env "${EDGE_ENV}" "${EDGE_DIR}/.env.example"
info "预检 edge 主机..."
preflight_host 2048 40960
command -v certbot >/dev/null || die "缺少 certbot；先: apt-get install -y certbot"
command -v dig >/dev/null || die "缺少 dig；先: apt-get install -y dnsutils"

info "渲染 edge 配置..."
bash "${REPO_ROOT}/deploy/scripts/render-config.sh" edge

CERT_DIR="/etc/letsencrypt/live/${PUBLIC_DOMAIN}"
FULLCHAIN="${CERT_DIR}/fullchain.pem"

render_full() {  # 全量 conf → nginx conf 路径（先在暂存目录校验候选，再落盘）
  local stage cand
  stage="$(mktemp -d "${NGINX_CONF}.stage.XXXXXX")"
  cand="${stage}/echo-chat.conf"   # 镜像 conf.d 只 include *.conf
  trap 'rm -rf "${stage}" "${NGINX_CONF}.candidate"*' RETURN
  umask 077
  envsubst '${PUBLIC_DOMAIN} ${FRP_VHOST_HTTP_PORT}' < "${FULL_TMPL}" > "${cand}"
  guard_no_residue "${cand}"
  # 候选校验：一次性容器只挂载含候选 conf 的 stage 目录 + 真实证书 → nginx -t
  if ! docker run --rm \
       -v "${stage}:/etc/nginx/conf.d:ro" \
       -v /etc/letsencrypt:/etc/letsencrypt:ro \
       -v /var/www/certbot:/var/www/certbot:ro \
       nginx:1.27-alpine nginx -t; then
    die "nginx -t 校验失败(全量 conf 候选)，未改动现有 conf"
  fi
  rm -f "${NGINX_CONF}.prev"
  [[ -f "${NGINX_CONF}" ]] && cp -a "${NGINX_CONF}" "${NGINX_CONF}.prev"
  cp -a "${cand}" "${NGINX_CONF}"   # 覆写原 inode 内容，bind 挂载容器 reload 即可见
  chmod 644 "${NGINX_CONF}"
}

render_bootstrap() {
  umask 077
  envsubst '${PUBLIC_DOMAIN}' < "${BOOT_TMPL}" > "${NGINX_CONF}"
  guard_no_residue "${NGINX_CONF}"
  chmod 644 "${NGINX_CONF}"
}

start_frps() {
  docker compose -f "${EDGE_DIR}/compose.yaml" up -d frps
  docker compose -f "${EDGE_DIR}/compose.yaml" logs --tail=20 frps
}

start_nginx() {
  docker compose -f "${EDGE_DIR}/compose.yaml" up -d nginx
}

reload_nginx() {
  docker compose -f "${EDGE_DIR}/compose.yaml" exec nginx nginx -s reload || \
    docker compose -f "${EDGE_DIR}/compose.yaml" restart nginx
}

info "启动 frps..."
start_frps

if [[ -f "${FULLCHAIN}" ]]; then
  info "证书已存在: ${FULLCHAIN}，直接部署全量 HTTPS conf"
  render_full
  if docker compose -f "${EDGE_DIR}/compose.yaml" ps --status running nginx >/dev/null 2>&1; then
    reload_nginx
  else
    start_nginx
  fi
else
  info "未发现证书，进入两阶段：bootstrap → certbot → HTTPS"
  render_bootstrap
  start_nginx
  sleep 2

  info "校验 DNS..."
  if [[ "$(dig +short "${PUBLIC_DOMAIN}" | head -1)" != "${PUBLIC_IP:-}" ]] \
     && ! ip -4 addr show | grep -qF "$(dig +short "${PUBLIC_DOMAIN}" | head -1)"; then
    # 允许 PUBLC_IP 显式设置；否则退化为仅提示（部分云 NAT 环境下本机 IP 探测困难）。
    [[ -n "${PUBLIC_IP:-}" ]] && die "DNS ${PUBLIC_DOMAIN} 未指向 ${PUBLIC_IP}"
    info "提示：无法确认 DNS 指向本机，certbot 若失败请检查解析与安全组(80)"
  fi

  info "申请证书(webroot)..."
  if ! certbot certonly --webroot -w /var/www/certbot \
       -d "${PUBLIC_DOMAIN}" --email "${ADMIN_EMAIL}" \
       --agree-tos --no-eff-email --non-interactive; then
    info "证书申请失败：保留 bootstrap(80) 供排查。修好后再跑本脚本（幂等）。"
    exit 1
  fi
  [[ -f "${FULLCHAIN}" ]] || die "certbot 成功但证书文件缺失: ${FULLCHAIN}"

  render_full
  reload_nginx
fi

info "HTTPS 验收..."
curl -fsS "https://${PUBLIC_DOMAIN}/api/health" >/dev/null \
  && info "https://${PUBLIC_DOMAIN} 验收通过" \
  || die "HTTPS 冒烟失败，查 nginx 日志: docker compose -f ${EDGE_DIR}/compose.yaml logs --tail=50 nginx"

info "续期 deploy hook 建议写入 /etc/letsencrypt/renewal-hooks/deploy/reload-echo-nginx.sh（见 deploy/README.md）"
```

- [ ] **Step 6: 补 render-config edge 的 nginx conf 说明（可选）**

`render-config.sh` edge 分支已渲染 frps；bootstrap/full conf 由 `deploy-edge.sh` 按需渲染（不重复）。

- [ ] **Step 7: 本地静态校验（含自签证书 nginx -t）**

```bash
bash -n deploy/scripts/deploy-edge.sh && echo "syntax OK"
python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['deploy/edge/compose.yaml','deploy/edge/tunnel/frps.yaml.envsubst']]; print('yaml OK')"

# 渲染两版 conf 到临时目录，并用临时 wrapper+自签证书做 nginx -t（仅语法；本机 nginx 1.18）
export PUBLIC_DOMAIN=chat.example.com FRP_VHOST_HTTP_PORT=39001
mkdir -p /tmp/echot/conf.d && cd /tmp/echot
# 自签证书要落到与 conf 一致的文件名（fullchain.pem / privkey.pem）
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
  -keyout privkey.pem -out fullchain.pem -subj "/CN=chat.example.com" >/dev/null 2>&1
envsubst '${PUBLIC_DOMAIN} ${FRP_VHOST_HTTP_PORT}' \
  < /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT/deploy/edge/nginx/echo-chat.conf.envsubst > conf.d/echo-chat.conf
grep -q '\$\{' conf.d/echo-chat.conf && echo "RESIDUE!" || echo "no residue"
# 让 conf 里 /etc/letsencrypt/live/... 证书路径指向本地临时文件
sed -i 's|/etc/letsencrypt/live/chat.example.com|/tmp/echot|g' conf.d/echo-chat.conf
# nginx -t wrapper（http{} 内 include conf.d/*.conf，触发 443 server 块校验）
printf 'pid /tmp/echot/nginx.pid; error_log /tmp/echot/error.log; events { worker_connections 64; } http { include /etc/nginx/mime.types; include /tmp/echot/conf.d/*.conf; }\n' > main.conf
nginx -t -c /tmp/echot/main.conf -p /tmp/echot/
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT && rm -rf /tmp/echot
```

Expected: `syntax OK`、`yaml OK`、`no residue`、`nginx: configuration ... test is successful`。

- [ ] **Step 8: Commit**

```bash
git add deploy/edge/tunnel/frps.yaml.envsubst deploy/edge/compose.yaml deploy/edge/nginx/echo-chat.bootstrap.conf.envsubst deploy/edge/nginx/echo-chat.conf.envsubst deploy/scripts/deploy-edge.sh deploy/scripts/render-config.sh
git commit -m "feat(deploy): 公网入口 frps + 两阶段 Nginx(TLS bootstrap→certbot→HTTPS) + deploy-edge.sh
- XFF 覆盖式转发(P0-3)；全量 conf 候选先 nginx -t 再落盘；幂等不重复申请"
```

---

### Task 7: smoke / secret 扫描 / README / 一致性 & 预检收口

**Files:**
- Create: `deploy/scripts/smoke-test.sh`
- Create: `deploy/scripts/scan-secrets.sh`
- Create: `deploy/README.md`

**Interfaces:**
- Consumes: Task 3-6 全部产物与 lib.sh；主方案 §9 验收清单。

- [ ] **Step 1: smoke-test.sh（分层 + 流式断言）**

创建 `deploy/scripts/smoke-test.sh`：

```bash
#!/usr/bin/env bash
# ECHO-CHAT 公网隧道分层验收（主方案 §9）。层数越低越先验证。
# 用法:
#   bash smoke-test.sh app                              # 应用节点本地 7080
#   bash smoke-test.sh edge                             # 入口节点: frps host 路由 + https
#   DOMAIN=chat.example.com bash smoke-test.sh e2e      # 端到端(公网)
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

MODE="${1:-app}"
DOMAIN="${DOMAIN:-chat.example.com}"
APP_BASE="http://127.0.0.1:7080"
EDGE_VHOST="http://127.0.0.1:${FRP_VHOST_HTTP_PORT:-39001}"
AUTH="${AUTH:-}"   # 不传则跳过需鉴权用例

req() { curl -fsS -o /dev/null -w '%{http_code}' "$@"; }

case "${MODE}" in
  app)
    info "L1 应用节点本地..."
    code=$(req "${APP_BASE}/api/readyz") && [[ "$code" == "200" ]] || die "L1 /api/readyz = $code"
    code=$(req "${APP_BASE}/api/health") && [[ "$code" == "200" ]] || die "L1 /api/health = $code"
    ;;
  edge)
    info "L2 frps host 路由(绕过 Nginx)..."
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${DOMAIN}" "${EDGE_VHOST}/api/health") \
      && [[ "$code" == "200" ]] || die "L2 frps vhost = $code (检查 frpc customDomains/frps token)"
    info "L3 HTTPS..."
    code=$(req "https://${DOMAIN}/api/health") && [[ "$code" == "200" ]] || die "L3 https health = $code"
    ;;
  e2e)
    info "L4 流式聊天..."
    [[ -n "${AUTH}" ]] || die "e2e 需 AUTH=<登录token>"
    out="$(mktemp)"   # 顶层不可用 local(set -e 下报 local: only in function)
    trap 'rm -f "${out}"' RETURN
    start=$(date +%s%N)
    # -N 关缓冲；记录首字节到文件；统计响应时间
    curl -N -sS "https://${DOMAIN}/api/chat-process" \
      -H "Authorization: ${AUTH}" -H 'Content-Type: application/json' \
      --data '{"prompt":"数到三","options":{}}' > "${out}" &
    cpid=$!
    first_byte=0
    while kill -0 "$cpid" 2>/dev/null; do
      if [[ -s "${out}" ]]; then
        first_byte=$(( ($(date +%s%N) - start) / 1000000 )); break
      fi
      sleep 0.05
    done
    wait "$cpid" || true
    end=$(( ($(date +%s%N) - start) / 1000000 ))
    info "首字节 ${first_byte}ms / 总时长 ${end}ms / 字节 $(wc -c < "${out}")"
    [[ -s "${out}" ]] || die "e2e 无任何响应体(连接/鉴权/上游失败)"
    chunks=$(grep -c '^\n' "${out}" || true)
    [[ "$chunks" -ge 2 ]] || die "chunk 数不足(=$chunks)，疑似代理缓冲聚合"
    [[ "${first_byte}" -lt 30000 ]] || die "首字节超过 30s，流式不通"
    ;;
  *) die "用法: smoke-test.sh app|edge|e2e" ;;
esac
info "SMOKE ${MODE} PASS"
```

> 说明：`chunk` 判据依赖后端在流末写 `\n`（见 `pkg/controllers/chat.go` 末包写法）。若更严格多 chunk 判据需要，用 `grep -c $'\n'` 统计换行数 ≥2 即可，实现时按实际流式输出微调。

- [ ] **Step 2: scan-secrets.sh**

创建 `deploy/scripts/scan-secrets.sh`：

```bash
#!/usr/bin/env bash
# Secret 启发式扫描：对给定路径集扫描高熵/云凭据/占位。CI/提交前可调用。
# 用法: bash scan-secrets.sh [path...]   (默认: 全部 render 产物 + docker/config)
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TARGETS=("$@")
[[ ${#TARGETS[@]} -eq 0 ]] && TARGETS=(
  "${REPO_ROOT}/docker/config" "${REPO_ROOT}/deploy" )

# 模式：云 AccessKey/SecretKey、sk- 密钥、长 hex/base64、私钥、占位
patterns=(
  'AKID[0-9A-Za-z]{13,}'
  'LTAI[0-9A-Za-z]{12,}'
  'sk-[0-9A-Za-z]{20,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'access[_-]?(key|secret|token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{16,}'
  'password[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}'
)
hits=0
while IFS= read -r f; do
  for p in "${patterns[@]}"; do
    if grep -Eiq "$p" "$f"; then
      echo "[scan] 命中 '$p' → $f"
      hits=$((hits+1))
    fi
  done
  # 渲染产物残留占位
  if grep -qE 'CHANGE_ME|REPLACE_ME|<[^>]*>|sk-placeholder' "$f"; then
    echo "[scan] 占位残留 → $f"
    hits=$((hits+1))
  fi
done < <(find "${TARGETS[@]}" -type f \( -name '*.yaml' -o -name '*.env*' -o -name '*.conf' -o -name '*.sh' \) 2>/dev/null)

if [[ "$hits" -gt 0 ]]; then
  die "发现 $hits 处可疑内容（检查是否真实凭据或需清理占位）"
fi
info "scan-secrets 通过"
```

> 误报属预期：本脚本只做门槛提示，是否放行由人判断。产物应为 gitignore 不提交，所以生产渲染产物通常不会出现在扫描结果。

- [ ] **Step 3: 一致性校验函数补进 lib.sh**

`deploy/scripts/lib.sh` 末尾追加（供 deploy-app/edge 调用）：

```bash
# 渲染产物一致性抽检（P1-3）：把 .env 里的端口/域名与产物比对。
check_consistency() {
  local side="$1"
  case "$side" in
    app)
      grep -q "serverPort: ${FRP_BIND_PORT:-}" "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml" \
        || die "frpc serverPort != FRP_BIND_PORT"
      grep -q "customDomains:" "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml" || die "frpc customDomains 缺失"
      ;;
    edge)
      grep -q "bindPort: ${FRP_BIND_PORT:-}" "${REPO_ROOT}/deploy/edge/tunnel/frps.yaml" \
        || die "frps bindPort != FRP_BIND_PORT"
      grep -q "vhostHTTPPort: ${FRP_VHOST_HTTP_PORT:-}" "${REPO_ROOT}/deploy/edge/tunnel/frps.yaml" \
        || die "frps vhostHTTPPort != FRP_VHOST_HTTP_PORT"
      grep -q "server_name ${PUBLIC_DOMAIN:-};" "${REPO_ROOT}/deploy/edge/nginx/echo-chat.conf" \
        || die "nginx server_name != PUBLIC_DOMAIN"
      grep -q "proxy_pass http://127.0.0.1:${FRP_VHOST_HTTP_PORT:-}" "${REPO_ROOT}/deploy/edge/nginx/echo-chat.conf" \
        || die "nginx upstream != FRP_VHOST_HTTP_PORT"
      ;;
  esac
}
```

精确插入位置：
- `deploy-app.sh`：紧接 `render-config.sh app` 之后、`启动 ECHO-CHAT 主栈...` 之前加一行 `check_consistency app`（此时 `docker/config/*` 与 `frpc.yaml` 均已渲染）。
- `deploy-edge.sh`：紧接函数定义之后、`info "启动 frps..."` 之前无需调（conf 未定）；在**最终 `info "HTTPS 验收..."` 之前**加一行 `check_consistency edge`（此时 `echo-chat.conf` 已写入，frps.yaml 已渲染）。

两个脚本改完都跑 `bash -n` 确认无语法错误。

- [ ] **Step 4: deploy/README.md**

创建 `deploy/README.md`，内容至少覆盖：

```markdown
# ECHO-CHAT 公网部署（deploy/）

拓扑与主方案：docs/deploy/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md
落地 spec：docs/superpowers/specs/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md

## 双节点
- 应用 VM：ECHO-CHAT 主 compose（127.0.0.1:7080）+ deploy/app frpc 栈。
- 入口 VM：deploy/edge frps + nginx(80/443)。
- 变量对照：见 deploy/{app,edge}/.env.example（common: PUBLIC_DOMAIN/FRP_AUTH_TOKEN 两端一致）。
- FRP token 用: openssl rand -hex 32（放 .env 的 FRP_AUTH_TOKEN）。
- 安全组：22(限管理IP)/80/443/39000(限应用VM出口)；39001/7500/7080 不对外。

## 一键
- 应用: cp deploy/app/.env.example deploy/app/.env && sudo bash deploy/scripts/deploy-app.sh
- 入口: cp deploy/edge/.env.example deploy/edge/.env && sudo bash deploy/scripts/deploy-edge.sh
- 分层验收: bash deploy/scripts/smoke-test.sh app|edge|e2e

## MySQL（应用 VM，外部/宿主机）
CREATE DATABASE ai_chat CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'echo_chat'@'%' IDENTIFIED BY '<STRONG_PASSWORD>';
GRANT ALL PRIVILEGES ON ai_chat.* TO 'echo_chat'@'%';
FLUSH PRIVILEGES;
# MYSQL_DSN=echo_chat:<STRONG_PASSWORD>@tcp(host.docker.internal:3306)/ai_chat?charset=utf8mb4&parseTime=true

## 镜像注意
- frps/frpc 默认 snowdreamtech:0.62.1；目标机 docker image inspect 核对 ENTRYPOINT；
  若换镜像保持 frps/frpc 同版本；固定版本后再固定 digest(P1)。

## 证书续期（edge）
- /etc/letsencrypt/renewal-hooks/deploy/reload-echo-nginx.sh:
    #!/usr/bin/env bash
    docker compose -f /opt/echo-chat-edge/compose.yaml exec -T nginx nginx -s reload
- sudo certbot renew --dry-run 验证。

## 交付边界
- 本仓库改动为静态验证（go build/test、nginx -t、yaml、bash -n）。
- 公网 TLS/登录/流式/断线/回滚验收在目标 VM 执行（smoke-test.sh + 主方案 §9/§13）。
- 单机演示(主方案 §3.2)：入口与应用同机时可让 frpc serverAddr 指向 127.0.0.1。

## Secret 纪律
- .env 与 render 产物已 gitignore；提交前 bash deploy/scripts/scan-secrets.sh。
- docker/config/*.yaml 需先 render-config app（见 docker/README.md）。
```

- [ ] **Step 5: 静态自检全量**

Run（仓库根）：
```bash
bash -n deploy/scripts/*.sh && echo "bash OK"
python3 -c "
import yaml
for f in ['docker/compose.yaml','docker/config/backend.yaml.envsubst','docker/config/service.yaml.envsubst','deploy/app/compose.yaml','deploy/app/tunnel/frpc.yaml.envsubst','deploy/edge/compose.yaml','deploy/edge/tunnel/frps.yaml.envsubst']:
    yaml.safe_load(open(f)); print('yaml OK', f)"
```
Expected：全部 OK。

- [ ] **Step 6: Commit**

```bash
git add deploy/scripts/lib.sh deploy/scripts/smoke-test.sh deploy/scripts/scan-secrets.sh deploy/scripts/deploy-app.sh deploy/scripts/deploy-edge.sh deploy/README.md
git commit -m "feat(deploy): smoke/scan-secrets/一致性校验/README + 预检收口"
```

---

### Task 8: 全量静态验收 + 工作树卫生

**Files:**（只读核对）
- `ai-chat-backend` 全部；`docker/` 与 `deploy/` 全部产物。

- [ ] **Step 1: Go 构建 + 全量测试**

Run: `cd ai-chat-backend && go build ./... && go test ./...`
Expected：全部 PASS。

- [ ] **Step 2: 渲染全链冒烟（app+edge 双 .env 假值）**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
cp deploy/app/.env.example deploy/app/.env; cp deploy/edge/.env.example deploy/edge/.env
sed -i -E 's/CHANGE_ME_random_hex_64/1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff/g; s/CHANGE_ME_sk-xxx/sk-dummy/g; s#CHANGE_ME_vector_db_url#http://vdbtest:60000#; s/CHANGE_ME_vector_db_user/u/; s/CHANGE_ME_vector_db_pwd/p/' deploy/app/.env
sed -i -E 's/CHANGE_ME_random_hex_64/1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff/g; s/CHANGE_ME_admin@example.com/admin@example.com/; s/CHANGE_ME_dashboard_pwd/dashpwd/' deploy/edge/.env
# 手工补 edge 渲染需要的 frps（deploy-edge.sh 里做 render_full/render_bootstrap 依赖证书，这里只测 frps 渲染与 conf envsubst）
bash deploy/scripts/render-config.sh app
bash deploy/scripts/render-config.sh edge
export PUBLIC_DOMAIN=chat.example.com FRP_VHOST_HTTP_PORT=39001
envsubst '${PUBLIC_DOMAIN} ${FRP_VHOST_HTTP_PORT}' < deploy/edge/nginx/echo-chat.conf.envsubst > /tmp/full.conf
envsubst '${PUBLIC_DOMAIN}' < deploy/edge/nginx/echo-chat.bootstrap.conf.envsubst > /tmp/boot.conf
grep -l '\$\{' /tmp/full.conf /tmp/boot.conf 2>/dev/null && die=1 || echo "no residue full/boot"
grep -q '$host' /tmp/full.conf && echo "nginx \$host preserved"
```

Expected：render 输出成功、无残留、`$host` 保留、yaml 均可加载。

- [ ] **Step 3: 工作树卫生确认**

```bash
git status --short
git diff --stat HEAD
git diff --cached --name-only
```

Expected：
- `openai-api-proxy/dev.config.yaml` 仍是 ` M`（未 staged）；
- 无 `docker/config/backend.yaml|service.yaml`、`deploy/*/.env`、渲染产物被跟踪；
- `git diff --cached` 为空。

- [ ] **Step 4: 更新 spec 勾选状态（如 spec 含 checklist）**

无 checklist；仅在 commit message 里标注交付边界。无需改动。

- [ ] **Step 5: 最终提交（如有零散修正）**

```bash
git status --short   # 若 git add 误含渲染产物/.env → git reset 后按精确路径 add
```

完成后总结：交付文件清单、本机验证通过项、目标机待执行项（主方案 §9/§13）。
