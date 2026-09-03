"""models.py —— e5(INT8) ONNX 封装：懒加载单例 + warmup + attention-mask 平均池化(L2)。

前缀职责唯一归属：调用方传 raw 文本，业务层禁止手工拼 "query: "/"passage: " 前缀；
`encode_query/encode_passage` 内部统一加一次前缀并自剥误带前缀（防 "query: query:"）。

池化必须与 `tools/verify_consistency.py` 里已验证的一致：
    取 ONNX token-level 输出（token_embeddings=last_hidden_state），对 attention_mask 覆盖的
    真实 token 做平均池化得 mean vector，再 L2 归一（除以 mask 和 = 真实 token 数，非固定 maxlen）。
不依赖模型自带 sentence_embedding 头（其加权可能与 mean-pool 不同）。

tokenizer 用 `tokenizers.Tokenizer.from_file`（host 无 transformers/torch）；
`enable_truncation(max_length=512)` + `enable_padding(length=512)` 得到定长 ids+attention_mask。
注意 pad 必须以模型 pad token 为准（XLM-R `<pad>`=id 1，非默认 0），否则 pad 区 token id 与
PT/transformers 不一致（mask 门控后不影响向量，但要一次校验 token id 全等）。

ORT 线程：SEMANTIC_INTRA_OP(默认4)/SEMANTIC_INTER_OP(默认1)，CPUExecutionProvider。
懒加载单例：首次调用才 `_load()`（session/tokenizer/manifest），线程安全；`warmup()` 首条固定短文本。
"""
import math
import os
import json
import threading

import numpy as np

MODEL_ID = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"  # 与 MANIFEST upstream_revision 一致
EXPORT_VERSION = "onnx-int8-v1"
DIMENSION = 384
MAX_LENGTH = 512
NAMESPACE = "semd:e5s:v1:"
_KNOWN_PREFIX = ("query: ", "passage: ")

# 模型工件目录（gitignore）：model.onnx(INT8) + tokenizer.json + MANIFEST.json + onnx-fp32(对照)
_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "e5s-v1")

_lock = threading.Lock()
_state = {"session": None, "tok": None, "manifest": None}


def _strip_prefix(text):
    for p in _KNOWN_PREFIX:
        if text.startswith(p):
            return text[len(p):]
    return text


def _load():
    with _lock:
        if _state["session"] is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("SEMANTIC_INTRA_OP", "4"))
        opts.inter_op_num_threads = int(os.environ.get("SEMANTIC_INTER_OP", "1"))
        path = os.path.join(_MODEL_DIR, "model.onnx")
        sess = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(os.path.join(_MODEL_DIR, "tokenizer.json"))
        with open(os.path.join(_MODEL_DIR, "MANIFEST.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        # pad 以 tokenizer 的 pad token 为准（先读 model 配置有无 padding，无则按 XLM-R `<pad>`/id1）。
        # unigram model.unk_id=3；已证 Tokenizers 与 transformers 在此 pad 下 real token id 全等。
        pad_id = _resolve_pad_id(tok)
        _state.update(session=sess, tok=tok, manifest=manifest, pad_id=pad_id)


def _resolve_pad_id(tok, pad_token="<pad>"):
    # tokenizer.json 未配置 padding；取 added/special token 中名为 pad_token 的 id。
    cfg = getattr(tok, "token_to_id", None)
    pid = None
    if callable(getattr(tok, "token_to_id", None)):
        pid = tok.token_to_id(pad_token)
    if pid is None:  # XLM-R/e5 兜底：<pad>=1（已核对 tokenizer.json added_tokens）
        pid = 1
    return pid


def _tokenize(text):
    tok = _state["tok"]
    tok.enable_truncation(max_length=MAX_LENGTH)
    tok.enable_padding(length=MAX_LENGTH, pad_id=_state["pad_id"], pad_token="<pad>")
    enc = tok.encode(text)
    ids = enc.ids
    mask = enc.attention_mask
    if mask is None:  # 兜底：attention_mask 缺失则按非 pad 自动推导（pad 需以 0 覆盖）
        mask = [1 if i != _state["pad_id"] else 0 for i in ids]
    return np.array([ids], dtype=np.int64), np.array([mask], dtype=np.int64)


def _session():
    _load()
    return _state["session"]


def _tokenizer():
    _load()
    return _state["tok"]


def _encode(text) -> list:
    """text(raw/已处理文本)→ L2 归一的 384 向量。token-输出(384 hidden)按 mask 平均池化。"""
    s = _session()
    ids, m_2d = _tokenize(text)
    feeds = {"input_ids": ids, "attention_mask": m_2d}
    # 仅取 token-level 输出做池化，避免依赖自动池化头与输出顺序差异（同 verify_consistency）
    all_out = dict(zip([o.name for o in s.get_outputs()], s.run(None, feeds)))
    name = next((n for n in all_out if n in ("token_embeddings", "last_hidden_state")), None)
    if name is None:
        raise RuntimeError("ONNX 缺 token 级输出: %r" % list(all_out))
    tok_arr = all_out[name][0].astype(np.float64)          # (seq, h)
    mask1d = m_2d[0].astype(np.float64)
    valid = mask1d.astype(np.float64)
    cnt = max(float(valid.sum()), 1e-9)
    mean = (tok_arr * valid[:, None]).sum(axis=0) / cnt    # 按真实 token 数平均（同 verify mean_pool）
    n = math.sqrt(float((mean * mean).sum())) or 1e-12
    vec = mean / n
    if len(vec) != DIMENSION:
        raise RuntimeError("e5 输出维度 %d != %d" % (len(vec), DIMENSION))
    if not all(math.isfinite(float(x)) for x in vec):
        raise RuntimeError("e5 输出含 NaN/Inf")
    return vec.astype(np.float64).tolist()


def encode_query(text) -> list:
    """query 编码：加一次 'query: ' 前缀（业务层禁拼），返回 384 L2 归一 list[float]。"""
    return _encode("query: " + _strip_prefix(text))


def encode_passage(text) -> list:
    """passage 编码：加一次 'passage: ' 前缀。存储/查询当前都用 encode_query(passage 备用)。"""
    return _encode("passage: " + _strip_prefix(text))


def warmup():
    """首条固定短文本 → 校验 384 维且非零。供启动预加载与 /readyz 复用。"""
    _load()
    v = encode_query("hello")
    if len(v) != DIMENSION:
        raise RuntimeError("warmup dim != %d" % DIMENSION)
    if math.sqrt(sum(x * x for x in v)) < 0.5:
        raise RuntimeError("warmup norm < 0.5 异常向量")
    return v


def ready():
    """返回 (ok, detail)；ok=False 时 detail 含 error。加载/校验失败不抛出。"""
    try:
        warmup()
        return True, {}
    except Exception as e:  # noqa
        return False, {"error": str(type(e).__name__) + ": " + str(e)}


def model_info():
    """返回 {model, revision, dimension, vector_namespace, export_version}，供 /model-info & Go 一致性校验。"""
    _load()
    m = _state["manifest"]
    return {
        "model": m.get("upstream_model", MODEL_ID),
        "revision": m.get("upstream_revision", REVISION),
        "dimension": DIMENSION,
        "vector_namespace": NAMESPACE,
        "export_version": EXPORT_VERSION,
    }
