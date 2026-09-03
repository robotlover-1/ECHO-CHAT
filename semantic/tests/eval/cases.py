# 权威集：MATRIX(手工,来自 spec) + _expand(矩阵/模板扩种)。全量 ≥120，按类设下限。
# 每条 (q, c, expect)：expect="match" 真命中；("reject", reason) 真拒绝（reason 为 decision 可达原因）。
# 生成后被 test_eval.py 逐条判真（凡因 parse/decision 实际情况与期望 reason 不符，先修 parser/本体，
# 仅当某期望 reason 不可达才把该 fixture 的 reason 改为实际可达拒因并注释）。不使用任何 nuxt 服务。
from itertools import product  # noqa: F401  —— 预留（模板线性展开仍可用 product）；保留 import 语义

MATRIX = [  # (q, c, expect)  expect: "match" | ("reject", reason)
    ("生成一个红黑树", "生成一个 rbtree", "match"),
    ("用 C 语言生成红黑树", "使用 C 编写 rbtree", "match"),
    ("红黑树是什么", "what is a red-black tree", "match"),
    ("红黑树的插入", "写一个红黑树的插入", "match"),
    ("用 Python 实现红黑树插入", "implement RB-tree insertion in Python", "match"),
    ("C 实现红黑树", "C++ 实现 rbtree", ("reject", "language_conflict")),
    ("生成红黑树", "用 C++ 生成红黑树", ("reject", "language_conflict")),
    ("用 Python 实现单例", "实现单例", ("reject", "language_conflict")),
    ("红黑树是什么", "实现一个 red-black tree", ("reject", "intent_conflict")),  # R3 回归：携词不走 unknown_subject
    ("用 Python 实现红黑树插入", "用 Python 实现 rbtree 删除", ("reject", "operation_conflict")),
    ("红黑树插入", "rbtree 删除", ("reject", "operation_conflict")),
    ("完整实现红黑树", "只实现红黑树插入", ("reject", "operation_conflict")),
    ("实现线程安全的 C++ 红黑树", "实现持久化的 C++ 红黑树", ("reject", "constraint_conflict")),
    ("实现带父指针的二叉树", "实现支持重复键的二叉树", ("reject", "constraint_conflict")),
    ("生成一个红黑树", "实现线程安全的红黑树", ("reject", "constraint_conflict")),
    ("实现一个二叉搜索树", "实现一棵二叉树", ("reject", "subject_conflict")),
    ("用 Rust 写一个快速排序", "用 C 写一个快速排序", ("reject", "language_conflict")),
]

# 扩种主体（16 个已建档概念；en 必须是 concepts.json 里的英文 alias）
SUBJ = [
    ("linked_list", "链表", "linked list"),
    ("singleton", "单例", "singleton"),
    ("thread_pool", "线程池", "thread pool"),
    ("stack", "栈", "stack"),
    ("queue", "队列", "queue"),
    ("hash_table", "哈希表", "hash table"),
    ("quick_sort", "快速排序", "quick sort"),
    ("deep_copy", "深拷贝", "deep copy"),
    ("red_black_tree", "红黑树", "red-black tree"),
    ("binary_search_tree", "二叉搜索树", "binary search tree"),
    ("binary_tree", "二叉树", "binary tree"),
    ("heap", "堆", "heap"),
    ("merge_sort", "归并排序", "merge sort"),
    ("insertion_sort", "插入排序", "insertion sort"),
    ("selection_sort", "选择排序", "selection sort"),
    ("graph", "图", "graph"),
]
# 操作类主体子集：插入/删除有意义的容器型概念
OP_SUBJ = [
    ("linked_list", "链表"), ("red_black_tree", "红黑树"), ("binary_tree", "二叉树"),
    ("binary_search_tree", "二叉搜索树"), ("stack", "栈"), ("queue", "队列"),
    ("hash_table", "哈希表"), ("heap", "堆"),
]


def _expand():
    cases = list(MATRIX)
    # 正样本·中↔英措辞（16×3 组）
    for _sid, zh, en in SUBJ:
        cases.append((f"实现一个{zh}", f"implement {en}", "match"))
        cases.append((f"用 Python 实现{zh}", f"implement {en} in python", "match"))
        cases.append((f"写一个{zh}", f"write {en}", "match"))
    # 负样本·语言冲突 C vs C++（16）
    for _sid, zh, _en in SUBJ:
        cases.append((f"用 C 实现{zh}", f"用 C++ 实现{zh}", ("reject", "language_conflict")))
    # 负样本·意图冲突 定义 vs 实现（16）
    for _sid, zh, _en in SUBJ:
        cases.append((f"{zh}是什么", f"实现一个{zh}", ("reject", "intent_conflict")))
    # 负样本·操作冲突 插入 vs 删除（8）
    for _sid, zh in OP_SUBJ:
        cases.append((f"用 Python 实现{zh}插入", f"用 Python 实现{zh}删除", ("reject", "operation_conflict")))
    # 负样本·约束冲突 无约束 vs 线程安全（16）
    for _sid, zh, _en in SUBJ:
        cases.append((f"实现一个{zh}", f"实现一个线程安全的{zh}", ("reject", "constraint_conflict")))
    # 负样本·主题边界（E1 及相似概念对，≥8）
    for (q, c) in [
        ("实现一棵二叉树", "实现一棵二叉搜索树"),
        ("实现二叉搜索树", "实现红黑树"),
        ("写一个快排", "写一个归并排序"),
        ("写一个快速排序", "写一个冒泡排序"),
        ("实现一个插入排序", "实现一个选择排序"),
        ("实现一个链表", "实现一个队列"),
        ("实现一个栈", "实现一个哈希表"),
        ("实现二叉搜索树", "实现一棵二叉树"),
    ]:
        cases.append((q, c, ("reject", "subject_conflict")))
    return cases


CASES = _expand()
