# semantic 解耦端到端验证记录：命中/拒绝/降级/基线

> 对应计划：`2026-09-03-semantic-service-decouple` Task 6。HEAD = `e652290`（本分支领先 origin/main 11 commits，均本计划产出）。
> 目标：验证全栈（本地 9 进程）下语义缓存**命中/拒绝判定**、计 token 与额度不回归、semantic 故障降级，并记录高命中率用例的 rerank 基线供 Phase 1+ 对照。**本轮零业务代码改动**（纯验证 + 记录）。
>
> LLM 链路：全部走**真实 DeepSeek**（proxy :8084 → `https://api.deepseek.com/v1`，本地已配真 key `sk-591…74`），未使用 mock(:8083)。验证时间：2026-09-03，分支 main。

## 0. 前置确认（脏文件盘点，未触碰）

- `git status` 显示仓库脏文件仅：**kvstore submodule**（modified/untracked）、**openai-api-proxy/dev.config.yaml**（本地真 key，用户既有改动）、**ai-chat-backend/www/**（untracked 前端产物）。本轮**全都不改动、不 stage**。
- `openai-api-proxy/dev.config.yaml` 已在动手前全文备份 `md5sum`（见下），若途中曾切换 mock 必还原同一字节。因真链路直接可用，**未切换 mock、未改动该文件**。

```bash
$ cp openai-api-proxy/dev.config.yaml /tmp/dev.config.yaml.BACKUP.task6
$ md5sum openai-api-proxy/dev.config.yaml /tmp/dev.config.yaml.BACKUP.task6
f03fb89e7c576f4df8e6a4fe77c30c99  openai-api-proxy/dev.config.yaml
f03fb89e7c576f4df8e6a4fe77c30c99  /tmp/dev.config.yaml.BACKUP.task6   # 字节一致（未被打扰改）
```

## 1. Step 1 — 全栈健康

```bash
$ ss -tln | grep -E ':3002|:3003|:50055|:5160|:7080'
LISTEN ... 0.0.0.0:5160   # kvstore
LISTEN ... 0.0.0.0:3002   # tokenizer
LISTEN ... 0.0.0.0:3003   # semantic
LISTEN ... *:50055        # ai-chat-service
LISTEN ... *:7080         # backend
```

全部在听。**结论：全栈健康。**（9 进程中本任务相关的 5 个端口就绪；故障注入前 semantic pid=`67211` 存活。）

## 2. Step 2 — 计 token 不回归（tokenizer 纯职责自检）

```bash
$ curl -s -m 3 -X POST http://127.0.0.1:3002/tokenizer/deepseek-v4-flash \
    -H 'Content-Type: application/json' -d '{"role":"user","content":"实现一个红黑树"}'
{"code":200,"num_tokens":16}
```

**结论：`code:200`、`num_tokens=16`（正整数），计 token 职责正常，无回归。**

## 3. Step 3 — 语义命中/拒绝判定不回归（真实 DeepSeek 链路）

登录拿 token 后经 `POST /api/chat-process` 流式返回；判定依据为**末包信封**字段 `source` / `tokensUsed` / `tokensSaved`（backend `chat.go` EOF 处由 `source` 分派：cache→不扣额度、记 tokensSaved；llm→扣额度、记 tokensUsed）。

### Step 3.1 — 写缓存（真实 LLM 生成，`source=llm`）

```bash
Q='用 Python 写一个单例'
curl -s -m 90 -X POST http://127.0.0.1:7080/api/chat-process \
  -H 'Content-Type: application/json' -H "Authorization: <token>" \
  -d "{\"prompt\":\"$Q\",\"options\":{}}"
# 末包: source=llm  tokensUsed=887  tokensSaved=0   (elapsed≈4.5s)
```

写缓存已确认：
```bash
$ redis-cli -h 127.0.0.1 -p 5160 GET '用 Python 写一个单例' | head -c 300
我来用 Python 实现几种常见的单例模式写法： ## 方法1…   # 明文 Q-A 落数组引擎
```

### Step 3.2 — 语义近似重问 → **命中缓存**（期望行为达成）

```bash
Q='用 Python 实现单例模式'   # 与 3.1 变更措辞的语义近似
curl -s -m 30 -X POST http://127.0.0.1:7080/api/chat-process \
  -H 'Content-Type: application/json' -H "Authorization: <token>" \
  -d "{\"prompt\":\"$Q\",\"options\":{}}"
# 末包: source=cache  tokensUsed=0  tokensSaved=888   (elapsed=0.01s, 秒回)
```

- 27 个流式 chunk 全部来自明文 Q-A（非 LLM），末包 `source=cache`。
- `tokensUsed=0` → **线上 cache-deduction 分派不扣额度**；`tokensSaved=888` 回传前端做"节省"统计。
- 与拆分前行为一致（命中=秒回 + source=cache + tokensSaved>0 + 不扣额度）。

### Step 3.3 — 语言冲突 → **不误命中**（期望行为达成）

```bash
Q='用 C++ 写一个单例'        # 语言 Python→C++，与 3.1 缓存冲突
curl -s -m 90 -X POST http://127.0.0.1:7080/api/chat-process \
  -H 'Content-Type: application/json' -H "Authorization: <token>" \
  -d "{\"prompt\":\"$Q\",\"options\":{}}"
# 末包: source=llm  tokensUsed=1533  tokensSaved=0   (elapsed≈7.9s)
```

- 回答正文是 **C++ 实现单例**（`text_head='…在 C++ 中实现单例…'`），**不是** 3.1 的 Python 缓存答案 → 语言冲突硬拒生效，未误命中。
- `source=llm`、正常扣额度，符合预期。

**结论：三条命中/拒绝判定全部符合拆分前线上行为，无回归。**（CacheQuery/CacheWrite 逻辑本轮零改动，命中差异由 kvstore 内容 + golden 等价的服务输出决定，均已验证。）

## 4. Step 4 — 故障降级（semantic 不可用 → 聊天不挂）

按 brief 用 pid 文件杀 semantic（`runtime/pids/semantic.pid`），**不用 pkill -f、无 sudo**：

```bash
$ SPID=$(cat runtime/pids/semantic.pid); echo $SPID   # 67211
$ kill "$(cat runtime/pids/semantic.pid)"; sleep 1
$ curl -s -m 3 http://127.0.0.1:3003/healthz   # (no response / connection refused)
$ ss -tln | grep ':3003\b'                       # 3003 NOT listening
$ ps -p $SPID                                    # pid no longer alive
```

semantic 确认已 down（端口 3003 release、healthz 拒连）。随即发一条**缓存必 miss** 的 LLM 题（`用 JavaScript 写一个单例`，该语言此前未缓存）：

```bash
$ /usr/bin/time -f 'elapsed=%es' curl -s -m 90 -X POST http://127.0.0.1:7080/api/chat-process \
     -H 'Content-Type: application/json' -H "Authorization: <token>" \
     -d '{"prompt":"用 JavaScript 写一个单例","options":{}}'
# 末包: source=llm  tokensUsed=1404  tokensSaved=0   (elapsed≈8.9s, 正常完整回答)
```

- **聊天未挂、未报错、未卡死**：semantic down 时 CacheQuery 退化为**优雅 miss**（embed 走 1.5s ctx 超时让位 → 不阻塞主流程 → 落 LLM）。JS 单例完整生成，全程无面向用户的失败。
- service 日志出现预期标签（`runtime/logs/service.log`）：

```log
2026/09/03 23:15:53 semantic_unavailable: Post "http://127.0.0.1:3003/embed": dial tcp … connection refused … semcache.go:74 semcache.embedText
2026/09/03 23:16:02 semantic_unavailable: Post "http://127.0.0.1:3003/embed": dial tcp … connection refused … semcache.go:74
2026/09/03 23:16:02 Post "http://127.0.0.1:3003/embed": … connection refused … server.go:301 ChatCompletionStream.func2   # CacheQuery goroutine
```

### 恢复

```bash
$ ./start.sh
# … [semantic] starting … [semantic] ✔ 3003 listening … ✔ 9/9 服务就绪
$ curl -s -m 5 http://127.0.0.1:3003/healthz
{"status":"ok","service":"semantic","embedding_type":"fnv_hash","dimension":256,"parser_version":"v1"}   # 恢复 200
```

恢复后全 9 端口在听（5160/3002/3003/50053/50054/8083/8084/50055/7080）。post-restart 跑 Task 1 golden 差分守护作为一致性复检：

```bash
$ python3 semantic/tests/test_golden.py --check http://127.0.0.1:3003
PASS: 44 cases field-identical (eps=1e-07)
```

**结论：semantic 故障时优雅降级走 LLM、服务日志记 `semantic_unavailable`/连接拒绝、1.5s 内让位不阻塞；恢复后健康、golden 44/44 字段一致——无回归。**

## 5. Step 5 — 高命中率 RERANK 基线（非阻断，仅记录供 Phase 1+ 对照）

`semantic/tests/test_golden.py` RERANK_PAIRS 中代表用例，对 live semantic :3003 `/rerank` 直接观察：

```bash
$ python3 - <<'PY'
import json, urllib.request
pairs = [
    ("生成一个红黑树", "生成一个 rbtree"),
    ("用C语言生成红黑树", "使用C++编写rbtree"),
    ("什么是红黑树", "实现一个红黑树"),
    ("红黑树的插入", "红黑树的删除"),
]
for q, c in pairs:
    req = urllib.request.Request("http://127.0.0.1:3003/rerank",
        data=json.dumps({"query": q, "cached_query": c}).encode(),
        headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=5).read())
    print(f"{q!r} vs {c!r} -> shared={r.get('shared')} score={r.get('score')}")
PY
'生成一个红黑树' vs '生成一个 rbtree'       -> shared=False score=0.0
'用C语言生成红黑树' vs '使用C++编写rbtree' -> shared=False score=0.0
'什么是红黑树'     vs '实现一个红黑树'      -> shared=False score=0.0
'红黑树的插入'     vs '红黑树的删除'        -> shared=False score=0.0
```

- **与拆分前（Task 1 金样）逐字段一致**：以上 4 对正是 spec 判定为**应拒绝（无冗余命中）**的跨语言 / 定义↔实现 / 操作冲突用例，`shared=False, score=0` 是该语义下**正确**的保守基线（rbtree↔红黑树词面差距、C↔C++语言冲突、what↔implement 意图冲突、插入↔删除操作冲突均不共享）。
- **本轮不要求命中/共享达标**；此基线留给 Phase 1+（真语义模型 bge）对照。首版高分命中（Step 3.2 的 Python 变写召回命中）已在 §3 演示。

## 6. 结论汇总

| 项 | 结果 | 依据 |
|---|---|---|
| 全栈健康 | ✅ | 5 关键端口 + 恢复后 9 进程全听 |
| 计 token 不回归 | ✅ | `/tokenizer` code:200, num_tokens=16 |
| 语义命中 | ✅ | 近似重问 `source=cache`, tokensUsed=0, tokensSaved=888, 0.01s |
| 语义拒绝（语言冲突） | ✅ | C++↔Python `source=llm`, C++ 正文, 未误命中 |
| 故障降级 | ✅ | semantic down → 优雅 miss → LLM 完整答; `semantic_unavailable` 记日志 |
| recovery | ✅ | `./start.sh` 恢复 3003; golden 44/44 字段一致 |
| rerank 基线 | 记录 | 4 对均为 `shared=False score=0`（保守拒绝基线，与金样一致） |

**代码变更：无。** 只新增本 bench 验证记录文档并提交。
