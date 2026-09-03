# semantic 独立服务解耦实施计划（Phase 0）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把语义检索（/embed /rerank + 槽位抽取）从 tokenizer 解耦成独立服务 `semantic:3003`，语义行为/检索效果与拆分前逐字段一致。

**Architecture:** 语义代码逐字搬迁到 monorepo 新目录 `semantic/`（nuxt、端口 3003、无 tiktoken）；tokenizer 只留 tiktoken 计 token；Go `semcache.go` 改指 `dependOn.semantic.address` 并加 ctx/超时/降级/标签日志；金样差分（改前 tokenizer 输出 vs 新 semantic 输出）证明行为不变。

**Tech Stack:** Python nuxt 0.2.15 / jieba 0.42.1 / tiktoken 0.7.0；Go（config/semcache）；bash（lib.sh）；Docker compose（仅定义不构建）。

## Global Constraints

- 仓库根 = 本计划所在 repo（下文所有相对路径基于仓库根；命令默认在仓库根执行）。
- **行为不变**：`/embed`、`/rerank` 的路由、请求/响应结构、字段语义不得改动；`CacheQuery/CacheWrite` 签名与判断逻辑不动；256 维、阈值 0.35/0.25、kvstore VSEARCH/向量记录格式不动。
- 依赖锁定：`nuxt==0.2.15`、`jieba==0.42.1`、`tiktoken==0.7.0`（本机实测版）。
- semantic 端口 **3003**；不执行任何 docker build/push；compose 镜像 `${SEMANTIC_IMAGE:-192.168.233.128:5000/2404/semantic:1.0.0}`。
- 提交只 add 本任务涉及文件，message 用中文、动词开头（feat/fix/docs(scope): 风格），如 `feat(semantic): ...`。
- 杀进程用 pid 文件或 `pkill -x`，**禁用** `pkill -f "nuxt.*3002"`（`-f` 会匹配到调用 shell 自身，产生 exit 144 空输出）。
- 依赖 jieba 首次运行会建词典缓存，略慢属正常。

---

### Task 1: 录制金样（改造前 baseline）

录制改造前 tokenizer 的 `/embed`、`/rerank` 输出到 `semantic/tests/golden_cases.json`，作为"行为不变"的比对基准。本任务**不触碰任何业务代码**。

**Files:**
- Create: `semantic/tests/test_golden.py`（corpus + --record/--check）
- Create: `semantic/tests/golden_cases.json`（由 --record 生成）

**Interfaces:**
- Produces: `test_golden.py` 提供 CLI：`--record <base_url>` 把 corpus 响应写入 golden_cases.json；`--check <base_url>` 用 golden_cases.json 对新服务做字段级差分。后续 Task 2/3 复用 `--check http://127.0.0.1:3003`。

- [ ] **Step 1: 确认改造前 tokenizer 在跑**

Run: `ss -tln | grep -E ':3002\b'`
Expected: `LISTEN ... 0.0.0.0:3002 ...`
若未在跑：`./start.sh`（仓库根，普通用户）后重查。

- [ ] **Step 2: 建目录并写工具**

Run: `mkdir -p semantic/tests`，然后创建 `semantic/tests/test_golden.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""semantic 服务与改造前 tokenizer 的字段级差分金样工具。

用法:
  python3 semantic/tests/test_golden.py --record http://127.0.0.1:3002  # 录金样（改造前 tokenizer）
  python3 semantic/tests/test_golden.py --check  http://127.0.0.1:3003  # 校验 live semantic 与金样一致
"""
import argparse
import json
import os
import urllib.request

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_cases.json")
EPS = 1e-7

EMBED_CORPUS = [
    # 定义
    "什么是红黑树", "什么是链表", "红黑树是什么", "什么是 rbtree",
    # 代码实现
    "生成一个红黑树", "生成一个rbtree", "写一个链表", "实现一个简易的冒泡排序",
    "用 C 语言生成一个红黑树", "使用 C++ 编写红黑树", "用 Go 实现红黑树", "python 实现红黑树",
    "用Java写一个线程池", "用 JavaScript 实现深拷贝",
    # 操作
    "红黑树的插入", "红黑树的删除", "链表的遍历", "数组的删除",
    # 上下文依赖 / 状态修改
    "继续修改上面的红黑树", "还记得我之前问的吗", "你是一名资深工程师", "记住我叫小明",
    # 英文 / 中英混合
    "implement a red-black tree", "what is a linked list", "生成一个 BST", "用c写rbtree",
    # 主题缺失（通用 how-to，subject 应为空）
    "怎么减肥", "推荐几部电影", "怎么学好英语",
    # 空白/大小写/标点差异
    " 生成  红黑树  ", "What Is A Linked List?", "红黑树 的 删除!",
]

RERANK_PAIRS = [
    ("生成一个红黑树", "生成一个 rbtree"),
    ("用C语言生成红黑树", "使用C编写rbtree"),
    ("用C语言生成红黑树", "使用C++编写rbtree"),
    ("什么是红黑树", "什么是 rbtree"),
    ("什么是红黑树", "实现一个红黑树"),
    ("红黑树的插入", "红黑树的删除"),
    ("实现一个链表", "链表是什么"),
    ("用Go实现红黑树", "用Python实现红黑树"),
    ("怎么减肥", "怎么学好英语"),
    ("继续修改上面的红黑树", "生成一个红黑树"),
    ("写一个线程池", "用Java写一个线程池"),
    ("什么是数组", "什么是红黑树"),
]


def post_json(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def record(base):
    data = {"embed_cases": [], "rerank_cases": []}
    for t in EMBED_CORPUS:
        resp = post_json(base, "/embed", {"text": t})
        if resp.get("code") != 200:
            raise SystemExit(f"/embed failed for {t!r}: {resp}")
        data["embed_cases"].append({"text": t, "resp": resp})
    for q, c in RERANK_PAIRS:
        resp = post_json(base, "/rerank", {"query": q, "cached_query": c})
        if resp.get("code") != 200:
            raise SystemExit(f"/rerank failed for {q!r}/{c!r}: {resp}")
        data["rerank_cases"].append({"query": q, "cached_query": c, "resp": resp})
    with open(GOLDEN, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    n_embed = len(data["embed_cases"])
    n_rerank = len(data["rerank_cases"])
    print(f"recorded {n_embed} embed + {n_rerank} rerank -> {GOLDEN}")


def check(base):
    with open(GOLDEN, encoding="utf-8") as f:
        data = json.load(f)
    ok = 0
    for case in data["embed_cases"]:
        old, text = case["resp"], case["text"]
        new = post_json(base, "/embed", {"text": text})
        assert new.get("code") == old.get("code"), (text, "code")
        assert new.get("bypass_cache") == old.get("bypass_cache"), (text, "bypass_cache")
        assert new.get("context_dependent") == old.get("context_dependent"), (text, "context_dependent")
        assert new.get("intent") == old.get("intent"), (text, "intent")
        assert new.get("subject") == old.get("subject"), (text, "subject")
        eo, en = old.get("embedding", []), new.get("embedding", [])
        assert len(eo) == len(en) == 256, (text, len(eo), len(en))
        assert all(abs(a - b) < EPS for a, b in zip(eo, en)), (text, "embedding")
        ok += 1
    for case in data["rerank_cases"]:
        old = case["resp"]
        new = post_json(base, "/rerank",
                        {"query": case["query"], "cached_query": case["cached_query"]})
        assert new.get("code") == old.get("code"), (case["query"], "code")
        assert new.get("shared") == old.get("shared"), (case["query"], "shared")
        assert abs(new.get("score", 0.0) - old.get("score", 0.0)) < EPS, (case["query"], "score")
        ok += 1
    print(f"PASS: {ok} cases field-identical (eps={EPS})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", metavar="BASE_URL")
    ap.add_argument("--check", metavar="BASE_URL")
    a = ap.parse_args()
    if a.record:
        record(a.record)
    elif a.check:
        check(a.check)
    else:
        ap.error("need --record or --check")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 对改造前 tokenizer 录金样**

Run: `python3 semantic/tests/test_golden.py --record http://127.0.0.1:3002`
Expected: `recorded 32 embed + 12 rerank -> semantic/tests/golden_cases.json`（无 SystemExit）

- [ ] **Step 4: 工具自检（record 与 check 同源应恒一致）**

Run: `python3 semantic/tests/test_golden.py --check http://127.0.0.1:3002`
Expected: `PASS: 44 cases field-identical (eps=1e-07)` —— 证明工具本身可复现、无随机。

- [ ] **Step 5: Commit**

```bash
git add semantic/tests/test_golden.py semantic/tests/golden_cases.json
git commit -m "test(semantic): 录制改造前 tokenizer 的 embed/rerank 金样(34+12 条)与字段级差分工具"
```

---

### Task 2: 建 semantic 服务本体并过金样

把语义代码**逐字搬迁**为独立服务 `semantic/semantic.py`（含 `/embed` `/rerank` 与新增 `GET /healthz`），补 requirements/Dockerfile/README，注册 lib.sh，启动后对金样 `--check` 通过。

**Files:**
- Create: `semantic/semantic.py`
- Create: `semantic/requirements.txt`
- Create: `semantic/Dockerfile`
- Create: `semantic/README.md`
- Modify: `lib.sh`（SERVICES 增 semantic 行）

**Interfaces:**
- Produces: 服务路由 `POST /embed`、`POST /rerank`（与 tokenizer 原契约一致）、`GET /healthz`（返回 `{status,service,embedding_type,dimension,parser_version}`）；module 由 `nuxt --port 3003 --module semantic.py` 加载。
- Consumes: `semantic/tests/golden_cases.json` + `semantic/tests/test_golden.py --check`（Task 1）。

- [ ] **Step 1: 用 tokenizer 当前语义段组装 semantic.py**

此刻 tokenizer.py 尚未裁剪（快照完整）。语义段自 `EMBED_DIM = 256` 起到文件尾；头部 import 去掉 `import tiktoken`（保留 `import math`/`jieba`）。先定位行号再拼装：

```bash
EMBED_START=$(grep -n '^EMBED_DIM = 256' tokenizer/tokenizer.py | cut -d: -f1)
echo "EMBED_START=$EMBED_START"   # 期望 67
{ head -n 8 tokenizer/tokenizer.py | grep -v '^import tiktoken$'; \
  sed -n "${EMBED_START},\$p" tokenizer/tokenizer.py; } > semantic/semantic.py
head -n 12 semantic/semantic.py   # 目检 import 区
```

- [ ] **Step 2: 追加 /healthz 路由（文件尾）**

```bash
cat >> semantic/semantic.py <<'PYEOF'

@route("/healthz", methods=["GET"])
def healthz(req: Request):
    """健康检查：供 compose healthcheck 与排障使用。不改 /embed /rerank 契约。"""
    return {
        "status": "ok",
        "service": "semantic",
        "embedding_type": "fnv_hash",
        "dimension": EMBED_DIM,
        "parser_version": "v1",
    }
PYEOF
```

- [ ] **Step 3: 确认文件自洽（语法 + 语义函数齐 + 无 tiktoken）**

```bash
python3 -m py_compile semantic/semantic.py
grep -cE '^(def |EMBED_DIM|LANG_PATTERNS|OPERATION_WORDS)' semantic/semantic.py   # 期望函数/常量都在
grep -n tiktoken semantic/semantic.py || echo "NO tiktoken in semantic.py (good)"
grep -nE '@route\("/(embed|rerank|healthz)"' semantic/semantic.py
```

- [ ] **Step 4: 写 requirements.txt（锁定）**

Create `semantic/requirements.txt`：

```text
nuxt==0.2.15
jieba==0.42.1
```

- [ ] **Step 5: 写 Dockerfile**

Create `semantic/Dockerfile`（仿 tokenizer/Dockerfile，端口与 module 改 3003）：

```dockerfile
FROM quay.io/0voice/python:3.10-alpine
WORKDIR /app
ENV PORT 3003

ADD ./semantic.py /app
ADD ./requirements.txt /app

RUN pip install -i  https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn  --upgrade pip
RUN pip install --root-user-action=ignore -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt

CMD ["sh","-c","nuxt --port ${PORT} --module semantic.py --workers 2"]
```

- [ ] **Step 6: 写 README.md**

Create `semantic/README.md`：

```markdown
# semantic（语义检索独立服务）

承接 ECHO-CHAT 语义缓存的向量生成与规则校验，与 tokenizer（计 token）解耦。
- `/embed`：256 维 FNV 哈希加权词面嵌入 + 主题/意图/上下文/状态抽取（契约见 spec）
- `/rerank`：主题/意图/语言/操作冲突硬拒 + 关键词 Jaccard
- `/healthz`：健康检查（embedding_type=fnv_hash, dimension=256）

## 镜像构建（本机 registry 默认）
docker build -t 192.168.233.128:5000/2404/semantic:1.0.0 .

## 服务启动
docker service create --name 2404-semantic \
-p 3003:3003 --replicas 2 --with-registry-auth \
192.168.233.128:5000/2404/semantic:1.0.0

## 本地进程直跑
nuxt --port 3003 --module semantic.py --workers 2
```

- [ ] **Step 7: lib.sh 注册 semantic（tokenizer 之后、ai-chat-service 之前）**

Modify `lib.sh`：在 `SERVICES` 数组中 tokenizer 行下插入：

```bash
  "semantic|3003|$BASE/semantic|nuxt --port 3003 --module semantic.py --workers 2"
```

- [ ] **Step 8: 启动 semantic 并验证**

Run: `./start.sh`（只补起缺失的 semantic；既有服务端口占用会跳过）

```bash
ss -tln | grep -E ':3003\b'
curl -s -m 3 http://127.0.0.1:3003/healthz          # 期望 status=ok & dimension=256
curl -s -m 3 -X POST http://127.0.0.1:3003/embed \
  -H 'Content-Type: application/json' -d '{"text":"生成一个红黑树"}' | head -c 200
```

- [ ] **Step 9: 新服务过金样（行为不变的第一证明）**

Run: `python3 semantic/tests/test_golden.py --check http://127.0.0.1:3003`
Expected: `PASS: 46 cases field-identical (eps=1e-07)`

> 若 `GET /healthz` 不通（nuxt 对 GET 有兼容问题），把该路由 methods 改为 `["GET","POST"]`，并同步把本计划 Task 5 compose healthcheck 的探测改为 POST（urllib.Request 带 data）；/embed /rerank 不受影响。

- [ ] **Step 10: Commit**

```bash
git add semantic/semantic.py semantic/requirements.txt semantic/Dockerfile semantic/README.md lib.sh
git commit -m "feat(semantic): 语义检索独立服务——从 tokenizer 逐字搬迁 /embed /rerank + /healthz，注册 lib.sh(:3003)"
```

---

### Task 3: 裁剪 tokenizer（只留计 token）

删 tokenizer 的语义代码与 jieba/math 依赖，锁 tiktoken 版本；重启后确认 `/tokenizer/<model>` 正常、语义路由 404、semantic 金样仍过。

**Files:**
- Modify: `tokenizer/tokenizer.py`（删 EMBED_DIM 起全部语义代码 + 删 jieba/math import）
- Modify: `tokenizer/requirements.txt`（锁版本、去 jieba）

**Interfaces:**
- Consumes: Task 2 的 `semantic/semantic.py`（已承载语义职责）。
- Produces: tokenizer 只暴露 `POST /tokenizer/<model>`；`/embed`、`/rerank` 404。

- [ ] **Step 1: 确认 semantic 仍在跑、金样仍在**

Run: `python3 semantic/tests/test_golden.py --check http://127.0.0.1:3003`
Expected: PASS

- [ ] **Step 2: 裁剪 tokenizer.py**

保留 1..`EMBED_DIM` 上一行（即 tiktoken 路由与 `num_tokens_from_messages`），去掉 jieba/math import：

```bash
EMBED_START=$(grep -n '^EMBED_DIM = 256' tokenizer/tokenizer.py | cut -d: -f1)
head -n $((EMBED_START - 1)) tokenizer/tokenizer.py \
  | grep -vE '^(import math|import jieba|from jieba import analyse)$' \
  > /tmp/tokenizer_new.py
mv /tmp/tokenizer_new.py tokenizer/tokenizer.py
```

- [ ] **Step 3: 目检裁剪结果**

```bash
python3 -m py_compile tokenizer/tokenizer.py
grep -nE 'import tiktoken|num_tokens_from_messages|@route' tokenizer/tokenizer.py
# 期望：只含 tiktoken import、num_tokens_from_messages、/tokenizer 路由
grep -n 'jieba\|EMBED_DIM\|/embed\|/rerank' tokenizer/tokenizer.py || echo "semantic code removed (good)"
```

- [ ] **Step 4: 锁 tokenizer requirements**

Overwrite `tokenizer/requirements.txt`：

```text
nuxt==0.2.15
tiktoken==0.7.0
```

- [ ] **Step 5: 重启 tokenizer 使裁剪生效**

只用 pid 文件杀 tokenizer（勿用 `pkill -f`）：

```bash
kill "$(cat runtime/pids/tokenizer.pid 2>/dev/null)" 2>/dev/null || true
sleep 1
./start.sh          # 补起缺失的 tokenizer（semantic 已在跑会跳过）
```

- [ ] **Step 6: 验证 tokenizer 职责收窄**

```bash
ss -tln | grep -E ':3002\b'
curl -s -m 3 -o /dev/null -w 'embed=%{http_code}\n' -X POST http://127.0.0.1:3002/embed -H 'Content-Type: application/json' -d '{"text":"x"}'
curl -s -m 3 -o /dev/null -w 'rerank=%{http_code}\n' -X POST http://127.0.0.1:3002/rerank -H 'Content-Type: application/json' -d '{"query":"a","cached_query":"b"}'
curl -s -m 3 -X POST http://127.0.0.1:3002/tokenizer/gpt-3.5-turbo -H 'Content-Type: application/json' -d '{"role":"user","content":"hello"}'
```
Expected: `embed=404`、`rerank=404`、`/tokenizer/...` 返回 `code:200` 与 num_tokens。

- [ ] **Step 7: semantic 金样复跑（职责搬家后仍一致）**

Run: `python3 semantic/tests/test_golden.py --check http://127.0.0.1:3003`
Expected: `PASS: 46 cases field-identical (eps=1e-07)`

- [ ] **Step 8: Commit**

```bash
git add tokenizer/tokenizer.py tokenizer/requirements.txt
git commit -m "refactor(tokenizer): 移除语义职责只留 tiktoken 计 token，依赖锁 nuxt==0.2.15/tiktoken==0.7.0"
```

---

### Task 4: Go 侧拆分与加固

`semcache.go` 改指 `dependOn.semantic.address`；新增 `DependOn.Semantic` 配置；语义 HTTP 请求带 ctx、共享 Client 超时 1.5s、失败记标签日志并优雅 miss。计 token 的 `GetTokens`/`services/tokenizer` 不动。

**Files:**
- Modify: `ai-chat-service/pkg/config/config.go`（DependOn 增 Semantic）
- Modify: `ai-chat-service/chat-server/semcache/semcache.go`（地址 + 超时 + ctx + 日志）
- Modify: `ai-chat-service/dev.config.yaml`（dependOn.semantic.address）

**Interfaces:**
- Consumes: Task 3 的 semantic 服务（地址由配置注入）。
- Produces: `semcache.CacheQuery/CacheWrite` 签名不变；内部改调 `DependOn.Semantic.Address`。

- [ ] **Step 1: config.go 增 Semantic**

在 `ai-chat-service/pkg/config/config.go` 的 `DependOn` 内、`Tokenizer` 结构之后追加：

```go
		Semantic struct {
			Address string
		}
```

（`Tokenizer` 保留给 `services/tokenizer.GetTokens` 用，勿删。）

- [ ] **Step 2: semcache.go 头部加 import 与包级 client/超时**

在 `ai-chat-service/chat-server/semcache/semcache.go` 的 import 块加入 `"time"` 与 `"ai-chat-service/pkg/log"`，并在 `const dim = 256` 附近追加：

```go
const semanticTimeout = 1500 * time.Millisecond

// 语义服务专用 HTTP client：整体超时兜底，防 semantic 卡死拖住聊天。
var semanticClient = &http.Client{Timeout: semanticTimeout}
```

- [ ] **Step 3: 重写 embedText（地址/ctx/超时/状态码/标签日志）**

把 `semcache.go` 的 `embedText` 整体替换为：

```go
func embedText(ctx context.Context, text string) (vec []float32, bypass bool, subject string, err error) {
	body, _ := json.Marshal(map[string]string{"text": text})
	endpoint := config.GetConfig().DependOn.Semantic.Address + "/embed"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, false, "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := semanticClient.Do(req)
	if err != nil {
		log.ErrorF("semantic_unavailable: %v", err)
		return nil, false, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.ErrorF("semantic_bad_status: %d", resp.StatusCode)
		return nil, false, "", fmt.Errorf("embed status=%d", resp.StatusCode)
	}
	var r embedResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		log.ErrorF("invalid_json: %v", err)
		return nil, false, "", err
	}
	if r.Code != 200 || len(r.Embedding) != dim {
		log.ErrorF("invalid_embedding_dimension: code=%d dim=%d", r.Code, len(r.Embedding))
		return nil, false, "", fmt.Errorf("embed failed: code=%d dim=%d", r.Code, len(r.Embedding))
	}
	vec = make([]float32, dim)
	for i, v := range r.Embedding {
		vec[i] = float32(v)
	}
	return vec, r.BypassCache, r.Subject, nil
}
```

- [ ] **Step 4: 重写 rerank**

把 `semcache.go` 的 `rerank` 整体替换为：

```go
func rerank(ctx context.Context, query, cached string) (float64, bool, error) {
	body, _ := json.Marshal(map[string]string{"query": query, "cached_query": cached})
	endpoint := config.GetConfig().DependOn.Semantic.Address + "/rerank"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return 0, false, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := semanticClient.Do(req)
	if err != nil {
		log.ErrorF("semantic_unavailable: %v", err)
		return 0, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.ErrorF("semantic_bad_status: %d", resp.StatusCode)
		return 0, false, fmt.Errorf("rerank status=%d", resp.StatusCode)
	}
	var r rerankResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		log.ErrorF("invalid_json: %v", err)
		return 0, false, err
	}
	if r.Code != 200 {
		log.ErrorF("rerank_failed: %s", r.Msg)
		return 0, false, fmt.Errorf("rerank failed: %s", r.Msg)
	}
	return r.Score, r.Shared, nil
}
```

- [ ] **Step 5: dev.config.yaml 加 semantic 地址**

在 `ai-chat-service/dev.config.yaml` 的 `dependOn:` 下、`tokenizer:` 之后追加：

```yaml
  semantic:
    address: "http://127.0.0.1:3003"
```

- [ ] **Step 6: 编译**

Run: `cd ai-chat-service/chat-server && go build ./... && cd ../..`
Expected: 无输出（编译通过）。

- [ ] **Step 7: 重启 ai-chat-service 加载新二进制/配置**

```bash
kill "$(cat runtime/pids/service.pid 2>/dev/null)" 2>/dev/null || true
sleep 1
./start.sh
ss -tln | grep -E ':50055\b'
```

- [ ] **Step 8: Commit**

```bash
git add ai-chat-service/pkg/config/config.go ai-chat-service/chat-server/semcache/semcache.go ai-chat-service/dev.config.yaml
git commit -m "feat(semcache): 语义调用改指 semantic:3003，加 ctx+1.5s 超时+标签日志，失败优雅 miss"
```

---

### Task 5: compose/stack 配置与文档同步

补 compose 的 semantic 服务（含 healthcheck、镜像 env 覆盖）、swarm 侧 ai-chat-service 配置的 semantic 地址；同步架构文档与 tokenizer README。

**Files:**
- Modify: `ai-chat-stack/compose.yaml`（semantic 服务块）
- Modify: `ai-chat-stack/configs/ai-chat-service.yaml`（dependOn.semantic.address）
- Modify: `docs/项目文档/04-业务应用.md`（§4.2/§6/§11.7/附录 所指服务改 semantic）
- Modify: `tokenizer/README.md`（确认未提 /embed /rerank；如提则删）

**Interfaces:**
- Consumes: Task 2 的 semantic 服务定义。
- Produces: 部署侧拓扑与文档真相源一致。

- [ ] **Step 1: compose.yaml 增 semantic 服务**

在 `ai-chat-stack/compose.yaml` 的 `tokenizer:` 服务块之后插入：

```yaml
  semantic:
    image: ${SEMANTIC_IMAGE:-192.168.233.128:5000/2404/semantic:1.0.0}
    environment:
      PORT: 3003
    deploy:
      mode: replicated
      replicas: 2
      endpoint_mode: vip
      update_config:
        parallelism: 2
        order: start-first
    command: ["sh","-c","nuxt --port 3003 --module semantic.py --workers 2"]
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3003/healthz', timeout=2).status==200 else 1)"]
      interval: 5s
      timeout: 2s
      retries: 3
      start_period: 5s
```

- [ ] **Step 2: swarm 配置加 semantic 地址**

在 `ai-chat-stack/configs/ai-chat-service.yaml` 的 `dependOn:` 下、`tokenizer:` 之后追加：

```yaml
  semantic:
    address: "http://semantic:3003"
```

- [ ] **Step 3: 校验 compose 语法**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('ai-chat-stack/compose.yaml')); yaml.safe_load(open('ai-chat-stack/configs/ai-chat-service.yaml')); print('yaml ok')"`
若本机无 pyyaml：改用 `docker compose -f ai-chat-stack/compose.yaml config >/dev/null`（如 docker 可用）。
Expected: yaml ok / 无报错。

- [ ] **Step 4: 更新架构文档 04-业务应用.md**

对 `docs/项目文档/04-业务应用.md` 做如下精确替换（用编辑器逐处定位，关键词含 `tokenizer /embed`、`/rerank`、`独立 Python 服务`）：

1. §4.2 写入流程 "1. 问题 → tokenizer /embed → 256 维加权向量…" → 把 **tokenizer /embed** 改为 **semantic /embed**（含该小节其余 `/embed` `/rerank` 出处）。
2. §4.2 时序图里 `["/embed → 256维…"]`、`["/rerank：…"]` 保持路径不变即可（路径未变），如旁注了服务名则改 semantic。
3. §4.2 "**rerank（tokenizer `/rerank`，规则启发式…）**" → 改 "**rerank（semantic `/rerank`，规则启发式…）**"。
4. §6 标题 "## 6. tokenizer：独立 Python 服务" → 拆为两节：tokenizer（仅 tiktoken 计 token）与新增 "semantic：语义检索独立服务（/embed /rerank /healthz，256 维 FNV 哈希，依赖 nuxt+jieba，无 tiktoken）"。§6 正文里原 `/embed` 加权词面描述段整体移到 semantic 节，并注明"职责自 tokenizer 迁入（2026-09-03 Phase 0）"。
5. §11.7 首句/表格里出现的 "(tokenizer.py + semcache.go)" → "(semantic.py + semcache.go)"；§11.7 结论句若指向 tokenizer.py 同理改 semantic。
6. 附录"关键源码索引" `- tokenizer/tokenizer.py /embed /rerank /tokenizer` → 拆成两行：`- tokenizer/tokenizer.py /tokenizer/<model>`（计 token）与 `- semantic/semantic.py /embed /rerank /healthz`。
7. §8.1 若列举服务数/清单，把 semantic 计入（5 个业务服务 → 6 个，如需保留旧数则注"另有 semantic:3003"）。

完成后自查：`grep -n 'tokenizer /embed\|tokenizer .*rerank' docs/项目文档/04-业务应用.md` 应无残留（指向语义的出处都改 semantic）。

- [ ] **Step 5: tokenizer/README 校对**

Run: `grep -n 'embed\|rerank' tokenizer/README.md`
若 README 未提 /embed /rerank → 无改动。若提及 → 删除该表述并注明"语义职责已迁至 semantic/（见仓库 docs spec）"。

- [ ] **Step 6: Commit**

```bash
git add ai-chat-stack/compose.yaml ai-chat-stack/configs/ai-chat-service.yaml docs/项目文档/04-业务应用.md tokenizer/README.md
git commit -m "docs(stack): compose 增 semantic 服务+healthcheck、swarm 配置加 semantic 地址、架构文档同步语义服务拆分"
```

---

### Task 6: 端到端一致性 + 故障注入 + 基线记录

验证本地全栈下缓存命中/拒绝判定、计 token 与额度不回归、semantic 故障降级，并产出高命中率用例的拆分前/后基线记录。**不改代码（除非发现回归，此时修复并补 commit）。**

**Files:**
- 可能 Create: `runtime/` 或 `docs/superpowers/bench/` 下 `2026-09-03-semantic-decouple-e2e.md`（验证记录）
- 视回归修复而定

**Interfaces:**
- Consumes: Task 1-4 的产物（golden、semantic:3003、Go 指向 semantic）。

- [ ] **Step 1: 确认全栈健康**

Run: `ss -tln | grep -E ':3002|:3003|:50055|:5160|:7080'`
Expected: 3002(tokenizer) 3003(semantic) 50055(ai-chat-service) 5160(kvstore) 7080(backend) 均在听。

- [ ] **Step 2: 计 token 不回归（tokenizer 纯职责自检）**

```bash
curl -s -m 3 -X POST http://127.0.0.1:3002/tokenizer/deepseek-v4-flash -H 'Content-Type: application/json' -d '{"role":"user","content":"实现一个红黑树"}'
```
Expected: `code:200` 且 num_tokens 为正整数。

- [ ] **Step 3: 语义命中/拒绝判定不回归**

通过运行中的前端 `http://localhost:7080`（或等价聊天 API）各发两条：

1. 发一条**代码实现类**问题（如"用 Python 写一个单例"），等流式完成后缓存已写入；
2. 改口重发**语义近似**问题（如"用 Python 实现单例模式"）→ 期望**命中缓存**：秒回、响应标注 source=cache、tokensSaved>0、不扣额度；
3. 发一条**语言冲突**问题（如"用 C++ 写一个单例"）→ 期望**不误命中**、正常走 LLM。

记下三条结果。若当前无法连真/伪 LLM（proxy 未配 key），改用 mock：把 `openai-api-proxy/dev.config.yaml` 的 `base_url` 指向 `http://localhost:8083/v1`（mock 服务）并重启 proxy 后再试；该 yaml 是既有本地改动，验证完还原。

预期行为与本任务无关性说明：CacheQuery/CacheWrite 判断逻辑本轮零改动，命中与否由 kvstore 内容 + golden 等价的服务输出决定，因此以上 3 条只要与拆分前线上行为一致即通过。

- [ ] **Step 4: 故障降级（semantic 不可用 → 聊天不挂）**

```bash
kill "$(cat runtime/pids/semantic.pid 2>/dev/null)" 2>/dev/null || true
sleep 1
curl -s -m 5 -o /dev/null -w 'semantic_down healthz=%{http_code}\n' http://127.0.0.1:3003/healthz || true
```
此时在 UI 再发一条任意问题：
Expected: 正常走 LLM 回答（缓存 miss 而非报错）；ai-chat-service 日志（runtime/logs/service.log）出现 `semantic_unavailable` 或连接拒绝类记录，且 `-m` 请求在 1.5s 内让位。

然后恢复：`./start.sh`，确认 `ss -tln | grep ':3003\b'` 恢复监听、`curl http://127.0.0.1:3003/healthz` 200。

- [ ] **Step 5: 高命中率用例基线（非阻断，只记录）**

用 `semantic/tests/test_golden.py` 的 RERANK_PAIRS 中代表用例（rbtree↔红黑树、C↔C++、定义↔实现、插入↔删除）直接观察 `/rerank` 现结果并写入验证记录：

Run: `python3 - <<'PY'
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
PY`
Expected: 结果与 Task 1 金样一致（该轮**不要求**命中/共享达标，仅记录，留 Phase 1+ 对照）。把输出存进验证记录文档。

- [ ] **Step 6: 写验证记录并（如有回归修复）提交**

Create `docs/superpowers/bench/2026-09-03-semantic-decouple-e2e.md`，记录 Step 3/4/5 实测输出与结论。
若 Step 3 或 Step 4 出现与预期不符的行为，用 systematic-debugging 排查修复，修复后补 `git add <修复文件> docs/superpowers/bench/2026-09-03-semantic-decouple-e2e.md && git commit`。

无回归则：
```bash
git add docs/superpowers/bench/2026-09-03-semantic-decouple-e2e.md
git commit -m "docs(bench): semantic 解耦端到端验证记录(命中/降级/基线)"
```

---

## Self-Review 对照

- spec §组件① semantic/ 目录与文件 → Task 2；/healthz → Task 2 Step 2
- spec §② tokenizer 裁剪 + requirements → Task 3
- spec §③ HTTP 契约不变 → Task 1 golden 差分守护（Task 2 Step 9、Task 3 Step 7）
- spec §④ Go 侧 config/semcache/dev.yaml → Task 4
- spec §⑤ 错误日志标签 → Task 4（semantic_unavailable/bad_status/invalid_json/invalid_embedding_dimension/rerank_failed）
- spec §⑥ lib.sh/compose/healthcheck → Task 2 Step 7、Task 5
- spec §⑦ 文档同步 → Task 5
- spec 验收 A 阻断（金样一致/404/依赖锁/Go build/降级）→ Task 1-4 覆盖；B 端到端+故障 → Task 6；C 高命中率基线（非阻断）→ Task 6 Step 5
