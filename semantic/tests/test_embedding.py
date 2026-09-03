import math
from embedding import embed_text

# 语义向量 = models.encode_query(对称 query 前缀)；合法性(384 维/L2/确定性)另由 test_models 覆盖，
# 本文件聚焦"语义近义 vs 异主题"的相对判别。
# 注意(e5 实测 + 官方 sentence-transformers 对照一致)：multilingual-e5-small 对短查询做句级
# mean-pool 后，**跨主题的绝对余弦本就很高且趋平**（红黑树~线程池≈0.85、红黑树~披萨≈0.84），
# 绝对余弦阈值无法区分主题 → Phase2 跨主题安全由 decision() 的 subject_id 硬门先行保证，
# 余弦仅在**同一 subject_id 内**之 candidate 重排用（Task3 决策层 / Task6-7 校准）。
def cos(a, b):
    return sum(x*y for x, y in zip(a, b))

def test_canonical_alias_high_cosine():
    # 同主题(红黑树=rbtree=red-black tree)别名改写 → 高余弦（e5 实测 ~0.93，取 >0.85）。
    v = cos(embed_text("生成一个红黑树"), embed_text("生成一个 rbtree"))
    assert v > 0.85, v
    v2 = cos(embed_text("用 C 语言生成红黑树"), embed_text("使用 C 编写 rbtree"))
    assert v2 > 0.85, v2

def test_same_subject_outranks_cross_subject():
    # 相对判别通道（仓内正/负样本分离）：
    #   同主题别名近义(红黑树↔rbtree, e5≈0.93) 应显著高于 异主题短查询(红黑树↔线程池, e5≈0.85)。
    # 保留相对 margin 断言而不用绝对阈值——绝对 cos 在 e5 下对异主题趋平(~0.84)，见模块注释。
    same = cos(embed_text("生成一个红黑树"), embed_text("生成一个 rbtree"))
    cross = cos(embed_text("红黑树是什么"), embed_text("线程池是什么"))
    assert same - cross > 0.03, (same, cross)
    assert same > 0.85, same
    # 反例兜底：确认 cross 落在 e5 实际带内（~0.85，非近 1），也非判别性翻转
    assert cross > 0.70, cross

def test_dim_and_norm():
    v = embed_text("生成一个红黑树")
    assert len(v) == 384
    assert abs(math.sqrt(sum(x*x for x in v)) - 1.0) < 1e-6
