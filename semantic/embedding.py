"""embedding.py —— 语义改文向量（e5 ONNX 384，封装自 models）

Task2：`embed_text(text)` = `models.encode_query(text)`（对称 Query 前缀）。
写/查一致性：存储与查询都用 encode_query（语料为历史问题）；`models.encode_passage` 备用，
若要改 passage 须先在验证集对比并保持一致。
原 Phase1 的 FNV/停用词/桶加权逻辑与无用常量已删除；保留函数签名与 L2 归一契约（现 384 维由
models._encode mean-pool+L2 保证）。

仅 import models：本模块不 import nuxt，py3.8 兼容。注意前置动作全在 models/parse（jieba 原子等）。
"""
from models import encode_query  # noqa: E402 —— 语义向量唯一实现归属 models（懒加载单例）


def embed_text(text):
    """返回 384 维 L2 归一语义向量（e5 multilingual，query 前缀）。写/查一致走 encode_query。"""
    return encode_query(text)
