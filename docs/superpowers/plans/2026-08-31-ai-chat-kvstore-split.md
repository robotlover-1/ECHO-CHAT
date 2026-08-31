# ai-chat 与 kvstore 拆分 + 明文 KV + 公有大模型 + tokens 统计 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ai-chat 与 kvstore 拆成两个独立 git 仓库（kvstore 作 ai-chat 的 submodule），并完成：删除短信验证码改免密自动登录、接入公有大模型（DeepSeek，key 走环境变量）、Q-A 以明文字符串 KV 存储（key=原始问题、value=原始回答、不做 hash）、回答来源标注（公有大模型/缓存命中）、网页按会话累计显示 tokens 总消耗与节省。

**Architecture:** 采用"先功能改造、后仓库拆分"的顺序。所有功能改动先在现有 `9.1-kvstore` 仓库中完成并逐任务验证（该环境已装好全部依赖），最后一步把 `kvstore/` 单独拆成 `pocket-kv` 仓库，其余组件构成 `ECHO-CHAT` 仓库并在其中以 submodule 引用 kvstore。存储层用双结构：明文 Q-A 存 array 引擎（`SET qa:<问题> <回答>`），向量索引存 hash 引擎（`HSET semcache:<问题> <[dim][vec]>`），VSEARCH 仍只扫 hash 引擎。

**Tech Stack:** C（kvstore，RESP 协议）、Go 1.19/1.20（gin/gRPC/go-openai/redis）、Python nuxt + tiktoken + jieba（tokenizer 嵌入/重排）、Vue3 + naive-ui + pnpm、git submodule。

## Global Constraints

- 中文回复；不客套、不迎合；不确定时标注置信度。
- 存储规则：问题-回答以 `qa:` 前缀 + **原始问题原文** 作 key，**原始回答原文** 作 value，明文字符串，禁止对 key 做 hash；向量索引 `semcache:` 前缀 + 原始问题作 key，value 为 `[u32 dim][float vec[dim]]` 二进制。
- 大模型：DeepSeek，`base_url = https://api.deepseek.com/v1`，模型 `deepseek-v4-flash`，key 一律从环境变量 `DEEPSEEK_API_KEY` 读取，**任何密钥不得写入 git**。
- 回答来源字段 `source` 取值仅 `"llm"` 或 `"cache"`；缓存命中不扣额度，LLM 回答扣额度。
- tokens 统计按当前会话在前端累计，不使用全局聚合。
- 拆分目标仓库：`git@github.com:robotlover-1/pocket-kv.git` 与 `git@github.com:robotlover-1/ECHO-CHAT.git`，均为全新初始提交；现有 `9.1-kvstore` 保持不变。
- 每任务结束必须提交；改动尽量小、可回滚；不引入新依赖；不破坏现有 kvstore 命令兼容性（SET/GET/HSET/HGET/VSEARCH 语义不变，只改 semcache record 格式）。

---

# Phase 0：前置准备

### Task 0：环境检查与停止运行中的服务

**Files:** 无

- [ ] **Step 1: 停止正在运行的旧服务**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore && ./stop.sh`（若提示没有服务可忽略）。
说明：功能改造期间会反复重建 kvstore 与各 Go 服务，避免端口占用。

- [ ] **Step 2: 确认构建工具可用**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
make -C kvstore --version 2>/dev/null; which protoc protoc-gen-go protoc-gen-go-grpc
```
Expected: `protoc`、`protoc-gen-go`、`protoc-gen-go-grpc` 均在 PATH（已在 `/usr/bin`、`/home/pp/go/bin` 验证存在）。

- [ ] **Step 3: 确认当前 9.1-kvstore 工作区干净**

Run: `git -C /home/pp/Desktop/ls_study/proj/9.1-kvstore status --short`
Expected: 无未提交改动（或只有已知的本地配置差异）。若 `openai-api-proxy/dev.config.yaml` 是本地真实 key 版本，先 `git stash` 或记下差异，避免误提交。

---

# Phase A：功能改造（全部在 /home/pp/Desktop/ls_study/proj/9.1-kvstore 中进行）

### Task 1：kvstore 向量 record 改为 `[dim][vec]` 格式

**Files:**
- Modify: `kvstore/src/storage/kvs_vector.c:14-31`（`parse_vec`）

**Interfaces:**
- Consumes: 无
- Produces: `parse_vec(value, vlen, want_dim, &vec_out, &dim_out)` —— 新格式 `[u32 dim][float vec[dim]]`，供 VSEARCH 使用；Task 2 的 semcache HSET 值必须匹配该格式。

- [ ] **Step 1: 重写 `parse_vec`**

将 `kvstore/src/storage/kvs_vector.c` 中第 15-31 行（`/* 值布局: [u32 qlen][q][u32 alen][a][u32 dim][float vec[dim]] */` 注释与整个 `parse_vec` 函数）替换为：

```c
/* 值布局: [u32 dim][float vec[dim]]（小端） */
static int parse_vec(const char *value, size_t vlen, int want_dim, const float **vec_out, int *dim_out) {
    if (!value || vlen < 4) return -1;
    uint32_t dim = 0;
    memcpy(&dim, value, 4);
    if ((int)dim != want_dim) return -1;
    if (vlen < 4 + (size_t)dim * 4) return -1;
    *vec_out = (const float *)(value + 4);
    *dim_out = (int)dim;
    return 0;
}
```

- [ ] **Step 2: 重建 kvstore**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore && make kvstore`
Expected: 编译成功，产物 `kvstore/kvstore` 更新。

- [ ] **Step 3: 冒烟验证 VSEARCH 不崩溃**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
./kvstore/kvstore configs/kvstore-ai.conf &  # 后台起（记录 pid）
sleep 1
# 手工插入一条新格式向量: dim=256, 全 0.1
python3 -c "import struct,sys;sys.stdout.buffer.write(struct.pack('<I',256)+struct.pack('<256f',*([0.1]*256)))" > /tmp/vec.bin
redis-cli -p 5160 -x HSET semcache:hello问题 < /tmp/vec.bin
redis-cli -p 5160 VSEARCH 256 "$(python3 -c "import struct,sys;sys.stdout.buffer.write(struct.pack('<256f',*([0.1]*256)))")" 5
kill %1
```
Expected: `HSET` 返回 `1`；`VSEARCH` 返回 `*2` 数组，其中包含 `semcache:hello问题` 与得分，无崩溃、无报错。
说明：redis-cli 无 `-x` 时可用 `--pipe` 方式，但本机已装较新 redis-cli，`-x` 可用。

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add kvstore/src/storage/kvs_vector.c
git commit -m "fix(kvstore): VSEARCH record 格式改为 [dim][vec]（配合明文 Q-A 存储）"
```

---

### Task 2：semcache 重写为明文 KV（key=原始问题，value=原始回答）

**Files:**
- Rewrite: `ai-chat-service/chat-server/semcache/semcache.go`

**Interfaces:**
- Consumes: `config.GetConfig().SemanticCache.{Enabled,Threshold,RerankThreshold}`；kvstore 命令 `VSEARCH`/`GET`/`SET`/`HSET`；tokenizer `/embed`、`/rerank`。
- Produces: `CacheQuery(ctx, query) (string, bool)`、`CacheWrite(ctx, query, answer) error`，语义与签名不变（Task 4 继续调用）。

- [ ] **Step 1: 整文件重写**

用以下内容整体替换 `ai-chat-service/chat-server/semcache/semcache.go`：

```go
package semcache

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"

	"ai-chat-service/pkg/config"
	kredis "ai-chat-service/pkg/db/redis"
	"github.com/redis/go-redis/v9"
)

const dim = 256

// 明文 Q-A 存储在 array 引擎（SET/GET）；向量索引在 hash 引擎（HSET，VSEARCH 只扫 hash）
const qaPrefix = "qa:"
const semPrefix = "semcache:"

type embedResp struct {
	Code      int       `json:"code"`
	Embedding []float64 `json:"embedding"`
	Msg       string    `json:"msg"`
}
type rerankResp struct {
	Code   int     `json:"code"`
	Score  float64 `json:"score"`
	Shared bool    `json:"shared"`
	Msg    string  `json:"msg"`
}

// poolClient 复用 ai-chat-service 已有的 redis 连接池：借出 *redis.Client，
// 单条命令执行后立即归还（避免连接得不到释放）。本包不保留跨请求连接。
type poolClient struct {
	pool   kredis.RedisPool
	client *redis.Client
}

func (c *poolClient) Do(ctx context.Context, args ...interface{}) (interface{}, error) {
	defer c.pool.Put(c.client)
	return c.client.Do(ctx, args...).Result()
}

func getClient() *poolClient {
	pool := kredis.GetPool()
	client := pool.Get()
	return &poolClient{pool: pool, client: client}
}

func embedText(ctx context.Context, text string) ([]float32, error) {
	body, _ := json.Marshal(map[string]string{"text": text})
	resp, err := http.Post(config.GetConfig().DependOn.Tokenizer.Address+"/embed",
		"application/json", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var r embedResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, err
	}
	if r.Code != 200 || len(r.Embedding) != dim {
		return nil, fmt.Errorf("embed failed: code=%d dim=%d", r.Code, len(r.Embedding))
	}
	vec := make([]float32, dim)
	for i, v := range r.Embedding {
		vec[i] = float32(v)
	}
	return vec, nil
}

func rerank(ctx context.Context, query, cached string) (float64, bool, error) {
	body, _ := json.Marshal(map[string]string{"query": query, "cached_query": cached})
	resp, err := http.Post(config.GetConfig().DependOn.Tokenizer.Address+"/rerank",
		"application/json", bytes.NewReader(body))
	if err != nil {
		return 0, false, err
	}
	defer resp.Body.Close()
	var r rerankResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return 0, false, err
	}
	if r.Code != 200 {
		return 0, false, fmt.Errorf("rerank failed: %s", r.Msg)
	}
	return r.Score, r.Shared, nil
}

func qaKey(query string) string { return qaPrefix + query }
func semKey(query string) string { return semPrefix + query }

// 向量索引值: [u32 dim][float vec[dim]]（小端），与 kvstore parse_vec 匹配
func encodeVec(vec []float32) []byte {
	buf := make([]byte, 4+len(vec)*4)
	binary.LittleEndian.PutUint32(buf, uint32(len(vec)))
	for i, v := range vec {
		binary.LittleEndian.PutUint32(buf[4+i*4:], math.Float32bits(v))
	}
	return buf
}

// CacheQuery: 嵌入查询 → VSEARCH → 阈值+rerank 校验 → 命中返回明文回答
func CacheQuery(ctx context.Context, query string) (string, bool) {
	cnf := config.GetConfig()
	if !cnf.SemanticCache.Enabled {
		return "", false
	}
	vec, err := embedText(ctx, query)
	if err != nil {
		return "", false
	}
	binVec := make([]byte, dim*4)
	for i, v := range vec {
		binary.LittleEndian.PutUint32(binVec[i*4:], math.Float32bits(v))
	}
	res, err := getClient().Do(ctx, "VSEARCH", strconv.Itoa(dim), binVec, "5")
	if err != nil {
		return "", false
	}
	arr, ok := res.([]interface{})
	if !ok || len(arr) < 2 {
		return "", false
	}
	bestKey := fmt.Sprintf("%v", arr[0])
	score, err := strconv.ParseFloat(fmt.Sprintf("%v", arr[1]), 64)
	if err != nil {
		return "", false
	}
	if score < float64(cnf.SemanticCache.Threshold) {
		return "", false
	}
	// bestKey = "semcache:<缓存问题原文>"，剥前缀取缓存问题
	cachedQ := strings.TrimPrefix(bestKey, semPrefix)
	if cachedQ == bestKey {
		return "", false // 前缀不匹配，忽略（防御）
	}
	// 读明文回答（array 引擎）
	answer, err := getClient().Do(ctx, "GET", qaKey(cachedQ))
	if err != nil {
		return "", false
	}
	ans, ok := answer.(string)
	if !ok {
		return "", false
	}
	rs, shared, err := rerank(ctx, query, cachedQ)
	if err != nil {
		return "", false
	}
	if rs < float64(cnf.SemanticCache.RerankThreshold) || !shared {
		return "", false
	}
	return ans, true
}

// CacheWrite: 嵌入 → SET qa:<问题> <回答>（明文）+ HSET semcache:<问题> <[dim][vec]>
func CacheWrite(ctx context.Context, query, answer string) error {
	cnf := config.GetConfig()
	if !cnf.SemanticCache.Enabled {
		return nil
	}
	vec, err := embedText(ctx, query)
	if err != nil {
		return err
	}
	if _, err := getClient().Do(ctx, "SET", qaKey(query), answer); err != nil {
		return err
	}
	_, err = getClient().Do(ctx, "HSET", semKey(query), encodeVec(vec))
	return err
}
```

- [ ] **Step 2: 编译验证**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-service && go build ./...`
Expected: 编译通过。若报 `strconv`/`strings` 未用，检查 import。

- [ ] **Step 3: 协议级验证命令契约（SET→array、HSET→hash、VSEARCH 命中）**

先起 kvstore（若未起）：
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
./kvstore/kvstore configs/kvstore-ai.conf &
sleep 1
```
逐条执行 semcache.go 实际发出的命令序列并核对：
```bash
# 1) CacheWrite 的第一条命令：SET qa:<问题> <回答>（走 array 引擎）
redis-cli -p 5160 SET "qa:go冒泡排序" "这是测试回答"
redis-cli -p 5160 --raw GET "qa:go冒泡排序"
#   预期：返回 "这是测试回答"（明文原样）
# 2) CacheWrite 的第二条命令：HSET semcache:<问题> <[dim][vec]>（走 hash 引擎）
python3 -c "import struct,sys;sys.stdout.buffer.write(struct.pack('<I',256)+struct.pack('<256f',*([0.1]*256)))" > /tmp/vec.bin
redis-cli -p 5160 -x HSET "semcache:go冒泡排序" < /tmp/vec.bin
#   预期：返回 1
# 3) CacheQuery 的查询命令：VSEARCH 应扫到该向量并返回 key+score
redis-cli -p 5160 VSEARCH 256 "$(python3 -c "import struct,sys;sys.stdout.buffer.write(struct.pack('<256f',*([0.1]*256)))")" 5
#   预期：返回数组含 "semcache:go冒泡排序" 与接近 1 的得分
# 4) CacheQuery 的取值命令：GET 走 array 读回明文
redis-cli -p 5160 --raw GET "qa:go冒泡排序"
#   预期：返回 "这是测试回答"
kill %1
```
Expected: 全部命令返回值符合预期——即 semcache.go 依赖的"SET/GET 在 array、HSET 在 hash、VSEARCH 扫 hash 命中"这一契约成立。（CacheWrite/CacheQuery 的端到端命中路径在 Task 9 全栈验证。）

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add ai-chat-service/chat-server/semcache/semcache.go
git commit -m "feat(semcache): Q-A 改明文 KV（key=原始问题、value=原始回答），向量索引独立存 semcache:"
```

---

### Task 3：proto 增加 `source` 字段并重新生成（service + backend 两份）

**Files:**
- Modify: `ai-chat-service/proto/chat.proto`
- Modify: `ai-chat-backend/services/ai-chat-service/proto/chat.proto`
- Regenerate: `ai-chat-service/proto/chat.pb.go`、`ai-chat-service/proto/chat_grpc.pb.go`
- Regenerate: `ai-chat-backend/services/ai-chat-service/proto/chat.pb.go`、`.../chat_grpc.pb.go`

**Interfaces:**
- Produces: `ChatCompletionStreamResponse.Source string`（proto3，json `source`），Task 4/5 使用。

- [ ] **Step 1: 两个 .proto 各加一个字段**

在 `ai-chat-service/proto/chat.proto` 与 `ai-chat-backend/services/ai-chat-service/proto/chat.proto` 的 `message ChatCompletionStreamResponse` 中，于 `repeated ChatCompletionStreamChoice choices = 5 [json_name = "choices"];` 之后追加（消息末尾）：

```proto
  string source = 6 [json_name = "source"];
```

（编号 6 紧接现有最大编号 5，避免冲突。）

- [ ] **Step 2: 重新生成 service 端 pb.go**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-service
protoc --go_out=. --go_opt=paths=source_relative --go-grpc_out=. --go-grpc_opt=paths=source_relative proto/chat.proto
```
Expected: `proto/chat.pb.go` 与 `proto/chat_grpc.pb.go` 被重写，`ChatCompletionStreamResponse` 含 `Source string` 字段。

- [ ] **Step 3: 重新生成 backend 端 pb.go**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-backend
protoc --go_out=. --go_opt=paths=source_relative --go-grpc_out=. --go-grpc_opt=paths=source_relative services/ai-chat-service/proto/chat.proto
```
Expected: `services/ai-chat-service/proto/chat.pb.go` 等重写，含 `Source`。

- [ ] **Step 4: 两端编译验证**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-service && go build ./...
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-backend && go build ./...
```
Expected: 均通过。

- [ ] **Step 5: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add ai-chat-service/proto ai-chat-backend/services/ai-chat-service/proto
git commit -m "feat(proto): ChatCompletionStreamResponse 增加 source 字段（llm/cache 标注）"
```

---

### Task 4：ai-chat-service 流式响应标注 source

**Files:**
- Modify: `ai-chat-service/chat-server/server/server.go`（缓存命中路径 ~168-180、LLM 流路径 ~199-230）

**Interfaces:**
- Consumes: `proto.ChatCompletionStreamResponse.Source`
- Produces: 所有流式响应都带 `Source`（`"cache"` 或 `"llm"`），Task 5 的 backend 据此分派计费。

- [ ] **Step 1: 加一个设置 source 的辅助函数**

在 `server.go` 中 `buildChatCompletionStreamResponse` 函数后追加：

```go
func (a *app) withSource(res *proto.ChatCompletionStreamResponse, source string) *proto.ChatCompletionStreamResponse {
	res.Source = source
	return res
}
```

- [ ] **Step 2: 缓存命中路径标注 "cache"**

把 `ChatCompletionStream` 中缓存命中分支（当前 `if cachedAns, hit := semcache.CacheQuery(...); hit {` 块内）的所有 `stream.Send(...)` 调用改为带 source 的版本：

```go
	resId := uuid.New().String()
	startRes := a.withSource(app.buildChatCompletionStreamResponse(resId, "", ""), "cache")
	endRes := a.withSource(app.buildChatCompletionStreamResponse(resId, "", "stop"), "cache")
	_ = stream.Send(startRes)
	resList := app.buildChatCompletionStreamResponseList(resId, cachedAns)
	for _, res := range resList {
		_ = stream.Send(a.withSource(res, "cache"))
	}
	_ = stream.Send(endRes)
	return nil
```

- [ ] **Step 3: LLM 路径标注 "llm"**

在 `ChatCompletionStream` 的 LLM 流循环中，`jsonpb.UnmarshalString(string(bytes), res)` 之后、`stream.Send(res)` 之前插入：

```go
	res.Source = "llm"
```

（即在 `res = &proto.ChatCompletionStreamResponse{}` 解出的每个 chunk 上标 llm。）

- [ ] **Step 4: 编译验证**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-service && go build ./...`
Expected: 通过。

- [ ] **Step 5: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add ai-chat-service/chat-server/server/server.go
git commit -m "feat(chat-service): 流式响应标注 source=llm/cache"
```

---

### Task 5：backend 免密自动登录 + ChatMessage source/tokens + 计费分派

**Files:**
- Modify: `ai-chat-backend/pkg/users/users.go`（phone → device_id）
- Modify: `ai-chat-backend/pkg/db/mysql/mysql.go`（`InitUsersTable` 改列）
- Modify: `ai-chat-backend/pkg/middlewares/auth.go`（ctx key 改 `device_id`）
- Modify: `ai-chat-backend/pkg/controllers/auth.go`（删 SendCode，Login 改 device_id）
- Modify: `ai-chat-backend/cmd/main.go`（删 SendCode 路由）
- Modify: `ai-chat-backend/pkg/controllers/chat.go`（ChatMessage 加字段、source 透传、EOF 计费分派 + 末包 tokens）

**Interfaces:**
- Consumes: `proto.ChatCompletionStreamResponse.Source`、`tokenizer.GetTokenCount`、`users.{GetByDeviceID,UpsertByDeviceID,DeductQuota}`
- Produces: HTTP 流每包 JSON 含 `source`；末包 JSON 含 `tokensUsed`/`tokensSaved`；`POST /v1/user/login` 接受 `{device_id}` 返回 `{access_token, data.quota}`。Task 8 前端依赖。

- [ ] **Step 1: users.go 改为 device_id**

用 device_id 重写 `ai-chat-backend/pkg/users/users.go`：

```go
package users

import (
	"database/sql"
	"errors"

	db "ai-chat-backend/pkg/db/mysql"
)

type User struct {
	ID       int64
	DeviceID string
	Quota    int
}

func GetByDeviceID(deviceID string) (*User, error) {
	u := &User{}
	err := db.GetDB().QueryRow(
		"SELECT id, device_id, quota FROM users WHERE device_id = ?", deviceID,
	).Scan(&u.ID, &u.DeviceID, &u.Quota)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return u, nil
}

// UpsertByDeviceID: 已存在返回现有行；不存在插入新用户（initQuota 初始额度）
func UpsertByDeviceID(deviceID string, initQuota int) (*User, error) {
	u, err := GetByDeviceID(deviceID)
	if err != nil {
		return nil, err
	}
	if u != nil {
		return u, nil
	}
	if _, err := db.GetDB().Exec(
		"INSERT INTO users (device_id, quota) VALUES (?, ?)", deviceID, initQuota,
	); err != nil {
		return nil, err
	}
	return GetByDeviceID(deviceID)
}

func DeductQuota(deviceID string, tokens int) error {
	if tokens <= 0 {
		return nil
	}
	_, err := db.GetDB().Exec(
		"UPDATE users SET quota = GREATEST(0, quota - ?) WHERE device_id = ?", tokens, deviceID,
	)
	return err
}
```

- [ ] **Step 2: mysql.go 建表列改 device_id**

把 `ai-chat-backend/pkg/db/mysql/mysql.go` 的 `InitUsersTable` 中建表语句改为：

```go
	_, err := GetDB().Exec(`CREATE TABLE IF NOT EXISTS users (
		id INT AUTO_INCREMENT PRIMARY KEY,
		device_id VARCHAR(64) NOT NULL UNIQUE,
		quota INT NOT NULL DEFAULT 100000,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	)`)
```

注意：若本地 MySQL 已有旧 `users` 表，需迁移。Run:
```bash
mysql -uroot -p123456 -e "USE ai_chat; ALTER TABLE users CHANGE COLUMN phone device_id VARCHAR(64) NOT NULL UNIQUE;"
```
（或在开发环境 `DROP TABLE users;` 后由后端自动重建。）

- [ ] **Step 3: 中间件 ctx key 改 device_id**

在 `ai-chat-backend/pkg/middlewares/auth.go` 中，把变量名 `phone` 改为 `deviceID`，`c.Set("phone", phone)` 改为 `c.Set("device_id", deviceID)`。其余逻辑不变。

- [ ] **Step 4: auth.go 删 SendCode、Login 改 device_id**

用以下内容整体替换 `ai-chat-backend/pkg/controllers/auth.go`：

```go
package controllers

import (
	"context"
	"errors"
	"time"

	"ai-chat-backend/pkg/config"
	kredis "ai-chat-backend/pkg/db/redis"
	"ai-chat-backend/pkg/users"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// Login: 免密自动登录。前端首访生成 device_id（UUID 存 localStorage），后端 upsert 用户并下发 session token。
func (chat *ChatService) Login(ctx *gin.Context) {
	var req struct {
		DeviceID string `json:"device_id"`
	}
	if err := ctx.BindJSON(&req); err != nil || req.DeviceID == "" {
		ctx.JSON(400, gin.H{"status": "Fail", "message": "device_id 不能为空", "data": nil})
		return
	}
	user, err := users.UpsertByDeviceID(req.DeviceID, config.GetConfig().Auth.InitQuota)
	if err != nil {
		chat.log.Error(err)
		ctx.JSON(500, gin.H{"status": "Fail", "message": "登录失败", "data": nil})
		return
	}
	token := uuid.New().String()
	if err := kredis.SetEx(context.Background(), "session:"+token, req.DeviceID, 7*24*time.Hour); err != nil {
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
	deviceID, err := kredis.Get(context.Background(), "session:"+token)
	if err != nil {
		if !errors.Is(err, redis.Nil) {
			chat.log.Error(err)
			ctx.JSON(500, gin.H{"status": "Fail", "message": "会话服务异常", "data": nil})
			return
		}
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI"}})
		return
	}
	user, err := users.GetByDeviceID(deviceID)
	if err != nil || user == nil {
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI"}})
		return
	}
	ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI", "phone": deviceID, "quota": user.Quota}})
}
```

（`data.phone` 字段保留以兼容前端，值即 device_id；`rand6`/`phoneRe`/`SendCode` 一并删除。）

- [ ] **Step 5: main.go 删 SendCode 路由**

删除 `ai-chat-backend/cmd/main.go` 中 `chat.POST("/v1/sms/send/code", chatService.SendCode)` 这一行。

- [ ] **Step 6: chat.go 加字段、透传 source、EOF 分派计费 + 末包 tokens**

(a) 在 `ai-chat-backend/pkg/controllers/chat.go` 的 `ChatMessage` 结构体末尾追加：

```go
	Source      string `json:"source"`
	TokensUsed  int    `json:"tokensUsed"`
	TokensSaved int    `json:"tokensSaved"`
```

(b) 在 `ChatProcess` 的流循环里，`if rsp.Id != "" { result.ID = rsp.Id }` 之后插入 source 透传：

```go
		if rsp.Source != "" {
			result.Source = rsp.Source
		}
```

(c) 把 `ChatProcess` 中 `if errors.Is(err, io.EOF)` 分支整体替换为（含 token 统计与末包发送，注意把统计逻辑从 `if Auth.Enabled` 中抽出，只把扣额度放在里面）：

```go
		if errors.Is(err, io.EOF) {
			// 流结束：统计本轮 tokens，按来源分派（缓存命中不计费、记节省；LLM 计费、记消耗）
			promptMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleUser, Content: payload.Prompt}
			respMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleAssistant, Content: result.Text}
			pt, err1 := tokenizer.GetTokenCount(promptMsg, chat.config.Chat.Model)
			rt, err2 := tokenizer.GetTokenCount(respMsg, chat.config.Chat.Model)
			if err1 == nil && err2 == nil {
				if result.Source == "cache" {
					result.TokensSaved = pt + rt
				} else {
					result.TokensUsed = pt + rt
				}
				if chat.config.Auth.Enabled {
					deviceID, _ := ctx.Get("device_id")
					if result.Source == "cache" {
						// 缓存命中：不扣额度
					} else if id, ok := deviceID.(string); ok {
						if err := users.DeductQuota(id, pt+rt); err != nil {
							chat.log.Error(err)
						}
					}
				}
			} else {
				chat.log.ErrorF("计费 token 统计失败: %v / %v", err1, err2)
			}
			// 末包：把 tokens 统计带给前端
			bts, err := json.Marshal(result)
			if err != nil {
				klog.Error(err)
				return
			}
			ctx.Writer.Write([]byte("\n"))
			if _, err := ctx.Writer.Write(bts); err != nil {
				klog.Error(err)
				return
			}
			ctx.Writer.Flush()
			return
		}
```

（同时把 `phone, _ := ctx.Get("phone")` 改为 `deviceID, _ := ctx.Get("device_id")`，以及 `ChatProcess` 开头额度校验处 `users.GetByPhone(phone.(string))` 改为 `users.GetByDeviceID(deviceID.(string))`。）

- [ ] **Step 7: 编译验证**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-backend && go build ./...`
Expected: 通过。

- [ ] **Step 8: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add ai-chat-backend
git commit -m "feat(backend): 免密自动登录（device_id），ChatMessage 透传 source/tokens，缓存命中不扣额度"
```

---

### Task 6：openai-api-proxy key 走环境变量 + base_url 指向 DeepSeek

**Files:**
- Modify: `openai-api-proxy/pkg/config/config.go`
- Modify: `openai-api-proxy/dev.config.yaml`

**Interfaces:**
- Produces: 代理启动时若 `DEEPSEEK_API_KEY` 存在则覆盖 `Chat.APIKeys`；`base_url` 默认 DeepSeek。Task 7 的 start.sh 注入该变量。

- [ ] **Step 1: config.go 读环境变量覆盖 key**

在 `openai-api-proxy/pkg/config/config.go` 的 `InitConfig` 中、`v.Unmarshal(conf)` 之后追加：

```go
	if k := os.Getenv("DEEPSEEK_API_KEY"); k != "" {
		conf.Chat.APIKeys = []string{k}
	}
```

并在 import 中加 `"os"`。

- [ ] **Step 2: dev.config.yaml 改 base_url**

把 `openai-api-proxy/dev.config.yaml` 的 `chat:` 段改为：

```yaml
chat:
  # DeepSeek key 通过环境变量 DEEPSEEK_API_KEY 注入；此处为占位，防止 api_keys 为空导致 rand 越界
  api_keys:
  - "sk-placeholder-please-set-DEEPSEEK_API_KEY"
  # DeepSeek OpenAI 兼容接口
  base_url: "https://api.deepseek.com/v1"
```

- [ ] **Step 3: 编译验证**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/openai-api-proxy && go build ./...`
Expected: 通过。

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add openai-api-proxy
git commit -m "feat(proxy): DeepSeek key 从 DEEPSEEK_API_KEY 环境变量读取，base_url 指向 api.deepseek.com"
```

---

### Task 7：start.sh 注入 DEEPSEEK_API_KEY 并检查

**Files:**
- Modify: `start.sh`

**Interfaces:**
- Consumes: 环境变量 `DEEPSEEK_API_KEY`
- Produces: proxy 服务启动前若未设置 key 则给出明确提示（仍可继续以 mock 方式运行）。

- [ ] **Step 1: 加 key 检查**

在 `start.sh` 中 `echo "== 启动服务 =="` 之前插入：

```bash
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "⚠ 未设置 DEEPSEEK_API_KEY：openai-api-proxy 将使用占位 key，DeepSeek 调用会失败。"
  echo "  使用真实 key：DEEPSEEK_API_KEY=sk-xxx ./start.sh"
  echo "  离线/无 key 调试：把 openai-api-proxy/dev.config.yaml 的 base_url 改回 http://localhost:8083/v1（mock）"
fi
```

（proxy 子进程会继承当前 shell 环境变量，无需额外 export；`DEEPSEEK_API_KEY=sk-xxx ./start.sh` 即注入。）

- [ ] **Step 2: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add start.sh
git commit -m "chore(start.sh): 提示设置 DEEPSEEK_API_KEY"
```

---

### Task 8：前端——去验证码、自动登录、来源徽标、tokens 统计条

**Files:**
- Modify: `ai-chat-web/src/typings/chat.d.ts`
- Modify: `ai-chat-web/src/api/index.ts`
- Modify: `ai-chat-web/src/views/chat/layout/Permission.vue`
- Modify: `ai-chat-web/src/views/chat/index.vue`
- Modify: `ai-chat-web/src/views/chat/components/Message/index.vue`

**Interfaces:**
- Consumes: HTTP 流每包 `{source}`、末包 `{tokensUsed, tokensSaved}`；`POST /v1/user/login` 接受 `{device_id}`
- Produces: 首访自动登录；每条 AI 回答显示「公有大模型」/「缓存命中」徽标；页面显示会话累计「总消耗」「节省」tokens。

- [ ] **Step 1: 类型扩展**

`ai-chat-web/src/typings/chat.d.ts` 的 `Chat.Chat` 接口追加：

```ts
		source?: string
		tokensUsed?: number
		tokensSaved?: number
```

- [ ] **Step 2: api/index.ts 去掉验证码、加 device 登录**

删除 `fetchCode` 与 `login(phone, code)`，替换为：

```ts
export function fetchDeviceLogin<T = any>() {
  const deviceId = localStorage.getItem('device_id') || `${Date.now()}-${Math.random().toString(36).slice(2)}`
  localStorage.setItem('device_id', deviceId)
  return post<T>({
    url: '/v1/user/login',
    data: { device_id: deviceId },
  })
}
```

- [ ] **Step 3: Permission.vue 改为自动登录**

删除 `Permission.vue` 中验证码相关模板（`NModal` 弹窗、手机号/验证码输入、`sendCode`、`handleLogin` 内验证码逻辑）与 `fetchCode`/`login` 导入。将 `<script setup>` 改为仅在挂载时确保有 token：

```ts
import { onMounted } from 'vue'
import { useAuthStore } from '@/store'
import { fetchDeviceLogin } from '@/api'

const authStore = useAuthStore()

onMounted(async () => {
  if (!authStore.token) {
    try {
      const data = await fetchDeviceLogin()
      authStore.setToken(data.access_token)
      window.location.reload()
    } catch (e) {
      // 自动登录失败：保持未登录态
      console.error('auto login failed', e)
    }
  }
})
```

模板里删除 `Permission` 弹窗内容（`Layout.vue` 中 `needPermission` 条件此时不会触发，可保留）。

- [ ] **Step 4: chat/index.vue 读取 source/tokens 并累计**

(a) 在 `chat/index.vue` 的流处理 `onDownloadProgress` 中，`updateChat(...)` 的 data 对象里追加：

```ts
                source: data.source,
                tokensUsed: data.tokensUsed,
                tokensSaved: data.tokensSaved,
```

(b) 在 `<script setup>` 内新增会话级累计（从消息列表推导，避免重复计数）：

```ts
const tokenStats = computed(() => {
  const consumed = dataSources.value.reduce((s, m) => s + (m.tokensUsed ?? 0), 0)
  const saved = dataSources.value.reduce((s, m) => s + (m.tokensSaved ?? 0), 0)
  return { consumed, saved }
})
```

(c) 在模板输入框上方（找到 `prompt` 输入区前）加统计条：

```html
    <div v-if="tokenStats.consumed > 0 || tokenStats.saved > 0" class="text-xs text-neutral-400 px-4 py-1">
      总消耗 {{ tokenStats.consumed }} tokens ｜ 节省 {{ tokenStats.saved }} tokens
    </div>
```

- [ ] **Step 5: Message 组件显示来源徽标**

(a) `ai-chat-web/src/views/chat/components/Message/index.vue` 的 `Props` 接口追加 `source?: string`。

(b) 在模板中 `TextComponent` 之后（`!inversion` 分支内）追加徽标：

```html
        <span
          v-if="!inversion && source"
          class="ml-1 text-[10px] px-1.5 py-0.5 rounded border border-neutral-300 text-neutral-400"
        >
          {{ source === 'cache' ? '缓存命中' : '公有大模型' }}
        </span>
```

(c) 在 `chat/index.vue` 渲染消息处（`<Message ...>`）传入 `:source="item.source"`。

- [ ] **Step 6: 前端编译验证**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat-web
pnpm build-only
```
Expected: 构建成功（只跑 `build-only` 跳过 type-check 可先确认；再跑 `pnpm build` 检查类型，必要时补齐）。

- [ ] **Step 7: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add ai-chat-web/src
git commit -m "feat(web): 去验证码改免密自动登录，回答标注来源，显示会话 tokens 统计"
```

---

# Phase B：端到端验证

### Task 9：全栈启动并验证 7 项功能

**Files:** 无（验证）

- [ ] **Step 1: 确认 MySQL users 表已迁移**

Run: `mysql -uroot -p123456 -e "USE ai_chat; SHOW COLUMNS FROM users;"`
Expected: 存在 `device_id` 列（无 `phone`）。若还是 phone，执行 Task 5 Step 2 的 ALTER。

- [ ] **Step 2: 带 key 启动全栈**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore && DEEPSEEK_API_KEY=sk-你的真实key ./start.sh`
Expected: 8 服务全部 listening（`http://localhost:7080`），kvstore 使用 Task 1 新 record 格式。

- [ ] **Step 3: 验证免密自动登录（无验证码）**

Run:
```bash
curl -s http://localhost:7080/v1/user/login -H 'Content-Type: application/json' -d '{"device_id":"test-device-001"}'
```
Expected: 返回 `access_token` 与 `quota`；重复调用返回同一用户、额度不变。
再 Run: `curl -s http://localhost:7080/session -H "Authorization: <token>"`
Expected: `auth:true`、quota 存在。

- [ ] **Step 4: 验证明文 KV 存储（无 hash）**

浏览器或 curl 向 `/api/chat-process` 发一条问题（如"写一个 go 的冒泡排序"），等回答完成。然后 Run:

```bash
redis-cli -p 5160 --raw GET "qa:写一个 go 的冒泡排序"
redis-cli -p 5160 KEYS 'semcache:*'   # 若该命令存在
```
Expected: `GET` 返回回答原文（明文字符串）；没有 `qa:` 的 hash 形态 key；VSEARCH 仍能命中近似问题（换个措辞再问，来源标"缓存命中"）。

- [ ] **Step 5: 验证真实大模型输出**

Step 4 的回答应为真实可用的代码/文本（非 mock 的 canned 随机句）。若仍是 mock 内容，检查 `openai-api-proxy/runtime/app.log` 与 `DEEPSEEK_API_KEY` 是否注入。

- [ ] **Step 6: 验证来源标注与 tokens 统计**

- 第一遍问"写一个 go 的冒泡排序"：回答下徽标为「公有大模型」，统计条"总消耗"增加、"节省"为 0。
- 换近似措辞（如"用 go 写冒泡排序"）再问：徽标为「缓存命中」，统计条"总消耗"不变、"节省"增加。
- 同措辞直接复问：命中缓存，不扣额度（Session 接口 quota 不变）。

- [ ] **Step 7: 关闭全栈**

Run: `cd /home/pp/Desktop/ls_study/proj/9.1-kvstore && ./stop.sh`

---

# Phase C：仓库拆分

### Task 10：创建 pocket-kv 独立仓库并推送

**Files:**
- Create: `/home/pp/Desktop/ls_study/proj/kvstore`（新 git 仓库）

**Interfaces:**
- Produces: `git@github.com:robotlover-1/pocket-kv.git`，只含 kvstore 存储相关文件；Task 11 的 ai-chat 以 submodule 指向它。

- [ ] **Step 1: 准备目录并初始化**

Run:
```bash
mkdir -p /home/pp/Desktop/ls_study/proj/kvstore && cd /home/pp/Desktop/ls_study/proj/kvstore
git init -b main
cp -r /home/pp/Desktop/ls_study/proj/9.1-kvstore/kvstore ./kvstore
rm -rf ./kvstore/.git ./kvstore/NtyCo
cp /home/pp/Desktop/ls_study/proj/9.1-kvstore/Makefile ./Makefile
cp /home/pp/Desktop/ls_study/proj/9.1-kvstore/lib.sh ./lib.sh
cp -r /home/pp/Desktop/ls_study/proj/9.1-kvstore/configs ./configs
cp -r /home/pp/Desktop/ls_study/proj/9.1-kvstore/bin ./bin
cp /home/pp/Desktop/ls_study/proj/9.1-kvstore/README.md ./README.md
cp /home/pp/Desktop/ls_study/proj/9.1-kvstore/LICENSE ./LICENSE
cp /home/pp/Desktop/ls_study/proj/9.1-kvstore/.gitignore ./.gitignore
```
（`docs/` 整体属于 ai-chat，不进 kvstore 仓。）

- [ ] **Step 2: 加 NtyCo submodule 并提交**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/kvstore
git submodule add https://github.com/wangbojing/NtyCo.git kvstore/NtyCo
git add -A
git commit -m "init: kvstore 独立仓库（含 NtyCo submodule）"
git remote add origin git@github.com:robotlover-1/pocket-kv.git
```
Expected: 提交成功，`kvstore/NtyCo` 为 submodule gitlink。

- [ ] **Step 3: 推送（github 空仓库已由用户创建：robotlover-1/pocket-kv）**

Run: `cd /home/pp/Desktop/ls_study/proj/kvstore && git push -u origin main`
Expected: 推送成功。

---

### Task 11：创建 ECHO-CHAT 仓库（kvstore 作 submodule）并推送

**Files:**
- Create: `/home/pp/Desktop/ls_study/proj/ai-chat`（新 git 仓库）

**Interfaces:**
- Consumes: Task 10 的 `pocket-kv` 仓库
- Produces: `git@github.com:robotlover-1/ECHO-CHAT.git`，含全部 ai-chat 组件 + `kvstore` submodule。

- [ ] **Step 1: 准备目录并复制 ai-chat 组件**

Run:
```bash
mkdir -p /home/pp/Desktop/ls_study/proj/ai-chat && cd /home/pp/Desktop/ls_study/proj/ai-chat
git init -b main
SRC=/home/pp/Desktop/ls_study/proj/9.1-kvstore
for d in ai-chat-backend ai-chat-service ai-chat-stack ai-chat-web openai-api-proxy mock-openai-api keywords-filter tokenizer monitoring; do
  cp -r "$SRC/$d" "./$d"
  rm -rf "./$d/.git" "./$d/node_modules" "./$d/dist" 2>/dev/null
done
# 删除 backend/www（旧的编译产物），否则 start.sh 认为前端已构建、不会用新源码重建
rm -rf ./ai-chat-backend/www
cp "$SRC/start.sh" ./start.sh
cp "$SRC/stop.sh" ./stop.sh
cp "$SRC/lib.sh" ./lib.sh
cp "$SRC/Makefile" ./Makefile
cp -r "$SRC/configs" ./configs
cp -r "$SRC/docs" ./docs
cp "$SRC/LICENSE" ./LICENSE
cp "$SRC/.gitignore" ./.gitignore
```
（`node_modules`/`dist` 由 `.gitignore` 排除，start.sh 会自动重建；`docs/` 属 ai-chat。）

- [ ] **Step 2: 加 kvstore submodule 并提交**

Run:
```bash
cd /home/pp/Desktop/ls_study/proj/ai-chat
git submodule add git@github.com:robotlover-1/pocket-kv.git kvstore
git add -A
git commit -m "init: ai-chat 独立仓库（kvstore 为 submodule）"
git remote add origin git@github.com:robotlover-1/ECHO-CHAT.git
```
Expected: `kvstore/` 为 submodule gitlink，`.gitmodules` 指向 `pocket-kv`。

- [ ] **Step 3: 推送（github 空仓库已由用户创建：robotlover-1/ECHO-CHAT）**

Run: `cd /home/pp/Desktop/ls_study/proj/ai-chat && git push -u origin main`
Expected: 推送成功。

---

### Task 12：新结构 clone 验证 + 文档更新

**Files:**
- Modify: `ai-chat/README.md`（或新增）说明仓库结构与启动方式

**Interfaces:**
- Consumes: Task 10/11 产出的两个仓库

- [ ] **Step 1: 干净 clone 验证 ai-chat**

Run:
```bash
rm -rf /tmp/ai-chat-clone && git clone --recurse-submodules git@github.com:robotlover-1/ECHO-CHAT.git /tmp/ai-chat-clone
cd /tmp/ai-chat-clone
git submodule update --init --recursive   # 拉下 kvstore（含 NtyCo）
make kvstore                              # 从 submodule 编译 kvstore
```
Expected: kvstore 子模块与 NtyCo 全部检出，`make kvstore` 编译成功。

- [ ] **Step 2: 新结构启动冒烟**

Run: `cd /tmp/ai-chat-clone && DEEPSEEK_API_KEY=sk-你的真实key ./start.sh`
Expected: 8 服务启动，`redis-cli -p 5160 GET "qa:写一个 go 的冒泡排序"` 能取到之前写入的明文回答（若 dump 未保留则重新问一次验证写入）。

- [ ] **Step 3: 更新 README 说明仓库结构**

在 `/home/pp/Desktop/ls_study/proj/ai-chat/README.md` 顶部加一段：

```markdown
# ai-chat（ai 助手）

独立仓库，kvstore 以 submodule 引入（`kvstore/`），用法与 kvstore 自身引用 NtyCo 一致。

- 启动：`DEEPSEEK_API_KEY=sk-xxx ./start.sh`（key 走环境变量，勿提交 git）
- 存储：Q-A 明文 KV 在 kvstore（`qa:<原始问题>` → `<原始回答>`），语义检索向量索引 `semcache:<原始问题>`。
- 免密登录：首次访问自动以 device_id 注册并分配额度，无验证码。
- 回答来源：每条回答标注「公有大模型」/「缓存命中」；页面按会话累计 tokens 总消耗与节省。
```

提交：`cd /home/pp/Desktop/ls_study/proj/ai-chat && git add README.md && git commit -m "docs: 仓库结构与启动说明" && git push`

- [ ] **Step 4: 收尾清理**

- 删除临时文件：`rm -rf /tmp/ai-chat-clone /tmp/vec.bin`。
- 9.1-kvstore 保留不动（作为历史/备份）。若后续主开发切换到两个新仓库，告知用户路径。

---

## 完成标准（与设计文档 §10 对齐）

1. 两个新仓库独立 clone 正常，ai-chat 的 kvstore submodule（含 NtyCo）检出并编译。
2. 免密：清 localStorage 打开页面无弹窗，自动登录，额度 init_quota，连续提问扣减正常。
3. 公有大模型：`DEEPSEEK_API_KEY=sk-xxx ./start.sh` 后得到真实代码/回答（非 mock canned 文本）。
4. 明文 KV：`redis-cli -p 5160 GET qa:<问题>` 返回回答原文；无 hash key；VSEARCH 命中近似问题。
5. 来源标注：缓存命中显示「缓存命中」，未命中显示「公有大模型」。
6. tokens 统计：同一问题问两遍，第一遍总消耗增、节省 0；第二遍消耗不变、节省 >0。
7. git 内无任何密钥。
