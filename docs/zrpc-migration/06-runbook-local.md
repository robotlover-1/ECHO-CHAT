# zrpc 迁移 · 本地真栈拉起 Runbook（tmp/t1/ECHO-CHAT, 分支 zrpc-migration）

日期：2026-09-04。目的：起一套**离线可用**的栈，用来做 gRPC vs zrpc 双栈对比与收尾。
前端 UI 不需要（用 curl 打 backend 即可）。

## 端口速查

| 服务 | 端口 | 本次是否必需 |
|---|---|---|
| MySQL（已在本机 3306 运行） | 3306 | 必需（chat_records） |
| kvstore（自研 redis，当 Redis 用） | 5160 | 必需（上下文缓存 + backend 会话） |
| tokenizer | 3002 | 必需（token 计费） |
| mock-openai-api（假 DeepSeek） | 8083 | 必需（离线 LLM） |
| keywords-filter 双栈 | 50053/54 gRPC · 50063/64 zrpc | 必需 |
| ai-chat-service 双栈 | 50055 gRPC · 50065 zrpc | 必需 |
| ai-chat-backend | 7080 | 必需 |
| semantic | 3003 | 可选（缺失→优雅 miss，聊天不受影响） |
| openai-api-proxy | 8084 | 不需要（离线直接用 mock） |

## 0) 一次准备（在仓库根 `.../tmp/t1/ECHO-CHAT`）

```bash
cd .../tmp/t1/ECHO-CHAT
# 建库/表（幂等失败没关系，已存在就跳过）
mysql -h127.0.0.1 -uroot -p123456 < docs/sql/create_db.sql
# 编译 C lib + 三个服务二进制（root Makefile 已接好）
make build
# 让 chat-service 离线直连 mock（不经过 proxy/DeepSeek），只改本机测试配置：
#   ai-chat-service/dev.config.yaml  chat.base_url: "http://127.0.0.1:8083/v1"
# 让 backend curl 免鉴权（仅本机测试）：
#   ai-chat-backend/dev.config.yaml  auth.enabled: false
```

## 1) 拉起必需服务（推荐手动子集，避免触发前端 pnpm/模型下载）

```bash
# kvstore(5160)：当作 redis 用
./kvstore/kvstore configs/kvstore-ai.conf &   # 日志在 runtime/logs/kvstore.log
sleep 1
# tokenizer(3002)
( cd tokenizer && nohup nuxt --port 3002 --module tokenizer.py --workers 2 \
    > ../runtime/logs/tokenizer.log 2>&1 & echo $! > ../runtime/pids/tokenizer.pid )
# mock(8083)
( cd mock-openai-api && nohup ../bin/mock-openai-api --config=dev.config.yaml \
    > ../runtime/logs/mock.log 2>&1 & echo $! > ../runtime/pids/mock.pid )
# keywords-filter 双栈 50053(gRPC)+50063(zrpc) 与 50054+50064
( cd keywords-filter && nohup ../bin/keywords-filter --config=dev.config.yaml --dict=dict.txt \
    > ../runtime/logs/sensitive.log 2>&1 & echo $! > ../runtime/pids/sensitive.pid )
( cd keywords-filter && nohup ../bin/keywords-filter --config=dev.kw.config.yaml --dict=keyword-dict.txt \
    > ../runtime/logs/keyword.log 2>&1 & echo $! > ../runtime/pids/keyword.pid )
# ai-chat-service 双栈 50055(gRPC)+50065(zrpc)
( cd ai-chat-service && nohup ../bin/ai-chat-service --config=dev.config.yaml \
    > ../runtime/logs/service.log 2>&1 & echo $! > ../runtime/pids/service.pid )
# ai-chat-backend 7080
( cd ai-chat-backend && nohup ../bin/ai-chat-backend --config=dev.config.yaml \
    > ../runtime/logs/backend.log 2>&1 & echo $! > ../runtime/pids/backend.pid )
```

> 嫌麻烦想一键：`./start.sh` 会全量起（含 semantic/前端/代理），但可能触发前端 pnpm 编译或模型下载；
> 上面的手动子集适合只验证 zrpc。停止：`kill $(cat runtime/pids/*.pid)` 逐个，或 `./stop.sh`。

## 2) 验证都起来了

```bash
ss -tln | grep -E ':(5160|3002|8083|50053|50054|50063|50064|50055|50065|7080)\b'
# 期望全部 LISTEN
```

## 3) 双栈对比（核心目的）

默认全链路是 gRPC（观察期）。测 zrpc 时只翻转要验证的那段：

- 验证 **backend→chat-service 走 zrpc**：
  `ai-chat-backend/dev.config.yaml` → `dependOn.ai-chat-service.transport: "zrpc"`（已指向 50065）。
- 验证 **chat-service→filter 走 zrpc**：
  `ai-chat-service/dev.config.yaml` → `dependOn.sensitive.transport: "zrpc"` + 地址 50063、
  `dependOn.keywords.transport: "zrpc"` + 地址 50064。
- 每改一处 transport 都要重启对应服务（杀掉 pid 后按上面命令重启）。

打请求（后端 /api/chat-process 是流式，逐行 JSON；`source` 与 `tokensUsed/tokensSaved` 在末包）：

```bash
curl -N -s http://127.0.0.1:7080/api/chat-process \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"介绍一下你自己","options":{"parentMessageId":""}}' | tee /tmp/out.txt
grep -o '"source":"[a-z]*"' /tmp/out.txt | sort -u     # 期望含 llm；命中语义缓存时是 cache
```

同一句分别用 grpc（transport=grpc）与 zrpc 各跑一次，比对逐行文本/`source`/末包 token 是否一致；
浏览器刷新/断连测试：请求中途 Ctrl-C，观察 `runtime/logs/service.log` 是否出现流 handler 取消（zrpc 取消链）。

## 3.5 语义缓存命中（source=cache）验证要点

语义缓存（自建 semantic 向量库 + kvstore 索引）命中依赖 **query 能抽到 subject**。
semcache 设计（`ai-chat-service/chat-server/semcache/semcache.go`）：`/embed` 返回的
`subject`/`subject_id` 皆空 → **不查全局缓存**（通用 how-to 句直接 miss，属准入规则非 bug）。

- ✅ 能命中的例句（代码/算法类，能抽到本体）：`红黑树和AVL树的区别是什么` —— 第二次同句
  返回 `source=cache`、app.log 出现 `semcache_vector_hit subject=avl_tree top=1.000 candidates=12`。
- ❌ 我最初误判用句 `缓存机制测试句子A` —— 通用句无 subject，永远 miss（与 zrpc/transport 无关）。

定位小抄：chat-service 自己的 logrus 写到 `ai-chat-service/runtime/logs/app.log`（不是 nohup 的
`runtime/logs/service.log`）。缓存决策行前缀 `semcache_`（query_encodes / vector_hit /
fingerprint_hit / vector_hit_no_answer 等）都在这。验证前确保 semantic(3003) 先起、模型在
（`semantic/models/e5s-v1/model.onnx`），并 `semantic_cache.enabled: true`。

## 4) 常见坑

- backend 启动连 redis/mysql 失败 → 确认 kvstore(5160) 已起、3306 已跑。
- chat-service redis 若报密码错误 → 看 `configs/kvstore-ai.conf` 是否 requirepass，把对应 dev 配置 pwd 填上。
- mock 不吐流 → `tail -f runtime/logs/mock.log`；chat-service 的 `base_url` 是否指到 8083。
- 请求返回 401/402 → 上文的 `auth.enabled:false` 没生效或没重启 backend。
- zrpc 服务未监听 50063/64/65 → 说明对应二进制是旧构建，重跑 `make build`。
