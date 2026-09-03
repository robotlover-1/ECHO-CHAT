import math, pytest
import numpy as np
import models


def _l2(v):
    return math.sqrt(sum(x * x for x in v))


def test_dim_norm_deterministic():
    v1 = models.encode_query("生成一个红黑树")
    v2 = models.encode_query("生成一个红黑树")
    assert len(v1) == 384
    assert all(math.isfinite(x) for x in v1)
    assert abs(_l2(v1) - 1.0) < 1e-4
    assert all(abs(a - b) < 1e-5 for a, b in zip(v1, v2))


def test_prefix_added_once():
    a = models.encode_query("用 C 实现红黑树")
    b = models.encode_query("query: 用 C 实现红黑树")   # 业务层误带前缀也应自剥
    assert all(abs(x - y) < 1e-4 for x, y in zip(a, b))


def test_cross_lang_close():
    a = models.encode_query("红黑树是什么")
    c = models.encode_query("what is a red-black tree")
    cos = sum(x * y for x, y in zip(a, c))
    assert cos > 0.6, cos


def test_encode_passage():
    p = models.encode_passage("红黑树是什么")
    assert len(p) == 384
    assert abs(_l2(p) - 1.0) < 1e-4


def test_model_info():
    info = models.model_info()
    assert info["model"] == "intfloat/multilingual-e5-small"
    assert info["dimension"] == 384
    assert info["vector_namespace"].startswith("semd:")

