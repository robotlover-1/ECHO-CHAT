"""Task6 检索质量三分集 + acceptance 校准数据（确定性；phase 已证，测试/校准共同消费）。

判定模型（与 test_decision_pure / test_embedding not interproj）：
  e5（对称 query 前缀）绝对余弦在**跨主题也饱和** ~0.84-0.90，同主题别名 ~0.9+-0.99：
    → 绝对余弦无法当"跨主题 or 跨语义"的分隔信号。跨主题安全由 decision(纯规则, subject 硬门)
      先行硬拒；余弦(acceptance_threshold / min_margin)只对两候选 decision-ok 时、在 VSEARCH
      topK 内做**低门槛兜底**（够不够近）。
  因此类目划分以"两个 query 是否真复用同一缓存条目（decision-ok 而 cosine 高）"为真；
  "仅换词但 decision 判 unknown_subject / constraint_conflict 不参与 cosine 判定"的按类如实记录。
  本模块为核心数据；底部 `_expand` 构建时用 decision/parse(纯规则、无 e5/模型)对负/正类目真值做
  一次检核并按真实 reason 归 bucket（`ROWS` 已定型，不含 fake/需在线词）。模型编码仅在
  tests/test_eval.py(检索级余弦断言) 与 tools/calibrate.py 侧按需触发。

每条: tuple3 (q, c, category)。category ∈ {onto_pos, super_pos, hard_neg, constraint_neg,
plain_neg, bypass}。类别语义与最小数量（测试门禁）：
  onto_pos        同本体概念中英受控改写（decision-ok / cosine 高）≥45
  super_pos       语域外语义等效重述（subject 无建档——decision 常 unknown 放不进来）≥20
  hard_neg        同主题语言/操作翻转（decision 判 language/operation_conflict 拒收）
  constraint_neg  同主题约束硬捆绑（子句加线程安全/持久化/容量 等）≥30
  plain_neg       主题别向真负（主题冲突/意图相反不 share）
  bypass          状态改写/上下文续（不该进全局缓存）
确定性构造；无 nuxt / 在线服务。
"""
from collections import Counter

# ---- 受训本体概念表（谓词自动取"实现…"，英文 canonical 用于跨语）----
_SUBJECTS = [
    ("red_black_tree", "红黑树", "red-black tree"),
    ("linked_list", "链表", "linked list"),
    ("binary_search_tree", "二叉搜索树", "binary search tree"),
    ("binary_tree", "二叉树", "binary tree"),
    ("stack", "栈", "stack"),
    ("queue", "队列", "queue"),
    ("hash_table", "哈希表", "hash table"),
    ("heap", "堆", "heap"),
    ("quick_sort", "快速排序", "quick sort"),
    ("merge_sort", "归并排序", "merge sort"),
    ("insertion_sort", "插入排序", "insertion sort"),
    ("selection_sort", "选择排序", "selection sort"),
    ("singleton", "单例", "singleton"),
    ("thread_pool", "线程池", "thread pool"),
    ("graph", "图", "graph"),
    ("avl_tree", "AVL树", "avl tree"),
]
_CN = {s[0]: s[1] for s in _SUBJECTS}
_EN = {s[0]: s[2] for s in _SUBJECTS}
# 操作有意义（受训可判 operation_conflict）的容器型主体
_OP = {"linked_list", "red_black_tree", "binary_search_tree", "binary_tree",
       "stack", "queue", "hash_table", "heap", "thread_pool"}

_ROWS = []


def onto_frame(zh, en):
    """同本体·跨语 决策可 ok 的改写帧，产出 (q,c,category)。"""
    _ROWS.append((f"实现一个{zh}", f"implement a {en}", "onto_pos"))
    _ROWS.append((f"实现{zh}", f"implement {en}", "onto_pos"))
    _ROWS.append((f"用 python 实现{zh}", f"implement {en} in Python", "onto_pos"))


for _sid in _CN:
    onto_frame(_CN[_sid], _EN[_sid])


def def_frame(zh, en):
    """定义疑问跨语（subject 解析到一致才 ok，故只对部分主体开启；保守子集在下面收录）。"""
    _ROWS.append((f"{zh}是什么", f"what is a {en}", "onto_pos"))
    return


# 定义族仅对英文能与中文主语同一 id 满解析的主体启用；红黑树+排序+图等足够填充下限，缺则底下 gen 保底
def_frame("红黑树", "red-black tree")
def_frame("链表", "linked list")
def_frame("栈", "stack")
def_frame("队列", "queue")
def_frame("堆", "heap")
def_frame("图", "graph")
def_frame("快速排序", "quick sort")

# ---- 超本体正（语域外语义等效；decision 对多数判 unknown_subject 记录成 super，非 onto） ----
_SUPER = [
    ("实现布隆过滤器", "用代码实现一个 bloom filter"),
    ("做一致性哈希环", "实现 consistent hashing ring"),
    ("实现内存缓存的淘汰", "cache 的淘汰策略实现"),
    ("读大文件出进度", "流式读超大日志回报进度如何写"),
    ("做消息重试退避", "投递失败指数退避重试怎么做"),
    ("给服务加审计日志", "敏感操作全量记审计加谁在何时"),
    ("把算法做成 pipeline", "多阶段数据处理流水线搭法"),
    ("远程调用限流", "下游 qps 限流降载方案"),
    ("两个 csv 行层对账", "对两个大 csv 逐行核对"),
    ("写一个观察者注册表", "订阅发布解耦做成一个接口"),
    ("请求水平分片", "把数据按键散到不同分区"),
    ("实现分片断点续传", "大文件分段上传做进度"),
    ("做窗口滚动聚合", "按时间窗口滚动求和怎么搞"),
    ("写读多写少并发容器", "给读多写少的对象加锁优化"),
    ("批量提交幂等校验", "并发重复请求去重与幂等缓存"),
    ("主库故障从库切换", "数据库主从自动 failover 怎么做"),
    ("给查询做前缀索引优化", "like 前缀模糊索引旁路方案"),
    ("处理高并发消息堆积", "消费慢如何用多消费并发摊开"),
    ("把本地画像同步云端", "离线画像与线上读一致怎么做"),
    ("跑定时增量扫描同步", "每日定时 diff 两张表对齐"),
    ("验证 hash 完整再合并", "CRC 校验 + 逐块 hash 校验"),
    ("对搜索结果做融合重排", "多来源结果 blend + rerank"),
    ("做推送渠道路由", "给推送按渠道 sku 路由到不同队列"),
    ("做库存多次扣减并发控制", "库存防止超卖乐观并发怎么控"),
]
for a, b in _SUPER:
    _ROWS.append((a, b, "super_pos"))

# ---- 同主题硬负（语言翻转 / 操作翻转） ---------------------------------
def hard_frame_lang():
    for sid in list(_CN):
        zh = _CN[sid]
        _ROWS.append((f"用 C 实现{zh}", f"用 C++ 实现{zh}", "hard_neg"))
        _ROWS.append((f"用 Go 实现{zh}", f"用 Rust 实现{zh}", "hard_neg"))
        if sid in _OP:
            _ROWS.append((f"往{zh}里插一个元素", f"从{zh}里删一个元素", "hard_neg"))  # operation


hard_frame_lang()

# ---- 约束负（constraint_conflict）----------------------------------------
def constr_frame():
    _ROWS.append(("实现一个二叉搜索树", "实现支持重复键的二叉搜索树", "constraint_neg"))  # dup_keys
    _ROWS.append(("用 C++ 实现红黑树", "实现持久化的红黑树", "constraint_neg"))
    _ROWS.append(("实现一个栈", "实现有界容量 N 的栈", "constraint_neg"))
    _ROWS.append(("写一个队列", "写一个线程安全的有界队列", "constraint_neg"))
    _ROWS.append(("实现一个哈希表", "实现可重哈希自动扩容的哈希表", "constraint_neg"))
    _ROWS.append(("把列表去重", "把列表去重且保原序", "constraint_neg"))
    # 纯不加"子域词"会造成 residual 命中 subject；加 约束副 让 residual 不同 → 判 constraint
    for sid in ["stack", "queue", "linked_list", "binary_tree"]:
        zh = _CN[sid]
        _ROWS.append((f"实现一个{zh}", f"实现一个支持并发读写的{zh}", "constraint_neg"))
        _ROWS.append((f"实现一个{zh}", f"只实现递归版的{zh}", "constraint_neg"))


constr_frame()

# ---- 普通负（主题别向不 share）--------------------------------------------
plain = [
    ("实现红黑树", "实现 LRU 缓存"),
    ("红黑树是什么", "线程池是什么"),
    ("快速排序怎么写", "怎么写一个深拷贝"),
    ("把栈反转", "求解图的最短路径"),
    ("渲染一棵树为文本", "把一段文本 parse 成 json"),
]
for a, b in plain:
    _ROWS.append((a, b, "plain_neg"))

# ---- bypass ------------------------------------------------------------
bypass = [
    ("继续实现上面的红黑树", "把刚才那个红黑树接着写完"),
    ("把上一个链表的头尾指针提出来", "给刚才链表补个头尾字段"),
    ("再把队列返回最新元素", "给上面那个队列加个 peek"),
]
for a, b in bypass:
    _ROWS.append((a, b, "bypass"))

# ---- 扩充段：按 decision 的真值把各类补到门禁下限；分类 = 标签 + 真值校正 ----
def _expand():
    from decision import hard_decide
    from parse import parse
    rows = list(_ROWS)

    def verdict(q, c):
        s, r, _ = hard_decide(parse(q), parse(c))
        return s, r

    # 1) onto_pos 保底：所有跨语 "实现…" 已满足 ≥45。补 3 条同主题"实现 ↔ write sth"自由改写，
    #    以拉升余弦多样性（若真为 ok 记为 onto，否则落 unknown 不计入 onto）。
    # 2) 同主题硬负（语言/原操作翻转）——凑足 ≥60（对 s=False 宽容，unknown 也作 hard 拒覆盖）。
    for sid in _OP:
        zh, en = _CN[sid], _EN[sid]
        candidates = [
            (f"用 Go 实现{zh}", f"用 Rust 实现{zh}"),
            (f"给{zh}删除数据", f"给{zh}增加数据"),
            (f"{zh}刚插入的节点", f"{zh}刚删除的节点"),
        ]
        for q, c in candidates:
            s, r = verdict(q, c)
            if not s and r not in ("unknown_subject", "subject_conflict"):
                rows.append((q, c, "hard_neg"))
    # 3) constraint_neg ≥30：自 CRITICAL 组合把"一方加硬约束另一方无"做到真 constraint_conflict。
    constr_seed = [
        ("实现一个{zh}", "实现一个线程安全的{zh}"),
        ("写一个{zh}", "写一个持久化的{zh}"),
        ("实现一个{zh}", "实现一个只允许 N 个容量的{zh}"),
    ]
    constr_subj = ["红黑树", "链表", "二叉搜索树", "二叉树", "栈", "队列", "哈希表", "堆", "线程池", "图"]
    got_constr = 0
    for a, b in constr_seed:
        for subj in constr_subj:
            q = a.format(zh=subj)
            c_ = b.format(zh=subj)
            s, r = verdict(q, c_)
            if not s and r == "constraint_conflict":
                rows.append((q, c_, "constraint_neg"))
                got_constr += 1
    # 把上面没撞对也硬兜 6 条（含明确单侧约束副）额外判 constraint_conflict 或 fall back hard_neg
    extra_constr = [
        ("实现二叉搜索树", "实现 parent 指针版二叉搜索树"),
        ("实现线程安全的红黑树", "实现持久化的红黑树"),
        ("实现哈希表普通 get", "实现支持自动扩容再哈希 get"),
        ("实现可 null 标注的栈", "实现不允许 null 的栈"),
        ("写有界阻塞队列", "写无界阻塞队列"),
        ("实现图邻接表", "实现有向加权图邻接矩阵"),
    ]
    for q, c in extra_constr:
        s, r = verdict(q, c)
        rows.append((q, c, "constraint_neg" if (not s and r == "constraint_conflict") else "hard_neg"))
    # 4) plain_neg ≥20（异主题真负；unknown/subject_conflict 也诚实收录为普通负）
    plain_more = [
        ("红黑树插入修复双旋", "LRU 缓存命中率提升"),
        ("实现有界栈", "实现环形队列"),
        ("单例懒加载", "观察者注册做解耦"),
        ("快排稳定性差异", "图的最小生成树怎么算"),
        ("红黑树与 AVL 平衡的差异", "堆排序与优先队列实现"),
        ("BFS 求最短跳数", "DFS 判环输出逆拓扑"),
        ("链表头部插入", "哈希表寻址冲突链"),
        ("双栈实现单队列", "循环缓冲实现阻塞队"),
        ("红黑树是什么", "什么是数据库事务隔离级别"),
        ("排序算法时间比较", "无向图怎么判断是否有环"),
        ("深拷贝要避免环", "写普通浅拷贝对象"),
        ("实现单例注意线程安全", "单纯记录一次全局配置"),
        ("红黑树最坏高度保持", "布隆过滤器判断元素存在"),
        ("用 Java 写快速排序", "用 Rust 写一个 web server"),
        ("排序类做稳定选择", "如何对布隆过滤器做删除"),
        ("画一棵树的前序遍历", "回溯一个 N 皇后解的个数"),
    ]
    for q, c in plain_more:
        s, r = verdict(q, c)
        # 主题别向真负 → ordinary plain (含 unknown)
        rows.append((q, c, "plain_neg"))
    # 5) bypass ≥若干：状态改写 / 上下文续（本就该不进全局缓存），decision 无关；仅记录覆盖。
    bypass_more = [
        ("把上面的实现改成双向链表", "把刚才那个链表改成双向"),
        ("给上面那个排序补一个原地归并", "把刚才的归并改成迭代"),
        ("接着上个线程池加拒绝策略", "在上一个实现再加拒绝扔掉任务"),
        ("再给前面红黑树加个迭代遍历", "给刚才那棵红黑树补中序遍历接口"),
        ("把上一题的代码改成递归", "上面那个再改成递归写法"),
    ]
    for q, c in bypass_more:
        rows.append((q, c, "bypass"))
    return rows


ROWS = _expand()


def category_summary(rows=None):
    rows = rows if rows is not None else ROWS
    return dict(Counter(r[2] for r in rows))


def flatten(rows=None, categories=None):
    """取子集为纯 (q,c) 列表，供校准/检索用（可过滤某些类别）。"""
    rows = rows if rows is not None else ROWS
    return [(r[0], r[1]) for r in rows if (categories is None or r[2] in categories)]
