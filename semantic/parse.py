"""parse.py —— 槽位抽取（semantic Phase1 Task2）

从一阶问句里结构化抽取 主题/意图/语言/操作/上下文标志，供给式 parse()。
两个核心接口：
    normalize(text) -> str                      归一化（NFKC/小写/符号保护/空白）
    parse(text) -> ParsedQuery   槽位结果数据类

纯逻辑模块，不 import nuxt/ontology 之外的外部服务。
接口契约（Task 1 已交付）：
    from ontology import lookup_subject_id, ONTOLOGY_VERSION
    lookup_subject_id(text): 全句最长别名命中（latin 词边界 / 中文受控子串）。

Task 3 才接入 residual_words / fingerprint / fingerprint_eligible；本任务 parse()
对这三者一律置空/False。
"""
from dataclasses import dataclass
import re
import unicodedata
import jieba  # noqa: F401  —— residual 词面统计在 Task3 使用，此处保留 import

from ontology import lookup_subject_id, ONTOLOGY_VERSION

PARSER_VERSION = "v1"

# fmt: off
# ============ 主题句式（另有新增中英句式见 _SUBJECT_EN，均在 _extract_subject_pair 汇总） ============
SUBJECT_PATTERNS = [
    r"^什么是(.+?)[？?]?$",
    r"^(.+?)是什么[？?]?$",
    r"^(.+)指的是什么[？?]?$",
    r"^(.+)是什么意思[？?]?$",
    r"^请介绍一下(.+?)[？?]?$",
    r"^实现(?:一个)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^写(?:一个|出)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^生成(?:一个)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^构建(?:一个)?(?:简单|简易|完整|基础)?(?:版本的)?(.+?)[。！!?？]?$",
    r"^之前实现过(.+?)吗[？?]?$",
    r"^以前写过(.+?)吗[？?]?$",
    r"^(.+?)怎么(写|实现)[？?]?$",              # "冒泡排序怎么写/怎么实现" → 冒泡排序
    r"^怎么(?:写|实现)(.+?)[。！!?？]?$",          # "怎么写冒泡排序" → 冒泡排序
    r"^(.+?)的(插入|删除|遍历|查询|添加|更新)[。！!?？]?$",  # "红黑树的插入" → 红黑树
    r"^用.+?(?:写|实现|生成|构建)(.+?)[。！!?？]?$",  # "用Go实现红黑树" → 红黑树（语言另由 extract_language 处理）
]

# 新增中英句式：英文定义/实现句式。theme 捕获组统一放 group(1)。
_EN = {
    "definition": [r"^what is(?: an?| the)? (.+?)[?]?$", r"^define (.+?)[.]?$",
                   r"^explain(?: the concept of)? (.+?)[?]?$", r"^what does (.+?) (?:mean|do)[?]?$"],
    "implementation": [r"^(?:implement|write|build|create|code) (?:an? |the )?(.+?)[.]?$",
                       r"^implement (.+?) in (.+?)$",            # group2=语言
                       r"^write (.+?) using (.+?)$",
                       r"^(.+?) (?:implementation|implementation in) (.+?)$"],
}
# 英文主题句式扁平化用于主题抽取：全部取 group(1) 为主题。
SUBJECT_EN_PATTERNS = [pat for pats in _EN.values() for pat in pats]


def normalize_subject(subject):
    subject = re.sub(r"^(一个|简单的|简易的|完整的|基础的|版本的)+", "", subject)
    # 剥尾部后缀/操作词：写一个红黑树的实现 → 红黑树；红黑树的删除 → 红黑树。
    # 否则 SUBJECT:红黑树的实现 与 SUBJECT:红黑树 标记桶不匹配、余弦被压低。
    # 删除/插入等操作区分由 rerank 的 extract_operation 单独负责。
    subject = re.sub(r"(的实现(方法|方式)?|的代码|的方法|的程序|的demo|的示例|的删除|的插入|的遍历|的查询|的添加|的更新)$", "", subject)
    return subject.strip()


def _extract_subject_pair(text):
    """双路主题：先跑原中文单条主题句式、再跑新增中英句式；返回 (subject_text, subject_id)。

    subject_text=首个句式命中主题，否则 None；
    subject_id = lookup(subject_text) 或 全体归一化文本兜底 lookup。
    """
    n = re.sub(r"\s+", "", text.strip().lower())
    subject_text = None
    for p in SUBJECT_PATTERNS:
        m = re.match(p, n)
        if m:
            subject_text = normalize_subject(m.group(1).strip())
            break
    if subject_text is None:
        # 英文句式在保留空格的文本上匹配（原中文句式已在上面无空格 n 上跑过）
        for p in SUBJECT_EN_PATTERNS:
            m = re.match(p, text.strip())
            if m:
                subject_text = normalize_subject(m.group(1).strip())
                break
    subject_id = lookup_subject_id(subject_text) if subject_text else None
    if subject_id is None:
        subject_id = lookup_subject_id(n)
    return subject_text, subject_id


# ============ 意图分类（definition 组排在 implementation 之前，兼顾英文定义不误撞实现） ============
# 中文沿用 semantic.py 句式语义；追加"使用/编写/用X写…"等 → implementation 的句式，以及英文触发词。
# 各 intent 的 cn 词集（在无空格文本上搜索）：
INTENT_CN = {
    "definition":   [r"是什么", r"什么是", r"什么意思", r"介绍一下", r"概念", r"定义"],
    "comparison":   [r"区别", r"差异", r"对比", r"哪个好", r"和.*区别"],
    "reason":       [r"为什么", r"原因", r"为何"],
    "troubleshooting": [r"报错", r"错误", r"异常", r"失败", r"怎么解决", r"无法"],
    "implementation":  [r"实现", r"写一个", r"写出", r"给.*代码", r"代码示例", r"完整代码",
                        r"生成", r"构建", r"做一个", r"使用", r"编写",
                        r"^(?:用|使用).{0,16}?(?:写|编写|实现|生成|构建|做)"],
    "operation":       [r"删除", r"插入", r"添加", r"遍历", r"查询", r"怎么(增|删|改|查|插)"],
    "state_update":    [r"记住", r"以后.*回答", r"后面.*会问", r"从现在开始", r"你是一个",
                        r"你是一名", r"扮演", r"我的名字是"],
    "history_query":   [r"之前.*过.*吗", r"以前.*过.*吗", r"刚才.*过.*吗", r"是否.*过",
                        r"还记得", r"上次", r"前面"],
}
# 各 intent 的英文触发词（在保留空格文本上搜索，词边界分隔）。
INTENT_EN = {
    "definition":     [r"what is", r"what are", r"what does", r"what do", r"\bdefine",
                       r"\bexplain(?!.*implement)", r"\bmeaning\b", r"\bdefinition\b"],
    "comparison":     [r"\bdifference\b", r"\bcompare\b", r"\bversus\b", r"\bvs\b", r"\bcontrast\b"],
    "reason":         [r"\bwhy\b", r"\breason\b"],
    "troubleshooting":[r"\berror\b", r"\bexception\b", r"\bfailed\b", r"\bfail\b", r"unable to"],
    "implementation": [r"\bimplement", r"\bwrite\b", r"\bbuild\b", r"\bcreate\b", r"\bcode\b"],
    "operation":      [r"\binsert(?:ion)?\b", r"\bdelete\b", r"\bremove\b", r"\btraverse", r"\bsearch\b", r"\bupdate\b"],
}

# 判定优先级：definition→comparison→reason→troubleshooting→implementation→operation→state_update→history_query
_INTENT_ORDER = ["definition", "comparison", "reason", "troubleshooting", "implementation",
                 "operation", "state_update", "history_query"]


def extract_intent(text):
    """规则型意图分类：definition/comparison/reason/troubleshooting/implementation/operation/
    state_update/history_query/unknown。

    definition 组先于 implementation，故中文/英文定义句不会误撞 implementation。
    中文词在无空格文本上搜索；英文词在原保留空格文本上搜索（词边界）。
    """
    n = re.sub(r"\s+", "", text.lower())
    for intent in _INTENT_ORDER:
        if any(re.search(p, n) for p in INTENT_CN[intent]):
            return intent
        if any(re.search(p, text) for p in INTENT_EN.get(intent, [])):
            return intent
    return "unknown"


CONTEXT_PATTERNS = [
    r"之前", r"刚才", r"前面", r"上次", r"上一", r"继续", r"还是",
    r"再改", r"刚刚", r"还记得", r"基于前", r"按照前", r"接着",
]


def is_context_dependent(text):
    """依赖当前会话上下文的问题（之前/刚才/继续…）→ 不适合全局语义缓存。"""
    n = re.sub(r"\s+", "", text.lower())
    return any(re.search(p, n) for p in CONTEXT_PATTERNS)


# 状态修改指令：会改变会话状态（身份/偏好/记忆/输出规则）→ 不可缓存、不可命中缓存。
# 这类内容即使 Embedding 再强也会与"无状态问答"混淆（你是一个工程师 vs 你是一个艺术家），
# 必须在准入层直接绕过。
STATEFUL_PATTERNS = [
    # 记忆与偏好
    r"记住", r"请记得", r"不要忘记", r"以后.*回答", r"后面.*会问", r"接下来.*会问",
    # 角色与身份设置
    r"你是一个", r"你是一名", r"你现在是", r"从现在开始", r"扮演", r"作为.*回答", r"假设你是",
    # 用户信息设置
    r"我是一个", r"我是一名", r"我的名字是", r"我的职业是", r"我的偏好是",
    # 输出规则设置
    r"以后都", r"接下来都", r"后续都", r"回答时要", r"回答不要", r"统一使用",
]


def is_stateful_instruction(text):
    n = re.sub(r"\s+", "", text.strip().lower())
    return any(re.search(p, n) for p in STATEFUL_PATTERNS)


def should_bypass_semantic_cache(text):
    """缓存准入：上下文依赖 或 状态修改指令 → 不查不写全局语义缓存。"""
    return is_context_dependent(text) or is_stateful_instruction(text)


# ============ 语言抽取：长模式优先，避免 c 吞 c++ / java 吞 javascript ============
# CJK 属 Unicode \w，故词界一律用 ASCII 邻接 lookaround（对中英粘连"用Python"同样生效）。
# 顺序即优先级；命中 ≥2 个不同语言视为歧义 → None。
_LK = lambda body: "(?<![A-Za-z0-9])" + body + "(?![A-Za-z0-9])"
LANG_PATTERNS = [
    ("cpp",      re.compile(r"(?<![A-Za-z0-9+#:\.])c\+\+(?![A-Za-z0-9])|" + _LK(r"cxx") + r"|" + _LK(r"cpp") + r"|" + _LK(r"CPP"))),
    ("csharp",   re.compile(_LK(r"c") + r"#(?![A-Za-z0-9])|" + _LK(r"csharp") + r"|" + _LK(r"C#"))),
    ("dotnet",   re.compile(r"(?<![A-Za-z0-9])\.net(?![A-Za-z0-9])")),
    ("javascript", re.compile(_LK(r"javascript"))),
    ("node",     re.compile(r"node\.js|" + _LK(r"nodejs") + r"|" + _LK(r"node"))),
    ("typescript", re.compile(_LK(r"typescript"))),
    ("java",     re.compile(_LK(r"java(?!script)"))),
    ("python",   re.compile(_LK(r"python") + r"|" + _LK(r"\bpy\b"))),
    ("rust",     re.compile(_LK(r"rust"))),
    ("golang",   re.compile(_LK(r"golang"))),
    ("go",       re.compile(_LK(r"go"))),
]
# `c` 单独特殊：仅命中"c语言"或紧跟实现语境才判（去空白后匹配，"使用 C 编写 rbtree"）。
_C_IMPL_CTX = re.compile(r"(?<![a-z0-9+#])c(?![a-z0-9+#])(?=.*(实现|编写|生成|写|编程|代码|implementation))")


def extract_language(text):
    """抽取实现目标语言：cpp/csharp/dotnet/javascript/node/typescript/java/python/rust/golang/go/c；
    长模式优先；命中多个不同语言返回 None。"""
    n = re.sub(r"\s+", "", text.lower())
    hits = set()
    for lang, pat in LANG_PATTERNS:
        if pat.search(n):
            hits.add(lang)
    if "c语言" in n or _C_IMPL_CTX.search(n):
        hits.add("c")
    if len(hits) == 1:
        return next(iter(hits))
    return None


# ============ 操作抽取：中文→英文 id 映射 + 英文触发词 ============
# 无信息模板词：嵌入权重 0。注意"实现/简单"等是意图/主题词，不在此列（结构化保存）。
STOP_WORDS = {
    "一个", "一下", "请问", "帮我", "可以", "简单地", "请", "能否", "让我", "帮我",
    # 通用疑问/模板词：无内容信息，去掉避免"怎么减肥 vs 怎么学好英语"这类跨主题误命中
    "怎么", "如何", "为什么", "为何", "推荐", "哪些", "多少", "怎样", "为啥",
    "怎么办", "是不是", "有没有", "是否", "什么", "是", "吗", "呢", "吧", "啊",
    "应该", "需要", "想要", "请问一下", "一下",
}

OP_ZH2ID = {
    "插入": "insert", "添加": "insert", "插入节点": "insert",
    "删除": "delete", "移除": "delete",
    "遍历": "traverse",
    "查询": "find", "查找": "find", "搜索": "find",
    "更新": "update", "修改": "update",
    "替换": "replace",
}
# 英文操作词 → 规范 id（专有词按优先级在中文之后扫描）
OP_EN = [
    ("insert", [r"\binsert(?:ion)?s?\b"]),
    ("delete", [r"\bdelete\b|\bdeletion\b|deleting"]),
    ("traverse", [r"\btraverse|\btraversal"]),
    ("find", [r"\bfind\b|\bsearch\b|\blookup\b"]),
    ("update", [r"\bupdate\b|\bmodify\b"]),
    ("remove", [r"\bremove\b|\bremoval\b"]),   # → delete 需容器语境，见 extract_operation
    ("replace", [r"\breplace\b"]),
]
# remove → delete 仅限容器语境（按 subject_id）放宽，否则保守忽略。
_DELETE_CONTAINER_SUBJECTS = {"array", "linked_list", "vector"}


def extract_operation(text, subject_id=None):
    """抽取具体操作（返回规范英文 id：insert/delete/traverse/find/update/replace）；
    守旧不贪：英文 remove/removal 仅当 subject_id 为容器（array/linked_list/vector）才归 delete。"""
    n = re.sub(r"\s+", "", text.lower())
    # 中文优先（最长条目数序固定，先精确节词亦可）
    terms = sorted(OP_ZH2ID, key=len, reverse=True)
    for op_zh in terms:
        if op_zh in n:
            return OP_ZH2ID[op_zh]
    for op_id, pats in OP_EN:
        for p in pats:
            if re.search(p, n):
                if op_id == "remove":
                    if subject_id in _DELETE_CONTAINER_SUBJECTS:
                        return "delete"
                    continue  # 非容器语境保守忽略
                return op_id
    return None


def normalize(text):
    """归一化问句：NFKC 统一、小写、c++/c#/.net/node.js 符号占位保护、统一空白。"""
    s = unicodedata.normalize("NFKC", text)
    s = s.lower()
    # 保留 c++/c#/.net 等符号：先保护占位再转半角
    for sym in ["c++", "c#", ".net", "node.js"]:
        s = s.replace(sym, sym.replace("+", "P").replace("#", "H").replace(".", "D"))
    # 全角→半角已在 NFKC 阶段完成；这里仅统一空白与常见全角标点
    s = re.sub(r"[\s　]+", " ", s)
    for sym in ["c++", "c#", ".net", "node.js"]:
        s = s.replace(sym.replace("+", "P").replace("#", "H").replace(".", "D"), sym)
    return s.strip()


@dataclass(frozen=True)
class ParsedQuery:
    raw_text: str
    normalized_text: str
    subject_text: "Optional[str]"
    subject_id: "Optional[str]"
    intent: str
    language: "Optional[str]"
    operation: "Optional[str]"
    output_type: "Optional[str]"
    residual_words: "frozenset"
    context_dependent: bool
    stateful: bool
    bypass_cache: bool
    fingerprint: object
    fingerprint_eligible: bool
    parser_version: str
    ontology_version: str
    def __post_init__(self):
        # residual_words 恒为可散列集合；fingerprint 本任务应已是 None
        object.__setattr__(self, "residual_words", frozenset(self.residual_words))


def output_type_for(intent):
    """意图→输出类型：实现/排障给代码，定义/对比/原因给讲解。"""
    if intent in {"implementation", "troubleshooting"}:
        return "code"
    if intent in {"definition", "comparison", "reason"}:
        return "explanation"
    return None


def parse(text):
    """槽位抽取主入口：normalize → 双路 subject → intent/language/operation → output_type
    → context/stateful/bypass（对原文本）→ 返回 ParsedQuery。

    residual_words/fingerprint/fingerprint_eligible 本任务先置空（Task 3 补）。
    """
    raw = text if isinstance(text, str) else str(text)
    normalized_text = normalize(raw)
    raw_lower = raw.strip()
    subject_text, subject_id = _extract_subject_pair(normalized_text)
    intent = extract_intent(normalized_text)
    language = extract_language(normalized_text)
    operation = extract_operation(normalized_text, subject_id)
    context_dependent = is_context_dependent(raw_lower)
    stateful = is_stateful_instruction(raw_lower)

    return ParsedQuery(
        raw_text=raw,
        normalized_text=normalized_text,
        subject_text=subject_text,
        subject_id=subject_id,
        intent=intent,
        language=language,
        operation=operation,
        output_type=output_type_for(intent),
        residual_words=frozenset(),
        context_dependent=context_dependent,
        stateful=stateful,
        bypass_cache=context_dependent or stateful,
        fingerprint=None,          # Task 3 接入语义指纹
        fingerprint_eligible=False,  # Task 3 计算
        parser_version=PARSER_VERSION,
        ontology_version=ONTOLOGY_VERSION,
    )
