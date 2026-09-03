# ECHO-CHAT 语义检索 Phase 2+3（合并）实施计划：E5 Dense + 指纹 + 规则硬门 融合检索

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 multilingual-e5-small(ONNX/INT8) 替换 256 维词面向量，并把线上检索重构为"Query 单次编码 → VSEARCH 返回余弦 → 纯规则 hard_decide + 单一阈值"，在 CPU-only 上提升跨语言/超本体召回而不牺牲区分度。

**Architecture:** `semantic/models.py` 封装唯一前缀职责(encode_query/encode_passage + attention-mask 池化 + warmup)；`decision.py` 拆为纯规则 hard_decide(无模型, reason 增 `semantic_soft_match`)；语义服务暴露 `/v1/decision[/batch]`、`/readyz`、`/model-info`；kvstore `VSEARCH` 加可选前缀参数(白名单校验)并写 `semd:e5s:v1:`；Go semcache 改为"一次 /embed、VSEARCH 余弦 + 批决策、acceptance_threshold + min_margin、异步回填、模式灰度"。

**Tech Stack:** Python 3.8 本地 / 3.10-slim 镜像、onnxruntime、tokenizers、numpy（torch 仅一次性导出）；Go(config+semcache)；C（kvstore VSEARCH）；kvstore 为 pocket-kv 子模块。

## Global Constraints

- 仓库根 = ECHO-CHAT；kvstore 子模块仓库在 `kvstore/`（pocket-kv）。命令默认仓库根执行；禁 sudo/root；杀服务用 `runtime/pids/*.pid` + `./start.sh`，勿用 `pkill -f`。
- **前缀职责唯一在 models**：业务层/decision/路由禁止手工拼 `query:`/`passage:`；`encode_*` 内部自剥已有前缀。写查必须一致（默认均 encode_query）。
- **线上决策无模型**：`hard_decide(qp,cp) -> (shared, reason, soft)` 纯规则；向量分一律来自 VSEARCH；Query 每请求只编码一次。
- 命名空间含版本 `semd:e5s:v1:`（常量）；`/model-info` 与 Go config 不一致 → 禁用向量缓存(仅 fp 保留)并告警。
- 阈值/soft 启动前由校准产物填入；不得以 `null` 上线；soft 默认 `false`。
- reason 枚举增 `semantic_soft_match`；其余 9 个不变；reject 分支分数无关（无分数返回）。
- Docker 基座换 `python:3.10-slim`；runtime requirements：`onnxruntime`、`tokenizers`、`numpy`（版本按本机实测锁定）；torch 只进一次性导出 venv。
- 仓库脏文件(kvstore submodule 未提交改动除外——本阶段 kvstore 是**主动要改的子模块**，需在子模块内提交并 bump 指针)不碰其它脏文件。
- 提交只 stage 本任务文件；离线 pytest 在 `semantic/` 下跑。

---

### Task 1: 模型导出 + MANIFEST + 一致性

一次性、环境重：从 HF 拉 multilingual-e5-small，用 dev venv(torch)导出 ONNX FP32 → 动态量化 INT8 → 生成 MANIFEST(sha256)，跑 PT/FP32/INT8 一致性。产物落 `semantic/models/e5s-v1/`（gitignore 目录内不提交模型大文件；MANIFEST.json 提交）。

**Files:**
- Create: `semantic/models/e5s-v1/{model.onnx, tokenizer.json, config.json, special_tokens_map.json, MANIFEST.json}`
- Create: `semantic/tools/export_e5_onnx.py`
- Create: `semantic/tools/verify_consistency.py`
- Test: `semantic/tests/test_model_artifacts.py`（MANIFEST 存在性/字段/路径）

**Interfaces:**
- Produces：`MODELS_DIR=semantic/models/e5s-v1`、`MANIFEST.json` 字段集（spec §①），Task 2 models.py 读取；`semantic/models/` 加入 `.gitignore`（`models/e5s-v1/*.onnx` 忽略但保留 `MANIFEST.json` 例外写法）。

- [ ] **Step 1: 建导出工具 `semantic/tools/export_e5_onnx.py`**

```python
"""一次性导出 multilingual-e5-small → ONNX FP32 → INT8 + MANIFEST。torch 仅用于本工具。"""
import argparse, hashlib, json, os, urllib.request, shutil, subprocess, sys

MODEL_ID = "intfloat/multilingual-e5-small"
REVISION = "main"           # 固定后写入 MANIFEST
OUT = os.path.join(os.path.dirname(__file__), "..", "models", "e5s-v1")
HF = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

def dl(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        print("dl", url)
        urllib.request.urlretrieve(url, dest)

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default=REVISION)
    a = ap.parse_args()
    # 1) 下载 tokenizer 三件套 + config
    base = f"{HF}/{MODEL_ID}/resolve/{a.rev}"
    for fn in ["config.json", "tokenizer.json", "special_tokens_map.json"]:
        dl(f"{base}/{fn}", os.path.join(OUT, fn))
    # 2) 用 optimum 导出 ONNX（需 dev venv: pip install optimum[exporters] transformers torch onnx onnxruntime）
    cmd = [sys.executable, "-m", "optimum.exporters.onnx",
           "--model", MODEL_ID, "--task", "feature-extraction",
           "--output", os.path.join(OUT, "onnx-fp32")]
    subprocess.run(cmd, check=True)
    # 3) 动态量化 INT8 → model.onnx
    import onnxruntime.quantization as q
    q.quantize_dynamic(os.path.join(OUT, "onnx-fp32", "model.onnx"),
                       os.path.join(OUT, "model.onnx"),
                       weight_type=q.QuantType.QInt8)
    # 4) MANIFEST
    man = {
        "upstream_model": MODEL_ID, "upstream_revision": a.rev,
        "export_tool_version": "optimum-onnx",
        "onnxruntime_version": subprocess.run([sys.executable, "-c", "import onnxruntime;print(onnxruntime.__version__)"],
                                              capture_output=True, text=True).stdout.strip(),
        "quantization_config": {"dynamic": "int8", "weight_type": "QInt8"},
        "model_sha256": sha(os.path.join(OUT, "model.onnx")),
        "tokenizer_sha256": sha(os.path.join(OUT, "tokenizer.json")),
        "export_timestamp": "2026-09-04",
    }
    with open(os.path.join(OUT, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print(json.dumps(man, indent=2))

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 建 dev venv 并安装导出依赖（一次性）**

```bash
python3 -m venv /tmp/e5exp && /tmp/e5exp/bin/pip install -U pip
HF_ENDPOINT=https://hf-mirror.com /tmp/e5exp/bin/pip install "optimum[exporters]" transformers torch onnx onnxruntime tokenizers numpy
```
若 HF 主站已可达可省略 HF_ENDPOINT；优先 hf-mirror。若安装/导出失败（网络/内存/7GB 限制），**停止并报告 BLOCKED**，不要伪造产物。

- [ ] **Step 3: 运行导出**

Run: `/tmp/e5exp/bin/python semantic/tools/export_e5_onnx.py --rev <固定sha>`（先用 `main`；跑通后把 sha 固化回工具默认与 MANIFEST）
Expected: `semantic/models/e5s-v1/{model.onnx, tokenizer.json, config.json, special_tokens_map.json, MANIFEST.json}` 生成；model.onnx 存在（INT8）。

- [ ] **Step 4: 一致性验证 `semantic/tools/verify_consistency.py`**

```python
"""PT(仅导出 venv) vs ONNX-FP32 vs ONNX-INT8 排序/向量一致性；取 60 条中英句子。"""
import subprocess, sys
# 在 /tmp/e5exp 跑：对每句分别用 transformers(PT)、fp32 onnx、int8 onnx 编码，
# 断言：cos(v_pt, v_fp32) > 0.999; cos(v_fp32, v_int8) > 0.99; 输出无 NaN/Inf; 对 20 对 (q,c) 排序一致。
# 实现：加载 fp32/int8 session 用 models 相同 attention-mask 池化；PT 用 transformers AutoModel + AutoTokenizer。
```
具体断言数值与"临界翻转率"记录到报告；翻转定义为 20 对中 INT8 相对 PT 排序变化的对数，目标 ≤1。空串与超长(512 截断)输入各测一次不炸。

- [ ] **Step 5: 工件测试 `semantic/tests/test_model_artifacts.py`**

```python
import json, os, pytest
HERE = os.path.dirname(__file__)
D = os.path.normpath(os.path.join(HERE, "..", "models", "e5s-v1"))
def test_manifest():
    m = json.load(open(os.path.join(D, "MANIFEST.json"), encoding="utf-8"))
    assert m["upstream_model"] == "intfloat/multilingual-e5-small"
    for k in ["model_sha256", "tokenizer_sha256", "export_version"]:
        pass  # export_version 可选；必须字段检查
    assert os.path.exists(os.path.join(D, "model.onnx"))
    assert os.path.exists(os.path.join(D, "tokenizer.json"))
def test_manifest_sha():
    import hashlib
    m = json.load(open(os.path.join(D, "MANIFEST.json"), encoding="utf-8"))
    h = hashlib.sha256(open(os.path.join(D, "model.onnx"), "rb").read()).hexdigest()
    assert h == m["model_sha256"]
```

- [ ] **Step 6: 更新 `.gitignore`**：`semantic/models/` 加入 `models/e5s-v1/*.onnx`（保留 MANIFEST.json 提交），即添加：

```gitignore
semantic/models/e5s-v1/*.onnx
semantic/models/e5s-v1/tokenizer.json
semantic/models/e5s-v1/config.json
semantic/models/e5s-v1/special_tokens_map.json
```

- [ ] **Step 7: 跑工件测试并提交（含小文件）**

```bash
cd semantic && python3 -m pytest tests/test_model_artifacts.py -q
cd .. && git add .gitignore semantic/models/e5s-v1/MANIFEST.json semantic/tools/export_e5_onnx.py semantic/tools/verify_consistency.py semantic/tests/test_model_artifacts.py
git commit -m "feat(semantic): 导出 multilingual-e5-small ONNX/INT8 + MANIFEST(sha256) + 一致性工具与工件测试"
```

---

### Task 2: `semantic/models.py` 封装 + 路由(healthz/readyz/model-info) + embedding 切换

**Files:**
- Create: `semantic/models.py`
- Modify: `semantic/embedding.py`（`embed_text = models.encode_query`；删 FNV 逻辑）
- Modify: `semantic/semantic.py`（`/healthz` liveness、`/readyz`、`/model-info`、启动 warmup；/embed 向量来自 embed_text）
- Modify: `semantic/requirements.txt`、`semantic/Dockerfile`、`semantic/README.md`
- Test: `semantic/tests/test_models.py`、改造 `semantic/tests/test_embedding.py`

**Interfaces:**
- Consumes: Task 1 模型工件。
- Produces: `models.encode_query(text)->list[float]`、`models.encode_passage`、`models.warmup()`、`models.ready() -> (ok, detail)`、`models.model_info() -> dict{model,revision,dimension,vector_namespace,export_version}`；`embedding.embed_text(text)`；路由 /readyz /model-info。

- [ ] **Step 1: 写失败测试 `semantic/tests/test_models.py`**

```python
import math, pytest
import models

def test_dim_norm_deterministic():
    v1 = models.encode_query("生成一个红黑树")
    v2 = models.encode_query("生成一个红黑树")
    assert len(v1) == 384
    assert all(math.isfinite(x) for x in v1)
    assert abs(math.sqrt(sum(x*x for x in v1)) - 1.0) < 1e-4
    assert all(abs(a-b) < 1e-5 for a, b in zip(v1, v2))

def test_prefix_added_once():
    a = models.encode_query("用 C 实现红黑树")
    b = models.encode_query("query: 用 C 实现红黑树")   # 业务层误带前缀也应自剥
    assert all(abs(x-y) < 1e-4 for x, y in zip(a, b))

def test_cross_lang_close():
    import math
    a = models.encode_query("红黑树是什么")
    c = models.encode_query("what is a red-black tree")
    cos = sum(x*y for x, y in zip(a, c))
    assert cos > 0.6, cos

def test_model_info():
    info = models.model_info()
    assert info["model"] == "intfloat/multilingual-e5-small"
    assert info["dimension"] == 384
    assert info["vector_namespace"].startswith("semd:")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_models.py -q`
Expected: FAIL（import 错误）。

- [ ] **Step 3: 实现 `models.py`（懒加载 + 线程配置 + warmup + 池化）**

实现 spec §① 代码（`MODEL_ID/REVISION/EXPORT_VERSION/DIMENSION/MAX_LENGTH/NAMESPACE`、`_strip_prefix`、`_encode`（attention-mask 平均池化 + L2）、`encode_query/encode_passage`），另加：

```python
import os, json, threading
_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "e5s-v1")
_lock = threading.Lock()
_state = {"session": None, "tok": None, "manifest": None}

def _load():
    with _lock:
        if _state["session"] is not None:
            return
        import onnxruntime as ort, json as _json
        import numpy as np
        from tokenizers import Tokenizer
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("SEMANTIC_INTRA_OP", "4"))
        opts.inter_op_num_threads = int(os.environ.get("SEMANTIC_INTER_OP", "1"))
        path = os.path.join(_MODEL_DIR, "model.onnx")
        _state["session"] = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        _state["tok"] = Tokenizer.from_file(os.path.join(_MODEL_DIR, "tokenizer.json"))
        with open(os.path.join(_MODEL_DIR, "MANIFEST.json"), encoding="utf-8") as f:
            _state["manifest"] = json.load(f)

def warmup():
    _load()
    v = encode_query("hello")
    if len(v) != 384:
        raise RuntimeError("warmup dim != 384")

def ready():
    try:
        warmup()
        return True, {}
    except Exception as e:      # noqa
        return False, {"error": str(e)}

def model_info():
    _load()
    m = _state["manifest"]
    return {"model": m["upstream_model"], "revision": m["upstream_revision"],
            "dimension": DIMENSION, "vector_namespace": NAMESPACE,
            "export_version": EXPORT_VERSION}
```

Tokenizers 使用（推荐，避免手拼 pad 踩 XLM-R 特殊 token 坑）：

```python
def _tokenize(text):
    tok = _tokenizer()          # Tokenizer.from_file
    tok.enable_truncation(max_length=MAX_LENGTH)
    tok.enable_padding(length=MAX_LENGTH)
    enc = tok.encode(text)
    ids = enc.ids                              # 含 special tokens，按 pad_id 自动补
    mask = list(enc.attention_mask)            # 1/0
    return np.array([ids], dtype=np.int64), np.array([mask], dtype=np.int64)
```

`_encode` 用上面 ids/mask 作 feeds，输出取 `out[0]`（last_hidden_state `[1,seq,hidden]`），按 spec §① attention-mask 平均池化 + L2。若 `enc.attention_mask` 不存在，用 `np.asarray(ids, bool).astype(int64)` 且 pad_id 必须取自 tokenizer 的 pad token（e5/XLM-R 为 `</s>` 的 pad；以 tokenizer.json 为准）。维度/范数断言兜底校验。

- [ ] **Step 4: 跑测试通过**

Run: `cd semantic && python3 -m pytest tests/test_models.py -q`
Expected: PASS（若 cos>0.6 未达，核对 pooling/mask；仅可调该断言阈值并注释依据，不得跳过）。

- [ ] **Step 5: 切换 embedding 与路由**

- `embedding.py`：`embed_text(text)` 实现替换为 `return models.encode_query(text)`；删除 FNV/_hash_bucket/_add/STOP_WORDS 拷贝；保留 docstring 说明"语义向量=encode_query；写查一致"。
- 改造 `test_embedding.py`：256 维/L2≈1 保留断言改 384；红黑树↔rbtree、红黑树↔线程池 余弦断言改 e5 实测（预期 ≥0.55 / ≤0.45 初值，跑后据实微调并注释）；保留确定性。
- `semantic.py`：/embed 向量即 `embed_text(text)`（384）；新增路由：

```python
@route("/healthz", methods=["GET"])
def healthz(req: Request):
    return {"status": "ok"}

@route("/readyz", methods=["GET"])
def readyz(req: Request):
    ok, detail = models.ready()
    return ({"status": "ok", **models.model_info(), **detail} if ok
            else {"status": "error", **detail})

@route("/model-info", methods=["GET"])
def model_info(req: Request):
    return {"code": 200, **models.model_info()}
```

启动即 `models.warmup()`（顶层 try/except：失败打日志不退出，readyz 会反映）。`/embed` 与 `/rerank` 保持；healthz 由原"semantic service 描述"改为 liveness 语义。

- [ ] **Step 6: 更新 requirements/Dockerfile/README**

- requirements.txt 增：`onnxruntime==<本机实测>`、`tokenizers==<本机实测>`、`numpy==1.24.4`（`nuxt==0.2.15`、`jieba==0.42.1` 保留）。
- Dockerfile：`FROM python:3.10-slim`；`ADD ./models/e5s-v1 /app/models/e5s-v1`；其余同前（pip 源不变）。
- README：补模型目录、启动 env（SEMANTIC_INTRA_OP 默认 4）、/readyz /model-info。

- [ ] **Step 7: 离线测试 + 重启冒烟 + 提交**

```bash
cd semantic && python3 -m pytest tests/ -q
cd .. && kill "$(cat runtime/pids/semantic.pid 2>/dev/null)" 2>/dev/null || true; sleep 1; ./start.sh
ss -tln | grep -E ':3003\b'
curl -s -m 10 http://127.0.0.1:3003/healthz
curl -s -m 10 http://127.0.0.1:3003/readyz
curl -s -m 10 http://127.0.0.1:3003/model-info
curl -s -m 10 -X POST http://127.0.0.1:3003/embed -H 'Content-Type: application/json' -d '{"text":"生成一个红黑树"}' | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d['embedding']), d.get('subject_id'))"
```
Expected：healthz `{"status":"ok"}`；readyz ok + model_info 字段；/embed embedding 长 384 且 subject_id=red_black_tree。

```bash
git add semantic/models.py semantic/embedding.py semantic/semantic.py semantic/requirements.txt semantic/Dockerfile semantic/README.md semantic/tests/test_models.py semantic/tests/test_embedding.py
git commit -m "feat(semantic): models 封装(e5 ONNX/INT8 懒加载+warmup+encode_query/passage)+healthz/readyz/model-info；embedding 切换 384 向量"
```

---

### Task 3: decision 纯规则化 + critical 约束 + /v1/decision[/batch] + 旧断言迁移

**Files:**
- Modify: `semantic/decision.py`（hard_decide 纯规则；`semantic_soft_match`；critical constraint 冲突；删模型分）
- Modify: `semantic/parse.py`（`critical_constraints(qp)`：由已知关键短语映射类别）
- Modify: `semantic/semantic.py`（/v1/decision、/v1/decision/batch；/rerank 标 deprecated 返回 {code,score:0,shared,reason,soft}）
- Modify: `semantic/tests/test_decision.py`、`semantic/tests/test_eval.py`（score 断言迁移到检索级 encode_query 余弦）
- Test: `semantic/tests/test_decision_pure.py`（新）

**Interfaces:**
- Consumes: Task 2 models（供检索级断言 proxy）。
- Produces: `decision.hard_decide(qp, cp)->(shared, reason, soft)`；`parse.critical_constraints(qp)->frozenset`；路由 /v1/decision、/v1/decision/batch。

- [ ] **Step 1: 写失败测试 `semantic/tests/test_decision_pure.py`**

```python
import pytest
from parse import parse
from decision import hard_decide

def H(q, c):
    return hard_decide(parse(q), parse(c))

def test_pure_no_model():
    s, r, soft = H("生成一个红黑树", "生成一个 rbtree")
    assert s is True and r == "ok" and soft is False

def test_hard_rejects_unchanged():
    assert H("C 实现红黑树", "C++ 实现 rbtree")[:2] == (False, "language_conflict")
    assert H("红黑树是什么", "实现一个 red-black tree")[:2] == (False, "intent_conflict")
    assert H("用 Python 实现红黑树插入", "用 Python 实现 rbtree 删除")[:2] == (False, "operation_conflict")
    assert H("实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树")[:2] == (False, "constraint_conflict")

def test_soft_not_triggered_when_disabled():
    s, r, soft = H("LRU缓存实现", "最近最少使用缓存怎么写")
    # 两侧无 subject_id，soft 默认关闭 → 保守拒 unknown_subject
    assert s is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_decision_pure.py -q`
Expected: FAIL。

- [ ] **Step 3: `parse.critical_constraints(qp)`**

在 parse.py 加（纯规则）：

```python
CRITICAL_ZH = {
    "concurrency": ["线程安全", "并发安全", "加锁", "锁"],
    "persistence": ["持久化", "落盘"],
    "dup_keys": ["支持重复键", "可重复键"],
    "parent_ptr": ["带父指针", "父指针"],
    "recursion_mode": ["无递归", "非递归"],
    "scope_limitation": ["只实现", "仅实现", "只写", "只做", "完整实现", "简化", "最小"],
    "capacity": ["容量", "N个", "有限", "上限"],
    "space_complexity": ["O(1)空间", "空间复杂度"],
    "time_complexity": ["O(n)", "O(logn)", "时间复杂度", "O(1)"],
    "return_format": ["返回类型", "输出格式", "JSON格式", "格式为"],
}
def critical_constraints(qp) -> frozenset:
    if not qp.raw_text:
        return frozenset()
    cats = set()
    n = qp.normalized_text or qp.raw_text
    for cat, kws in CRITICAL_ZH.items():
        for kw in kws:
            if kw in n:
                cats.add(cat); break
    return frozenset(cats)
```

- [ ] **Step 4: `decision.py` 重构**

- 保留 Phase-1 规则主流程（subject/lang/operation/intent/普通 residual 相等→constraint_conflict 等），把返回 `(score, shared, reason)` 改为 `(shared, reason, soft)`，且：
  - 普通路径 constraint_conflict 判定沿用 Phase-1 `residual_words` 相等性（reject 不变）；
  - 新增 `soft_flag`；普通命中 `soft=False, reason="ok"`；
  - **soft 通道**（仅当 subject 硬门因"缺 id"拟拒时尝试；配置 `SEMANTIC_SOFT_FALLBACK` env 默认 `"0"`）：
    ```python
    def _soft_path(qp, cp) -> bool:
        # 任一侧有 subject_id 而另一侧无 → False(Phase2 保守)
        # intent 均已知且相等；语言敏感意图下语言同值/双空；operation 相等或双空；
        # not (critical_constraints(qp) 与 cp 有交集差非空)；soft_flag=True
    ```
    命中返回 `(True, "semantic_soft_match", True)`；未启用或条件不满足走原保守拒（unknown_subject）。
- `hard_decide` 是主入口（替代原 decide）；保留 `decide = hard_decide` 兼容名？否——**旧 decide 删除**，旧调用点（/rerank、路由、测试）改为 hard_decide。

- [ ] **Step 5: 语义路由端点**

semantic.py 增：

```python
@route("/v1/decision", methods=["POST"])
@use_args({"query": fields.Str(required=True), "cached_query": fields.Str(required=True)}, location="json")
def v1_decision(req, args):
    shared, reason, soft = hard_decide(parse(args["query"]), parse(args["cached_query"]))
    return {"code": 200, "shared": shared, "reason": reason, "soft": soft}

@route("/v1/decision/batch", methods=["POST"])
@use_args({"query": fields.Str(required=True), "candidates": fields.List(fields.Str(), required=True)}, location="json")
def v1_decision_batch(req, args):
    qp = parse(args["query"])
    out = []
    for c in args["candidates"]:
        s, r, soft = hard_decide(qp, parse(c))
        out.append({"cached_query": c, "shared": s, "reason": r, "soft": soft})
    return {"code": 200, "results": out}
```

`/rerank`：改为 `{code:200, score:0.0, shared, reason, soft}`，docstring 标 deprecated（Go 不再调用）。`fields.List` 若 nuxt validation 不支持，改手写解析 JSON 数组。

- [ ] **Step 6: 迁移旧 score 断言（检索级代理）**

`test_decision.py`/`test_eval.py` 中所有依赖 `decide(...)[0]`(score) 的断言改为：match 行断言 `shared is True`（纯规则）；需要余弦的"Go 接受谓词"改在检索级测试里用：

```python
from models import encode_query
import math
def cos(a, b): return sum(x*y for x, y in zip(encode_query(a), encode_query(b)))
```
对 MATCH 行断言 `shared and reason=="ok" and cos(q,c) >= ACCEPTANCE`（`ACCEPTANCE` 在 Task 6 校准后填入；未校准前先用宽松值 0.0 占位并注释"Task6 填"）。reject 行断 `shared is False`（不再依赖 score==0.0）。删除依赖模型分的断言。

- [ ] **Step 7: 跑全量测试 + 提交**

```bash
cd semantic && python3 -m pytest tests/ -q
```
Expected：全绿（含 test_models/test_decision_pure；soft 关闭态断言通过）。

```bash
git add semantic/parse.py semantic/decision.py semantic/semantic.py semantic/tests/test_decision.py semantic/tests/test_eval.py semantic/tests/test_decision_pure.py
git commit -m "refactor(semantic): decision 纯规则化 hard_decide(shared/reason/soft)+critical约束；新增 /v1/decision[/batch]；旧score断言迁检索级"
```

---

### Task 4: kvstore（pocket-kv 子模块）VSEARCH 前缀参数 + 白名单校验

**Files:**
- Modify: `kvstore/kvstore/src/storage/kvs_vector.c`、命令分发（`kvstore/kvstore/src/main/kvstore.c` VSEARCH 分支）
- Test: C 侧新增/复用测试入口（`kvstore/kvstore/tests/` 既有结构内加 vsearch 用例）
- Modify: ECHO-CHAT `.gitmodules` 指针 bump（在子模块提交后）

**Interfaces:**
- Produces：`VSEARCH <dim> <query_vec> <topk> [prefix]`；prefix 缺省 `semcache:`；白名单校验。Task 5 Go 调用 `VSEARCH 384 <vec> 30 "semd:e5s:v1:"`。

- [ ] **Step 1: 读现状**

在 `kvstore/kvstore/src/storage/kvs_vector.c` 顶部 `#define VSEARCH_PREFIX "semcache:"`；命令分支把第 4 参（可选）传给 `kvs_vector_search`。

- [ ] **Step 2: 改 C（前缀参数 + 校验）**

```c
/* kvs_vector.c 内 */
#define VSEARCH_DEFAULT_PREFIX "semcache:"
#define VSEARCH_ALLOWED_DIMS 3          /* 256/384/1024 */
static const int VSEARCH_DIMS[] = {256, 384, 1024};
static const char *VSEARCH_ALLOWED_PREFIX[] = {"semcache:", "semd:e5s:v1:"};

static int valid_prefix(const char *p, int len) {
    if (len < 1 || len > 64) return 0;
    for (int i = 0; i < len; i++) {
        unsigned char c = (unsigned char)p[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == ':' || c == '_' || c == '-')) return 0;
    }
    for (size_t i = 0; i < sizeof(VSEARCH_ALLOWED_PREFIX)/sizeof(*VSEARCH_ALLOWED_PREFIX); i++)
        if (len == (int)strlen(VSEARCH_ALLOWED_PREFIX[i]) &&
            memcmp(p, VSEARCH_ALLOWED_PREFIX[i], (size_t)len) == 0) return 1;
    return 0;
}
static int allowed_dim(int d) {
    for (int i = 0; i < VSEARCH_ALLOWED_DIMS; i++) if (VSEARCH_DIMS[i] == d) return 1;
    return 0;
}
```
`kvs_vector_search(int dim, const float *query, int topk, const char *prefix, int plen, char *resp, int cap)`：
- 校验 `allowed_dim(dim)`、`1<=topk<=100`、prefix 合法（显式空前缀→错；缺省传 `VSEARCH_DEFAULT_PREFIX`）;遍历前缀匹配用 `(plen==0? 默认 : 参数)`。`parse_vec` 的 want_dim 已保证只扫同维记录（异维跳过）。
- 命令分发：解析到可选第 4 参，缺省空→默认。

- [ ] **Step 3: 编 C 测试**

在 `kvstore` 既有测试里加：老三参(默认 semcache: 命中)；新四参(semd:e5s:v1: 只命中该前缀)；空前缀/非法字符/超长/dim 不在集/topk>100/vec 长度错 → 报错不崩；256 与 384 记录混存 → 各按自己 dim 扫、互不串。编译运行测试通过。

- [ ] **Step 4: 子模块提交 + bump 指针**

```bash
cd kvstore && git add -A && git commit -m "feat(kvs_vector): VSEARCH 可选前缀参数+白名单校验(dim/prefix/topk/len)，默认 semcache: 向后兼容" && git rev-parse --short HEAD
cd .. && git add kvstore && git commit -m "chore(kvstore): bump 子模块指针到 VSEARCH 前缀参数版"
```
重编：`cd kvstore && make`（或仓库既有构建脚本），确认 `kvstore/kvstore/kvstore` 新二进制。若需 `./start.sh` 重起 kvstore：`kill "$(cat runtime/pids/kvstore.pid)"` → start.sh。

- [ ] **Step 5: 手动验证 VSEARCH 前缀隔离**

用 redis-cli 向 kvstore(:5160) 写 `semd:e5s:v1:a` 与 `semcache:b` 各一条记录后，分别 `VSEARCH 384 <vec> 5 "semd:e5s:v1:"` 只返回前者；老三参返回后者。提交前清理测试键（本仓库内说明保留即可，AOF 会存——可接受用后删除）。

```bash
git add -A && git commit  # 已在 Step4 提交；本步无新提交仅验证
```

---

### Task 5: Go semcache 重构（单次编码 / 批决策 / 复用余弦 / 阈值+margin / 回填 / 一致性）

**Files:**
- Modify: `ai-chat-service/pkg/config/config.go`、`ai-chat-service/chat-server/semcache/semcache.go`
- Modify: `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`

**Interfaces:**
- Consumes: Task 3 `/v1/decision/batch`、Task 4 `VSEARCH … [prefix]`、Task 2 `/model-info`/embedding 384。
- Produces：`CacheQuery/CacheWrite` 签名不变；新链路（一次 /embed；VSEARCH semd 384 topK30；batch decision；acceptance+margin；async backfill；model-info 一致性校验）。

- [ ] **Step 1: config.go 扩展**

按 spec §⑧ 加字段（mapstructure）：`Dimension int`、`VectorNamespace string`、`EmbeddingModel/EmbeddingRevision/ExportVersion string`、`AcceptanceThreshold float32`、`MinMargin float32`、`SoftSemanticFallback bool`、`VectorReadMode/VectorWriteMode string`、`AsyncBackfill bool`。旧 `Threshold/RerankThreshold` 保留读兼容。

- [ ] **Step 2: yaml（dev + ai-chat-stack）**

`semantic_cache` 下加：`dimension: 384`、`vector_namespace: "semd:e5s:v1:"`、`embedding_model/embedding_revision/export_version`、`acceptance_threshold: <占位 0.0，Task6 校准后填>`、`min_margin: <占位 0.0>`、`soft_semantic_fallback: false`、`vector_read_mode: "new_only"`、`vector_write_mode: "new_only"`、`async_backfill: true`。

- [ ] **Step 3: embedResp 扩展 + 一致性校验**

- embedResp 已含字段；加 `ModelVersion` 等可选；/embed 现返回 384 向量，`dim` 常量改为读 `cnf.SemanticCache.Dimension`。
- 启动/首用一致性：调 `/model-info`，若 model/revision/dimension/namespace 与 config 任一不符 → `log.ErrorF("vector_index_mismatch …")` 并置包级 `vectorEnabled=false`（fp 路径仍可用），CacheQuery/CacheWrite 的 VSEARCH/写向量分支短路。

- [ ] **Step 4: CacheQuery 新链路（一次编码 + 批决策）**

```go
// 1) /embed once
m, err := embedMetaOf(ctx, query)          // 含 vec(384), fingerprint, subject_id, subject_text
if err != nil || m.Bypass || (m.Subject=="" && m.SubjectID=="") { return "", false }
// 2) fp
if fpEnabled && m.Fingerprint != "" && eligible {
    if cq, ok := fpCandidate(ctx, m.Fingerprint); ok {
        if s, r, _ := decisionOf(ctx, query, []string{cq}); len(s)==1 && s[0].Shared && s[0].Reason=="ok" {
            if ans, ok2 := fetchAnswer(ctx, cq); ok2 {
                if cfg.AsyncBackfill && !vecExists(ctx, cq) { go backfillVec(ctx, cq) }
                return ans, true
            }
        }
    }
}
// 3) VSEARCH once
keys := vsearch(ctx, vec, topK, namespace)          // 返回 semd:<q> 候选(剥前缀成 cachedQ 列表) + scores
// 4) batch decision(一次 HTTP)
decs := decisionBatch(ctx, query, cachedQs)
accepted := [](cand, score)...
for i, dq := range cachedQs {
    if !decs[i].Shared { continue }
    if scores[i] < cfg.AcceptanceThreshold { continue }
    if decs[i].Soft && !cfg.SoftSemanticFallback { continue }   // soft 命中需开开关
    accepted = append(accepted, {dq, scores[i]})
}
sort by score desc
if len==0 { return "", false }
top1 := accepted[0]
if len>1 && top1.score - accepted[1].score < cfg.MinMargin { return "", false }
ans := fetchAnswer(ctx, top1.q); if !ok { return "", false }
return ans, true
```

- 埋点：日志 `semcache_query_encodes=1` 于一次 /embed；决策计数 reason 分布。
- `backfillVec(ctx, cachedQ)`：异步 goroutine；幂等（先 `GET semd:...` 判断存在则返回）；`/embed(mode query)` 编 cachedQ 写 semd；失败重试上限 1；限速（每秒最多 N，包级计数）；不阻塞请求；`log.InfoF("backfill …")`。不随请求结束被取消（用 `context.Background()` + 短超时）。
- `vecExists`：`EXISTS semd:e5s:v1:<q>`(kvstore 若有 EXISTS；否则 GET 判 nil)。

- [ ] **Step 5: CacheWrite（只写新 ns + 版本化）**

```go
// 1) SET <q>=<ans>（不变）
// 2) HSET <cfg.VectorNamespace><q> = [u32 dim][vec]（dim 384）
// 3) fp(不变)
// vectorWriteMode=="dual_write" 时额外写旧 semcache:256(灰度期)
```

- [ ] **Step 6: 编译 + 重启 + 服务层冒烟**

```bash
cd ai-chat-service/chat-server && go build ./... && cd ../..
kill "$(cat runtime/pids/service.pid 2>/dev/null)" 2>/dev/null || true; sleep 1; ./start.sh
ss -tln | grep -E ':50055\b'
```
服务层：curl semantic `/embed` 长度 384；`/v1/decision` C++ 对 reason=language_conflict；`/model-info`；kvstore `VSEARCH 384 … 5 "semd:e5s:v1:"` 手工可查。真实聊天 e2e 若可驱动则做（写→跨语言命中），否则如实报告受限。不伪造。

- [ ] **Step 7: 提交**

```bash
git add ai-chat-service/pkg/config/config.go ai-chat-service/chat-server/semcache/semcache.go ai-chat-service/dev.config.yaml ai-chat-stack/configs/ai-chat-service.yaml
git commit -m "feat(semcache): 单次编码/复用VSEARCH余弦/批decision/acceptance+margin/异步回填/模式灰度/model-info一致性；dim 384 semd:e5s:v1:"
```

---

### Task 6: 校准集 + 阈值填入 + 检索质量测试

**Files:**
- Create: `semantic/tools/calibrate.py`、`semantic/tests/eval/retrieval_cases.py`（检索质量集：本体正/超本体正/普通负/同主题硬负/约束负/绕过，≥200）
- Modify: `semantic/tests/test_eval.py`（acceptance 断言用真实阈值）
- Modify: `ai-chat-service/dev.config.yaml`、`ai-chat-stack/configs/ai-chat-service.yaml`（填 acceptance_threshold/min_margin）

**Interfaces:**
- Consumes: Task 2 models、Task 3 hard_decide。
- Produces：`semantic/tests/eval/retrieval_cases.py` 三份切分；`calibrate.py` 输出阈值；config 填实。

- [ ] **Step 1: 建检索质量集（确定性构造 + 手写锚点）**

`retrieval_cases.py` 结构（每行 `(q, c, label)`，label∈{match, neg, hard_neg, bypass}，附 meta subject）：

```python
# 锚点（手写，覆盖本体正/超本体正/同主题硬负）
ANCHOR = [
    # 本体跨语言正
    ("红黑树是什么", "what is a red-black tree", "match", "red_black_tree"),
    ("生成一个红黑树", "生成一个 rbtree", "match", "red_black_tree"),
    # 超本体正（无 subject_id）
    ("LRU缓存实现", "最近最少使用缓存怎么写", "match", None),
    # 同主题硬负（意图/操作/语言）
    ("C 实现红黑树", "C++ 实现红黑树", "neg", "red_black_tree"),
    ("红黑树的插入", "红黑树的删除", "neg", "red_black_tree"),
    # 约束负
    ("实现线程安全的 LRU 缓存", "实现持久化的 LRU 缓存", "neg", None),
    ("把列表去重", "把字符串反转", "neg", None),
    # 绕过
    ("继续修改上面的红黑树", "生成红黑树", "bypass", "red_black_tree"),
]
```
再加 16 本体概念 × (中文/英文措辞/中英) 扩种正样本与 (语言/意图/操作) 负样本（复用 Phase-1 eval 的 SUBJ 表思路），约束负用 CRITICAL_ZH 组合，确保总 MATCH+reject ≥200 且含 ≥15 超本体 match、≥40 同主题硬负。逐条是"真"样本（实现后由测试校验）。

- [ ] **Step 2: 写 `tools/calibrate.py`**

```python
"""三分切分（按主题分组，避免同概念别名跨集）+ 校准 acceptance_threshold/min_margin。
输出: 直方图、建议阈值、bootstrap CI（可选）。阈值规则: 校准集 Precision≥99% 下 Recall 最高。
soft 阈值同理在 soft 子集上（本阶段不启用, 仅报告）。"""
```
- 对每个候选对用 `hard_decide(parse(q),parse(c))` + `cos(models.encode_query(q), encode_query(c))`；
- 正样本 = match 且 decision shared；负样本=neg/hard_neg；把"绕过"排除（它们本不该进检索）。
- 输出建议 `acceptance_threshold`（使 calibration 上正样本过、负样本尽量不过、Precision≥0.99 前提 Recall 最大）与 `min_margin`（可选经验 0.02 起步，说明待验证）。打直方图到报告。
- 主题分组：把锚点/扩种样本按 subject 分桶（含 None 桶）随机 60/20/20 split 到 dev/cal/test；**重复 5 折报告中位**。测试集只在最终跑一次。

- [ ] **Step 3: 填入 config + 检索级断言**

把校准产物写进 dev/stack yaml `acceptance_threshold/min_margin`；`test_eval.py` 用真实阈值断言 MATCH 行 `cos≥acceptance`、reject(非 bypass) 在 hard_decide 下 shared False。soft 保持关闭（`soft_threshold` 不启用，测试断言关闭态行为）。

- [ ] **Step 4: 全量离线测试 + 报告 + 提交**

```bash
cd semantic && python3 -m pytest tests/ -q
```
报告记录：总样本、三份切分、校准阈值、直方图摘要、独立测试集一次结果（Recall@30 近似用检索测试中 Recall@K 代理或说明口径）。

```bash
git add semantic/tools/calibrate.py semantic/tests/eval/retrieval_cases.py semantic/tests/test_eval.py ai-chat-service/dev.config.yaml ai-chat-stack/configs/ai-chat-service.yaml
git commit -m "test(semantic): 检索质量集三分+阈值校准(acceptance/min_margin)+config 填实+检索级断言"
```

---

### Task 7: 文档同步 + 收尾

**Files:**
- Modify: `docs/项目文档/04-业务应用.md`（§4.2/§6.2 更新到 e5/纯规则决策/新命名空间/一次编码）、`semantic/README.md`（补 /readyz /model-info /v1/decision、worker/threads env、Dockerfile slim）、`lib.sh`（semantic 启动 env 若需传 SEMANTIC_INTRA_OP）、`ai-chat-stack/compose.yaml`（semantic env/镜像说明）
- Test: 全量回归

**Interfaces:**
- Consumes: 全部。

- [ ] **Step 1: 文档同步（按文档真实文本就近改）**

- 04 §4.2：写入/查询流程改为 `semd:e5s:v1:`、384、单次 /embed、VSEARCH 余弦 + `/v1/decision/batch`、acceptance+margin、fp+异步回填；§6.2 semantic 服务描述加 models/encode/readyz/model-info。保持章节自洽（进程数仍 9）。
- semantic/README：模型目录与构建(slim)、env、端点表。
- lib.sh：如用 `SEMANTIC_INTRA_OP=4` 默认可不加；若加则同步 compose。

- [ ] **Step 2: 全量回归**

```bash
cd semantic && python3 -m pytest tests/ -q
cd .. && cd ai-chat-service/chat-server && go build ./... && cd ../..
./start.sh   # 全栈起（或确认已在跑）
ss -tln | grep -E ':3002|:3003|:50055|:5160|:7080'
```
+ e2e 尽力（跨语言改写命中；停 semantic miss；一次编码日志观测）。

- [ ] **Step 3: 提交**

```bash
git add docs/项目文档/04-业务应用.md semantic/README.md lib.sh ai-chat-stack/compose.yaml
git commit -m "docs(04/README): Phase2+3 语义检索更新——e5 一次编码/纯规则决策/新命名空间/端点与运维"
```

---

## Self-Review 对照（spec 覆盖）

- spec §① models 封装/前缀/池化/warmup/manifest → Task1(Task2 编码验证)；model-info/readyz → Task2。
- spec §② embedding 384 对称 query → Task2。
- spec §③ decision 纯规则 hard_decide/critical/soft/semantic_soft_match + /v1/decision[/batch] + 旧断言迁移 → Task3。
- spec §④ 路由(healthz liveness/readyz/model-info/启动 warmup) → Task2。
- spec §⑤ kvstore VSEARCH 前缀参数+白名单校验+C 测试+bump 指针 → Task4。
- spec §⑥ Go 一次编码/批决策/复用余弦/acceptance+margin/回填/modes/model-info 一致性/弃双阈值 → Task5。
- spec §⑦ 迁移：fp 异步回填 + 灰度 modes → Task5（backfill + modes）。
- spec §⑧ 配置（dim/ns/模型/阈值/soft/rt workers/threads） → Task5(Task6 填阈值)。
- spec 校准三分集 → Task6；soft 默认关 → Task3/6。
- spec 命名澄清/测试/故障表 → 各 Task 验证步骤 + Task7 文档。
- 风险项（alpine→slim → Task2 Dockerfile；torch 一次性 → Task1；前缀唯一 → Task2 测试）。
