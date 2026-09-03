import math
from embedding import embed_text

def cos(a, b):
    return sum(x*y for x, y in zip(a, b))

def test_canonical_alias_high_cosine():
    v = cos(embed_text("生成一个红黑树"), embed_text("生成一个 rbtree"))
    assert v > 0.55, v
    v2 = cos(embed_text("用 C 语言生成红黑树"), embed_text("使用 C 编写 rbtree"))
    assert v2 > 0.5, v2

def test_cross_topic_low():
    v = cos(embed_text("红黑树是什么"), embed_text("线程池是什么"))
    assert v < 0.35, v

def test_dim_and_norm():
    v = embed_text("生成一个红黑树")
    assert len(v) == 256
    assert abs(math.sqrt(sum(x*x for x in v)) - 1.0) < 1e-6
