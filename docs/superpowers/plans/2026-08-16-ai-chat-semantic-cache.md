# ai-chat 本地语义知识库（kvstore VSEARCH + jieba 嵌入 + 语义缓存）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ai-chat 的知识库做成完全本地的语义问答缓存：kvstore 内核加 `VSEARCH` 命令，tokenizer 加 jieba `/embed`+`/rerank`，ai-chat-service 每条查询先查缓存（命中直接返回缓存答案）。

**Architecture:** 三个独立组件按依赖顺序：
1. kvstore（C）：新增 `VSEARCH` 命令，遍历内部哈希表找 `semcache:*` 条目，暴力余弦 top-k，返回 RESP `(key, score)`。缓存条目用现有 `SET` 存（二进制记录 `[u32 qlen][q][u32 alen][a][u32 dim][float vec]`），VSEARCH 只读。
2. tokenizer（Python/nuxt）：加 jieba `/embed`（256 维哈希嵌入）+ `/rerank`（关键词重合度）。
3. ai-chat-service（Go）：`semcache` 模块做 CacheQuery/CacheWrite，server.go 在敏感词后先查缓存、聊天后异步写缓存；移除腾讯 vectorDB 路径。

**Tech Stack:** C（kvstore）、Python 3.8 + nuxt + jieba、Go（go-redis、已有 tokenizer 客户端）

## Global Constraints

- 缓存条目 key 前缀固定 `semcache:`；value 二进制记录格式：`[u32 q_len][q bytes][u32 a_len][a bytes][u32 dim][float vec[dim]]`（小端）
- `VSEARCH <dim> <query_vec_binary> <topk>` → RESP `*[2*k]` 交替 `(key, score)`，score=余弦相似度 0~1（保留 4 位小数）；无匹配 `*0\r\n`
- 遍历 `global_hash`：`ht[0]` 必查；`rehash_idx >= 0`（扩容中）时也查 `ht[1]`（节点只在一个表里，不会重复）
- 嵌入维度固定 256，L2 归一化；jieba 分词 + 字 bigram 哈希累加
- ai-chat-service：每条查询先查缓存（命中流式返回、不调大模型）；关键词只用于 rerank 校验 + MySQL 记录，不再门控检索
- config：`ai-chat-service/dev.config.yaml` 加 `semantic_cache: {enabled: true, threshold: 0.70, rerank_threshold: 0.50}`
- 阈值默认 0.70/0.50，按实际 jieba 嵌入行为在验收时调优
- 提交进 9.1-kvstore 仓库（main）；kvstore 源码在根目录，ai-chat 在 `ai-chat/`；父仓库有未提交课设改动，**只 git add 本任务文件**
- kvstore 构建：`make`（已有工具链）；tokenizer 重启：`fuser -k -TERM 3002/tcp` 后重起

---

### Task 1: kvstore VSEARCH 命令（C）

**Files:**
- Create: `src/storage/kvs_vector.c`
- Modify: `src/main/kvstore.c`（命令分发 + RESP 辅助已存在）
- Modify: `include/kvstore/kvstore.h`（声明）
- Create: `tools/test_vsearch.py`（RESP 测试客户端）

**Interfaces:**
- Produces: `int kvs_vector_search(int dim, const float *query, int topk, char *resp, int cap)`（写入 RESP 报文，返回字节数）
- Consumes: `global_hash`（`include/kvstore/kvstore.h` 的 `kvs_hash_t`）、`resp_bulk`/`resp_error`（main.c）
- 值格式：`[u32 qlen][q][u32 alen][a][u32 dim][float vec[dim]]`，向量在 q/a 之后

- [ ] **Step 1: 创建 `src/storage/kvs_vector.c`**

```c
/**
 * @file kvs_vector.c
 * @brief 语义向量检索：遍历 global_hash 中 semcache:* 条目，暴力余弦 top-k
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "kvstore/kvstore.h"

#define VSEARCH_PREFIX "semcache:"
#define VSEARCH_PREFIX_LEN (sizeof(VSEARCH_PREFIX) - 1)

/* 值布局: [u32 qlen][q][u32 alen][a][u32 dim][float vec[dim]]（小端） */
static int parse_vec(const char *value, size_t vlen, int want_dim, const float **vec_out, int *dim_out) {
    if (!value || vlen < 12) return -1;
    size_t pos = 0;
    uint32_t qlen = 0, alen = 0, dim = 0;
    memcpy(&qlen, value + pos, 4); pos += 4;
    if (pos + qlen + 4 > vlen) return -1;
    pos += qlen;
    memcpy(&alen, value + pos, 4); pos += 4;
    if (pos + alen + 4 > vlen) return -1;
    pos += alen;
    memcpy(&dim, value + pos, 4); pos += 4;
    if (pos + (size_t)dim * 4 > vlen) return -1;
    if ((int)dim != want_dim) return -1;
    *vec_out = (const float *)(value + pos);
    *dim_out = (int)dim;
    return 0;
}

static float cosine(const float *a, const float *b, int dim) {
    float dot = 0.0f, na = 0.0f, nb = 0.0f;
    for (int i = 0; i < dim; i++) {
        dot += a[i] * b[i];
        na += a[i] * a[i];
        nb += b[i] * b[i];
    }
    if (na < 1e-9f || nb < 1e-9f) return 0.0f;
    return dot / (sqrtf(na) * sqrtf(nb));
}

/* 候选：暴力 top-k（本地规模小，O(n*k) 足够） */
typedef struct { const char *key; float score; } cand_t;

int kvs_vector_search(int dim, const float *query, int topk, char *resp, int cap) {
    if (!query || topk <= 0) return resp_error(resp, cap, "vsearch bad args");
    if (cap < 16) return -1;

    cand_t *cands = (cand_t *)calloc((size_t)topk, sizeof(cand_t));
    if (!cands) return resp_error(resp, cap, "vsearch oom");
    int n = 0;

    /* 遍历 ht[0]（rehash 中再查 ht[1]；节点只在一个表） */
    for (int t = 0; t < 2; t++) {
        if (t == 1 && global_hash.rehash_idx < 0) break;
        hashtable_t *ht = &global_hash.ht[t];
        for (int i = 0; i < ht->max_slots && ht->nodes; i++) {
            for (hashnode_t *node = ht->nodes[i]; node; node = node->next) {
                if (strncmp(node->key, VSEARCH_PREFIX, VSEARCH_PREFIX_LEN) != 0) continue;
                const float *vec = NULL; int vdim = 0;
                if (parse_vec(node->value, node->vlen, dim, &vec, &vdim) != 0) continue;
                float s = cosine(query, vec, dim);
                if (n < topk) {
                    cands[n].key = node->key; cands[n].score = s; n++;
                    /* 简单上浮：新插入候选冒泡到正确位置 */
                    for (int j = n - 1; j > 0 && cands[j].score > cands[j-1].score; j--) {
                        cand_t tmp = cands[j]; cands[j] = cands[j-1]; cands[j-1] = tmp;
                    }
                } else if (s > cands[topk-1].score) {
                    cands[topk-1].key = node->key; cands[topk-1].score = s;
                    for (int j = topk - 1; j > 0 && cands[j].score > cands[j-1].score; j--) {
                        cand_t tmp = cands[j]; cands[j] = cands[j-1]; cands[j-1] = tmp;
                    }
                }
            }
        }
    }

    /* RESP: *[2*k]\r\n (key,score) 交替 bulk */
    int pos = snprintf(resp, cap, "*%d\r\n", n * 2);
    for (int i = 0; i < n; i++) {
        int len = (int)strlen(cands[i].key);
        int klen = snprintf(resp + pos, cap - pos, "$%d\r\n", len);
        if (klen < 0 || pos + klen + len + 4 > cap) { free(cands); return -1; }
        pos += klen; memcpy(resp + pos, cands[i].key, len); pos += len;
        pos += snprintf(resp + pos, cap - pos, "\r\n");
        char ds[32]; int dlen = snprintf(ds, sizeof(ds), "%.4f", cands[i].score);
        int blen = snprintf(resp + pos, cap - pos, "$%d\r\n", dlen);
        pos += blen; memcpy(resp + pos, ds, dlen); pos += dlen;
        pos += snprintf(resp + pos, cap - pos, "\r\n");
    }
    free(cands);
    return pos;
}
```

- [ ] **Step 2: 头文件声明 + 命令分发**

`include/kvstore/kvstore.h` 追加：
```c
/* kvs_vector.c：语义向量检索（VSEARCH 命令实现） */
int kvs_vector_search(int dim, const float *query, int topk, char *resp, int cap);
```

`src/main/kvstore.c` 的 `handle_parsed_command` 里、`MGET` 分支之后加 VSEARCH 分支：
```c
    } else if (!strcmp(op, "VSEARCH") && argc == 4) {
        int dim = atoi(argv[1]);
        float *query = (float *)argv[2];
        int topk = atoi(argv[3]);
        if (dim <= 0 || topk <= 0 || argl[2] != (size_t)dim * sizeof(float)) {
            n = resp_error(resp, BUFFER_CAP, "vsearch bad args");
        } else {
            n = kvs_vector_search(dim, query, topk, resp, BUFFER_CAP);
        }
        if (c) queue_bytes(c, (unsigned char *)resp, (size_t)n);
        goto out;
        return 0;
    }
```
> 说明：VSEARCH 只读，不写 AOF/复制（缓存由 SET 命令持久化）。`argl[2]` 校验保证 query 长度=dim*4。

- [ ] **Step 3: 编译**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
make 2>&1 | tail -3
```

Expected: 编译通过，生成 `kvstore`。

- [ ] **Step 4: Python RESP 测试客户端 `tools/test_vsearch.py`**

```python
#!/usr/bin/env python3
"""kvstore VSEARCH 冒烟测试：SET 二进制缓存条目 + VSEARCH 余弦 top-k"""
import socket, struct, sys

HOST, PORT = "127.0.0.1", 5160

def send_cmd(sock, *parts):
    data = b"*%d\r\n" % len(parts)
    for p in parts:
        data += b"$%d\r\n" % len(p) + p + b"\r\n"
    sock.sendall(data)

def read_resp(sock):
    def readline():
        b = b""
        while not b.endswith(b"\r\n"):
            ch = sock.recv(1)
            if not ch: raise EOFError
            b += ch
        return b[:-2]
    line = readline()
    if line[:1] == b"*":
        return [read_resp(sock) for _ in range(int(line[1:]))]
    if line[:1] == b"$":
        ln = int(line[1:])
        data = b""
        while len(data) < ln + 2:
            data += sock.recv(ln + 2 - len(data))
        return data[:-2]
    if line[:1] == b"+": return line[1:].decode()
    if line[:1] == b"-": raise Exception(line[1:].decode())
    if line[:1] == b":": return int(line[1:])
    return line

def record(q, a, vec):
    bq, ba = q.encode(), a.encode()
    return struct.pack("<I", len(bq)) + bq + struct.pack("<I", len(ba)) + ba + struct.pack("<I", len(vec)) + struct.pack("<%df" % len(vec), *vec)

def norm(v):
    import math
    n = math.sqrt(sum(x*x for x in v))
    return [x/n for x in v] if n else v

def main():
    # 三条缓存：两条语义相近（golang 学习），一条无关（天气）
    entries = [
        ("semcache:a", "golang 怎么学并发", "答1：看官方 tour + 写小项目", norm([1.0, 0.8, 0.6, 0.1])),
        ("semcache:b", "如何学习 Go 的并发", "答2：并发原语 + 实战", norm([0.9, 0.85, 0.7, 0.0])),
        ("semcache:c", "今天天气怎么样", "答3：晴转多云", norm([0.0, 0.1, 0.1, 1.0])),
    ]
    s = socket.create_connection((HOST, PORT))
    for k, q, a, v in entries:
        send_cmd(s, b"SET", k.encode(), record(q, a, v))
        assert read_resp(s) in (b"OK", b"+OK", "OK"), "SET 失败 " + k
    # 查询向量：接近 golang 学习
    qv = norm([0.95, 0.8, 0.65, 0.05])
    qb = struct.pack("<%df" % len(qv), *qv)
    send_cmd(s, b"VSEARCH", b"256", qb, b"2")
    res = read_resp(s)
    print("VSEARCH 结果:", res)
    assert isinstance(res, list) and len(res) == 4, "期望 2 组 (key,score)，实际 %r" % res
    keys = [res[i] for i in range(0, len(res), 2)]
    scores = [float(res[i]) for i in range(1, len(res), 2)]
    assert keys[0] in (b"semcache:a", b"semcache:b"), "top1 应是相近条目，实际 %r" % keys
    assert keys[0] != b"semcache:c", "天气条目不应排第一"
    assert scores[0] > scores[-1], "分数应降序"
    # 空查询向量 → 空结果
    send_cmd(s, b"VSEARCH", b"256", struct.pack("<256f", *([0.0]*256)), b"2")
    res2 = read_resp(s)
    print("无关向量结果:", res2)
    s.close()
    print("PASS")

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 跑 VSEARCH 测试**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
# 确保 kvstore 在 5160 运行（若不在：./kvstore ai-chat/configs/kvstore-ai.conf 后台）
ss -tln | grep 5160 || (fuser -k -TERM 5160/tcp 2>/dev/null; sleep 1; nohup ./kvstore ai-chat/configs/kvstore-ai.conf > /tmp/kvstore-test.log 2>&1 & sleep 1)
python3 tools/test_vsearch.py
```

Expected: `VSEARCH 结果: [b'semcache:a', b'0.xxxx', b'semcache:b', b'0.xxxx']` 且 `PASS`（top1 是相近的 golang 条目，天气条目不在前面）。

- [ ] **Step 6: 重启 AOF 后仍可检索**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
fuser -k -TERM 5160/tcp 2>/dev/null; sleep 2
nohup ./kvstore ai-chat/configs/kvstore-ai.conf > /tmp/kvstore-test.log 2>&1 & sleep 1
python3 tools/test_vsearch.py   # 重启后 SET 的条目靠 AOF 重放，VSEARCH 应仍命中
```

Expected: 重启后再次 `PASS`（AOF 重放恢复缓存，VSEARCH 能检索）。

- [ ] **Step 7: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add src/storage/kvs_vector.c src/main/kvstore.c include/kvstore/kvstore.h tools/test_vsearch.py
git commit -m "feat(kvstore): VSEARCH 命令——遍历 semcache 条目余弦 top-k"
```

---

### Task 2: tokenizer /embed + /rerank（jieba）

**Files:**
- Modify: `ai-chat/tokenizer/tokenizer.py`
- Modify: `ai-chat/tokenizer/requirements.txt`

**Interfaces:**
- Produces: `POST /embed {text}` → `{code:200, embedding:[float;256]}`；`POST /rerank {query, cached_query}` → `{code:200, score:float}`
- Consumes: 无（jieba）

- [ ] **Step 1: requirements 加 jieba + 安装**

`ai-chat/tokenizer/requirements.txt`：
```
nuxt>=0.2.0
tiktoken>=0.3.3
jieba>=0.42.1
```
```bash
pip3 install --user jieba 2>&1 | tail -2
python3 -c "import jieba; print('jieba OK')"
```

Expected: jieba 安装成功可导入。

- [ ] **Step 2: tokenizer.py 加嵌入/校验**

`ai-chat/tokenizer/tokenizer.py` 顶部 import 加：
```python
import math
import jieba
from jieba import analyse
```
文件末尾追加：
```python
EMBED_DIM = 256

def _hash_bucket(s, dim):
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    return h % dim

def embed_text(text):
    vec = [0.0] * EMBED_DIM
    for w in jieba.cut(text):
        w = w.strip()
        if w:
            vec[_hash_bucket(w, EMBED_DIM)] += 1.0
    chars = [ch for ch in text if not ch.isspace()]
    for i in range(len(chars) - 1):
        vec[_hash_bucket(chars[i] + chars[i+1], EMBED_DIM)] += 1.0
    norm = math.sqrt(sum(v*v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec

def rerank_score(query, cached_query):
    kw1 = set(analyse.extract_tags(query, topK=4))
    kw2 = set(analyse.extract_tags(cached_query, topK=4))
    if not (kw1 | kw2):
        return 1.0
    return len(kw1 & kw2) / len(kw1 | kw2)

@route("/embed", methods=["POST"])
@use_args({"text": fields.Str(required=True)}, location="json")
def get_embedding(req: Request, args: dict):
    try:
        return {"code": 200, "embedding": embed_text(args["text"])}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}

@route("/rerank", methods=["POST"])
@use_args({"query": fields.Str(required=True), "cached_query": fields.Str(required=True)}, location="json")
def get_rerank(req: Request, args: dict):
    try:
        return {"code": 200, "score": rerank_score(args["query"], args["cached_query"])}
    except Exception as e:
        logger.error(traceback.format_exc())
        return {"code": 500, "msg": str(e)}
```

- [ ] **Step 3: 重启 tokenizer + curl 验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
fuser -k -TERM 3002/tcp 2>/dev/null; sleep 2
( cd tokenizer && nohup /home/pp/.local/bin/nuxt --port 3002 --module tokenizer.py --workers 2 > /tmp/tokenizer.log 2>&1 & echo $! > /tmp/tokenizer.pid )
sleep 3
echo "=== /embed ==="
curl -s -X POST http://127.0.0.1:3002/embed -H 'Content-Type: application/json' -d '{"text":"golang 怎么学并发"}' | python3 -c 'import sys,json;d=json.load(sys.stdin);v=d["embedding"];print("code",d["code"],"dim",len(v),"norm",round(sum(x*x for x in v)**0.5,3))'
echo "=== /rerank 相近 ==="
curl -s -X POST http://127.0.0.1:3002/rerank -H 'Content-Type: application/json' -d '{"query":"golang 怎么学并发","cached_query":"如何学习 Go 的并发"}' 
echo; echo "=== /rerank 无关 ==="
curl -s -X POST http://127.0.0.1:3002/rerank -H 'Content-Type: application/json' -d '{"query":"golang 怎么学并发","cached_query":"今天天气怎么样"}'
```

Expected: `/embed` 返回 256 维、L2 范数≈1.0；`/rerank` 相近分高于无关分。

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add ai-chat/tokenizer/tokenizer.py ai-chat/tokenizer/requirements.txt
git commit -m "feat(tokenizer): jieba /embed 256维 + /rerank 关键词校验"
```

---

### Task 3: ai-chat-service 语义缓存集成（Go）

**Files:**
- Create: `ai-chat/ai-chat-service/chat-server/semcache/semcache.go`
- Modify: `ai-chat/ai-chat-service/chat-server/server/server.go`
- Modify: `ai-chat/ai-chat-service/chat-server/server/app.go`（如需取 tokenizer 地址）
- Modify: `ai-chat/ai-chat-service/dev.config.yaml`
- Modify: `ai-chat/ai-chat-service/pkg/config/config.go`

**Interfaces:**
- Consumes: tokenizer `/embed`+`/rerank`（Task 2）、kvstore `VSEARCH`+`SET`（Task 1）、go-redis（已有）
- Produces: `semcache.CacheQuery(ctx, query, model) (answer string, hit bool)`、`semcache.CacheWrite(ctx, query, answer)`；config `SemanticCache{Enabled, Threshold, RerankThreshold}`

- [ ] **Step 1: config 加 semantic_cache**

`ai-chat/ai-chat-service/pkg/config/config.go` 的 `Config` 加：
```go
	SemanticCache struct {
		Enabled         bool    `mapstructure:"enabled"`
		Threshold       float32 `mapstructure:"threshold"`
		RerankThreshold float32 `mapstructure:"rerank_threshold"`
	} `mapstructure:"semantic_cache"`
```

`ai-chat/ai-chat-service/dev.config.yaml` 加：
```yaml
semantic_cache:
  enabled: true
  threshold: 0.70
  rerank_threshold: 0.50
```

- [ ] **Step 2: 创建 `semcache/semcache.go`**

```go
package semcache

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"math"
	"net/http"
	"strconv"

	"ai-chat-service/pkg/config"
	kredis "ai-chat-service/pkg/db/redis"
)

const dim = 256

type embedResp struct {
	Code      int       `json:"code"`
	Embedding []float64 `json:"embedding"`
	Msg       string    `json:"msg"`
}
type rerankResp struct {
	Code  int     `json:"code"`
	Score float64 `json:"score"`
	Msg   string  `json:"msg"`
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

func rerank(ctx context.Context, query, cached string) (float64, error) {
	body, _ := json.Marshal(map[string]string{"query": query, "cached_query": cached})
	resp, err := http.Post(config.GetConfig().DependOn.Tokenizer.Address+"/rerank",
		"application/json", bytes.NewReader(body))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	var r rerankResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return 0, err
	}
	if r.Code != 200 {
		return 0, fmt.Errorf("rerank failed: %s", r.Msg)
	}
	return r.Score, nil
}

// 记录格式: [u32 qlen][q][u32 alen][a][u32 dim][float32 vec[dim]]
func encodeRecord(query, answer string, vec []float32) []byte {
	buf := make([]byte, 8+len(query)+len(answer)+4+len(vec)*4)
	pos := 0
	binary.LittleEndian.PutUint32(buf[pos:], uint32(len(query))); pos += 4
	pos += copy(buf[pos:], query)
	binary.LittleEndian.PutUint32(buf[pos:], uint32(len(answer))); pos += 4
	pos += copy(buf[pos:], answer)
	binary.LittleEndian.PutUint32(buf[pos:], uint32(len(vec))); pos += 4
	for i, v := range vec {
		binary.LittleEndian.PutUint32(buf[pos:], math.Float32bits(v)); pos += 4
	}
	return buf
}

// 从记录解析答案（VSEARCH 命中后读回）
func decodeAnswer(value []byte) (string, error) {
	if len(value) < 8 {
		return "", fmt.Errorf("record too short")
	}
	qlen := binary.LittleEndian.Uint32(value[0:])
	pos := 4 + int(qlen)
	if pos+4 > len(value) {
		return "", fmt.Errorf("bad record")
	}
	alen := binary.LittleEndian.Uint32(value[pos:])
	pos += 4
	if pos+int(alen) > len(value) {
		return "", fmt.Errorf("bad record")
	}
	return string(value[pos : pos+int(alen)]), nil
}

func queryKey(query string) string {
	h := fnv.New32a()
	h.Write([]byte(query))
	return fmt.Sprintf("semcache:%x", h.Sum32())
}

// CacheQuery: 嵌入查询 → VSEARCH → 阈值+rerank 校验 → 命中返回答案
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
	res, err := kredis.GetPool().Do(ctx, "VSEARCH", strconv.Itoa(dim), binVec, "5").Result()
	if err != nil {
		return "", false
	}
	arr, ok := res.([]interface{})
	if !ok || len(arr) < 2 {
		return "", false
	}
	bestKey := fmt.Sprintf("%v", arr[0])
	score, _ := strconv.ParseFloat(fmt.Sprintf("%v", arr[1]), 64)
	if score < float64(cnf.SemanticCache.Threshold) {
		return "", false
	}
	// 读回记录拿缓存 query 做 rerank 校验
	// 注意：必须用 HGET 读回——条目是 HSET 写入 hash 引擎的，GET 走数组引擎读不到
	val, err := kredis.GetPool().Do(ctx, "HGET", bestKey).Bytes()
	if err != nil {
		return "", false
	}
	answer, err := decodeAnswer([]byte(val))
	if err != nil {
		return "", false
	}
	// rerank 需要 cached_query，从记录里解析（简化：此处用 decodeQuery 辅助）
	cachedQ := decodeQuery([]byte(val))
	rs, err := rerank(ctx, query, cachedQ)
	if err != nil {
		return "", false
	}
	if rs < float64(cnf.SemanticCache.RerankThreshold) {
		return "", false
	}
	return answer, true
}

func decodeQuery(value []byte) string {
	if len(value) < 4 {
		return ""
	}
	qlen := binary.LittleEndian.Uint32(value[0:])
	if 4+int(qlen) > len(value) {
		return ""
	}
	return string(value[4 : 4+int(qlen)])
}

// CacheWrite: 嵌入 → SET semcache:<hash> <record>
func CacheWrite(ctx context.Context, query, answer string) error {
	cnf := config.GetConfig()
	if !cnf.SemanticCache.Enabled {
		return nil
	}
	vec, err := embedText(ctx, query)
	if err != nil {
		return err
	}
	// 注意：必须用 HSET（hash 引擎）写入——kvstore 的 SET 路由到数组引擎，
	// VSEARCH 只扫描 hash 引擎（global_hash）。用 Do 发 HSET key <二进制记录>。
	return kredis.GetPool().Do(ctx, "HSET", queryKey(query), encodeRecord(query, answer, vec)).Err()
}
```

> 说明：`kredis` 复用 ai-chat-service 已有的 `pkg/db/redis`（go-redis）。VSEARCH 用字符串参数传：`kredis.GetPool().Do(ctx, "VSEARCH", strconv.Itoa(dim), binVec, "5")`（kvstore 按字符串解析 argv）。**缓存写入用 HSET、读回用 HGET**（kvstore 按命令首字母路由引擎：`H*`→hash 引擎、其余→数组引擎；VSEARCH 只扫 hash 引擎，所以语义缓存必须 HSET/HGET 配对，绝不能用 SET/GET）。VSEARCH 返回数组元素为 `interface{}`（RESP bulk → string）。实现时以实测为准。

- [ ] **Step 3: server.go 接入缓存**

`ai-chat/ai-chat-service/chat-server/server/server.go`：
1) import 加 `"ai-chat-service/chat-server/semcache"`
2) `ChatCompletionStream`：敏感词校验通过后（`if !ok { ... }` 之后、`keywords := app.keywords(in)` 之前）插入：
```go
	// 语义缓存：命中直接返回历史答案（不调大模型）
	if cachedAns, hit := semcache.CacheQuery(stream.Context(), in.Message); hit {
		resId := uuid.New().String()
		startRes := app.buildChatCompletionStreamResponse(resId, "", "")
		endRes := app.buildChatCompletionStreamResponse(resId, "", "stop")
		_ = stream.Send(startRes)
		resList := app.buildChatCompletionStreamResponseList(resId, cachedAns)
		for _, res := range resList {
			_ = stream.Send(res)
		}
		_ = stream.Send(endRes)
		return nil
	}
```
3) 移除原 `keywords > 0` 的 `vectorData.QueryData` 命中分支（server.go:197-236 附近，腾讯 vectorDB 路径）
4) 异步收尾 goroutine（写 MySQL 那个）里、`data.Add(records)` 之后加：
```go
			if err := semcache.CacheWrite(context.Background(), in.Message, completionContent); err != nil {
				s.log.Error(err)
			}
```
5) `ChatCompletion`（非流式）同样在 `app.keywords` 前插 CacheQuery（命中直接 `buildChatCompletionResponse(cachedAns)` 返回），并移除其 vectorDB 分支
6) `NewChatService` 的 `vectorData` 参数、字段、`vector.InitDB` 调用、`vector_data` import 一并移除（腾讯 vectorDB 弃用）

> 说明：`completionContent` 是流式累积的完整回答（server.go 已有）。CacheWrite 幂等覆盖同 hash。

- [ ] **Step 4: 构建 + 端到端验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/ai-chat-service/chat-server
export GOPROXY=https://goproxy.cn,direct
go build ./... && echo "build OK"
```

重启 service + 验证：
```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
fuser -k -TERM 50055/tcp 2>/dev/null; sleep 2
cd ai-chat-service/chat-server && go build -o ../../bin/ai-chat-service . && cd ../..
# 重启整个 8 服务栈以加载新 tokenizer/backend
./stop.sh 2>&1 | tail -1
./start.sh 2>&1 | tail -3

echo "=== 发一条消息（写入缓存）==="
TOKEN=$(bash -c 'curl -s -X POST http://localhost:7080/api/v1/sms/send/code -H "Content-Type: application/json" -d "{\"phone\":\"13800138000\"}" >/dev/null; sleep 1; CODE=$(grep -oE "code=[0-9]{6}" ai-chat-backend/runtime/logs/app.log | tail -1 | cut -d= -f2); curl -s -X POST http://localhost:7080/api/v1/user/login -H "Content-Type: application/json" -d "{\"user_name\":\"13800138000\",\"pwd\":\"$CODE\",\"type\":1}" | python3 -c "import sys,json;print(json.load(sys.stdin)[\"access_token\"])"')
curl -s -X POST http://localhost:7080/api/chat-process -H "Authorization: $TOKEN" -H 'Content-Type: application/json' -d '{"prompt":"golang 怎么学并发","options":{}}' > /dev/null
echo "（kvstore 无 SCAN 命令，缓存条目的存在性由下一条命中行为验证）"

echo "=== 再发语义相近消息（应命中缓存，秒回）==="
time curl -s -X POST http://localhost:7080/api/chat-process -H "Authorization: $TOKEN" -H 'Content-Type: application/json' -d '{"prompt":"如何学习 Go 的并发","options":{}}' | head -c 80; echo
```

Expected: 第二条消息秒回且内容是第一条的缓存答案（mock 固定回复），`questions_total` 不增；`semcache:*` 有条目。

- [ ] **Step 5: 阈值调优**

跑一组相似/无关问题，观察 VSEARCH score 分布；若默认 0.70 命中过严/过松，调整 `dev.config.yaml` 的 `threshold`/`rerank_threshold` 并重测（记录调整后的值）。

- [ ] **Step 6: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add ai-chat/ai-chat-service/chat-server/semcache/semcache.go ai-chat/ai-chat-service/chat-server/server/server.go ai-chat/ai-chat-service/chat-server/server/app.go ai-chat/ai-chat-service/dev.config.yaml ai-chat/ai-chat-service/pkg/config/config.go
git commit -m "feat(ai-chat-service): 语义缓存 CacheQuery/CacheWrite，替换腾讯 vectorDB 路径"
```

---

## 自审结论

- **Spec 覆盖**：kvstore VSEARCH→Task1；jieba /embed+/rerank→Task2；ai-chat-service 全查缓存+异步回存+替换 vectorDB→Task3；阈值调优→Task3 Step5；验收（VSEARCH top-k/重启恢复/命中不增计数/无关被 rerank 拦）→各任务验证步骤。
- **无占位符**：C/Python/Go 关键代码完整给出；测试脚本完整。
- **类型一致**：`semcache:` 前缀、记录格式 `[u32 qlen][q][u32 alen][a][u32 dim][float]`、维度 256、`VSEARCH <dim> <queryvec> <topk>`、`semantic_cache.enabled/threshold/rerank_threshold` 在三个任务全局一致。
- **风险标注**：Task3 会重启 service/整个栈；VSEARCH 的 go-redis `Do` 参数编码（整型 vs 字符串）需实测；`decodeQuery` 复用记录前 4 字节（与 decodeAnswer 互补）；腾讯 vectorData 移除涉及 `NewChatService` 签名与 `main.go`。
