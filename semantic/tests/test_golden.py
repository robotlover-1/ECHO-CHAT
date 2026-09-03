# LEGACY (Phase-0 产物)：canonical 嵌入(FN)Phase-1 有意改变 /embed 输出，本文件不再作 CI 门。
# 权威回归迁移到 tests/ 的 test_parse/test_decision/test_fingerprint/test_embedding + tests/eval。

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
