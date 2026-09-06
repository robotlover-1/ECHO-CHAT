# 三类设计领域本体概念扩展 实现计划（电气 / 机械 / 嵌入式软件）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 向 `semantic/ontology/concepts.json` 追加 34 个三类工程领域 subject 概念（含 `group` 标注与领域前缀 id），使 `/embed` 能识别这三类问句进入语义缓存去重；同步升档 `ontology_version`、加 group 校验、补 eval 验收集、更新 README。

**Architecture:** 纯**本体数据扩容** + 两处小代码改动。subject 识别为通用最长别名匹配，新概念写入 JSON 即自动进入现有 parse→decision→指纹链路；`group` 字段与领域前缀 id 只服务维护与校验，**永不进运行时判定**。已用沙箱（/tmp/sembox 独立 ontology 副本）对全部将落盘的 eval 行做过 parse 探针，本计划中的期望真值（subject_id/language/eligible/reason）均为实测结果，非猜测。

**Tech Stack:** Python 3.8、pytest（从 `semantic/` 目录运行）、jieba 分词、无新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-06-semantic-domain-concepts-design.md`（commit `82a6a97`/`9b09971`/`98509b8`）。

## Global Constraints

- 工作树 = `tmp/t1/ECHO-CHAT`（git 仓库，`main`）。仓库里有与本任务无关的未提交改动（`openai-api-proxy/dev.config.yaml`、未跟踪 `kvstore/`）——**只暂存/提交本任务改的文件，绝不 `git add -A`**。
- 全部命令在 `semantic/` 下跑 pytest：`python3 -m pytest <test> -q`。
- `concepts.json` schema：每条须有非空 `canonical_zh`/`canonical_en`/`aliases`；id 全局唯一；别名经 `folding_variants` 展开后跨概念必须唯一（validator 启动即校验）；禁单字符拉丁别名；`concepts` 总数下限 40。
- 别名数组与 id 用本计划给的值**逐字复制**——沙箱已验证全局唯一（74 概念无碰撞）。
- `ontology_version` 三处同步：`concepts.json` 顶层字段、`ontology/loader.py` 的 `ONTOLOGY_VERSION` 常量、`tests/test_ontology.py::test_loader_loads_version` 断言串。
- `group ∈ {electrical, mechanical, embedded}`；`elec_/mech_/emb_` 前缀 id 必须带对应 group（Task 2 后由 validator 强制）。
- **不改** `parse.py` / `decision.py` / `embedding.py` / 指纹 schema / Go 编排 / VSEARCH。
- eval 各段**只增不减**，既有行与既有 floor 断言不动。

---

### Task 1: 升档 ontology_version（JSON + loader 常量 + 测试断言）

**Files:**
- Modify: `semantic/ontology/concepts.json`（顶层 `ontology_version` 字段，约第 3 行）
- Modify: `semantic/ontology/loader.py:5`
- Modify: `semantic/tests/test_ontology.py:9`

**Interfaces:**
- Produces: 常量 `ONTOLOGY_VERSION == "2026-09-06.1"`（parse 指纹 payload、`/embed` 的 `ontology_version` 字段都来自它）。旧指纹携带 `2026-09-03.1` → 升档后自然孤儿、不跨版混用，无需清库。

- [ ] **Step 1: 改 `loader.py` 常量**

`semantic/ontology/loader.py` 第 5 行改为：
```python
ONTOLOGY_VERSION = "2026-09-06.1"
```

- [ ] **Step 2: 改 `concepts.json` 顶层版本字段**

`semantic/ontology/concepts.json` 第 3 行 `"ontology_version": "2026-09-03.1"` → `"ontology_version": "2026-09-06.1"`（与 loader 常量保持一致，`schema` 仍为 `"v1"`）。

- [ ] **Step 3: 改测试断言**

`semantic/tests/test_ontology.py` 第 9 行：
```python
    assert d["concepts"] and ONTOLOGY_VERSION == "2026-09-06.1"
```

- [ ] **Step 4: 跑测试确认绿**

Run:
```bash
cd semantic && python3 -m pytest tests/test_ontology.py::test_loader_loads_version tests/test_fingerprint.py -q
```
Expected: PASS（fingerprint 测试用相对漂移校验，版本换值不影响其断言）。

- [ ] **Step 5: 提交**

```bash
git add semantic/ontology/concepts.json semantic/ontology/loader.py semantic/tests/test_ontology.py
git commit -m "chore(semantic): ontology_version 2026-09-03.1→2026-09-06.1 三处同步(JSON/loader常量/测试断言)——为新增领域概念做准备, 旧指纹孤儿无需清库"
```

---

### Task 2: validator 增加 group / 领域前缀一致性校验（含单元测试）

**Files:**
- Modify: `semantic/ontology/validator.py`
- Modify: `semantic/tests/test_ontology.py`

**Interfaces:**
- Produces: `validator.GROUP_ALLOW`（`frozenset[str]`）、`validator.validate_groups(concepts: list[dict]) -> None`（违规抛 `AssertionError`）。`validate()` 末尾调用之。后续 Task 3-5 新增概念依赖此函数在**启动时**把漏标 group / 标错 group 拦下。

- [ ] **Step 1: 写失败测试**

在 `semantic/tests/test_ontology.py` 追加：
```python
from ontology.validator import validate_groups
import pytest

def _g(cid, group):
    return {"id": cid, "canonical_zh": "x", "canonical_en": "x",
            "aliases": ["x"], "group": group}

def test_group_valid():
    validate_groups([_g("elec_x", "electrical"),
                     _g("mech_x", "mechanical"),
                     _g("emb_x", "embedded"),
                     {"id": "legacy", "canonical_zh": "y", "canonical_en": "y", "aliases": ["y"]}])  # 旧概念无 group 放行

def test_group_illegal_value():
    with pytest.raises(AssertionError):
        validate_groups([_g("elec_x", "civil")])

def test_group_prefix_mismatch():
    with pytest.raises(AssertionError):
        validate_groups([_g("elec_x", "mechanical")])
    with pytest.raises(AssertionError):
        validate_groups([_g("emb_x", "embedded")]) is None or validate_groups([{"id": "elec_y", "canonical_zh": "z", "canonical_en": "z", "aliases": ["z"]}])
    with pytest.raises(AssertionError):  # 领域前缀 id 漏标 group 也要拦
        validate_groups([{"id": "mech_y", "canonical_zh": "z", "canonical_en": "z", "aliases": ["z"]}])

def test_group_unknown_prefix_legacy_ok():
    # 既有 CS id 无前缀无 group → 放行；带前缀但非领域前缀且无 group → 放行
    validate_groups([{"id": "red_black_tree", "canonical_zh": "r", "canonical_en": "r", "aliases": ["r"]}])
```
> 注：`test_group_prefix_mismatch` 里第一个子句 `... is None or validate_groups(...)` 写法绕口——改写为两个独立 `with pytest.raises` 块（elec 标 mechanical 抛错；elec 前缀漏 group 抛错），见下实现后保持一致。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py -q`
Expected: FAIL with `ImportError: cannot import name 'validate_groups'`（函数尚不存在）。

- [ ] **Step 3: 实现 group 校验**

在 `semantic/ontology/validator.py` 顶部（`validate()` 前）加：
```python
GROUP_ALLOW = frozenset({"electrical", "mechanical", "embedded"})
_PREFIX_GROUP = {"elec_": "electrical", "mech_": "mechanical", "emb_": "embedded"}


def validate_groups(concepts):
    """group 标注校验：值须在白名单；带领域前缀 id 须带且匹配对应 group。
    旧 CS 概念（无前缀无 group）不受影响。违规抛 AssertionError（启动即失败）。"""
    for c in concepts:
        cid = c["id"]
        g = c.get("group")
        if g is not None and g not in GROUP_ALLOW:
            raise AssertionError(f"concept {cid}: illegal group {g!r} (∈{sorted(GROUP_ALLOW)})")
        for prefix, expect in _PREFIX_GROUP.items():
            if cid.startswith(prefix) and g != expect:
                raise AssertionError(
                    f"concept {cid}: prefix {prefix!r} requires group={expect!r}, got {g!r}")
```

在 `validate()` 的 `assert len(ids) >= 40` 之后追加一行：
```python
    validate_groups(data["concepts"])
```

- [ ] **Step 4: 把测试里绕口的断言改干净并重跑**

把 `test_group_prefix_mismatch` 改为四个独立断言块：
```python
def test_group_prefix_mismatch():
    with pytest.raises(AssertionError):
        validate_groups([_g("elec_x", "mechanical")])          # 前缀 vs group 不一致
    with pytest.raises(AssertionError):
        validate_groups([{"id": "elec_y", "canonical_zh": "z", "canonical_en": "z", "aliases": ["z"]}])  # 前缀漏 group
    with pytest.raises(AssertionError):
        validate_groups([_g("emb_x", "electrical")])           # 前缀 vs group 不一致
    with pytest.raises(AssertionError):
        validate_groups([{"id": "mech_y", "canonical_zh": "z", "canonical_en": "z", "aliases": ["z"]}])  # 前缀漏 group
```

Run: `cd semantic && python3 -m pytest tests/test_ontology.py -q`
Expected: PASS（全部，含既有 lookup/别名唯一性用例）。

- [ ] **Step 5: 提交**

```bash
git add semantic/ontology/validator.py semantic/tests/test_ontology.py
git commit -m "feat(semantic/ontology): validator group 校验(group白名单+领域前缀一致性)——为新域概念漏标/标错提供启动即失败"
```

---

### Task 3: 电气概念 + 电气/通用 eval 行

**Files:**
- Modify: `semantic/ontology/concepts.json`（追加 12 条 electrical 概念）
- Modify: `semantic/tests/eval/term_entity_cases.py`（RECOGNIZE + MISS + FP_ELIGIBLE_SAFE 增行）

**Interfaces:**
- Consumes: `ontology_version="2026-09-06.1"`（Task 1）、`validate_groups`（Task 2）。
- Produces: `elec_*` 概念 id（`elec_schematic/elec_harness/elec_contactor/elec_relay/elec_breaker/elec_motor/elec_plc/elec_inverter/elec_transformer/elec_fuse/elec_emc/elec_grounding`），供 Task 4 的 REJECT 跨域对与后续任务复用。

- [ ] **Step 1: 写失败 eval 行（TDD 红）**

在 `semantic/tests/eval/term_entity_cases.py`：

RECOGNIZE 列表（段 ①）末尾追加 4 行（第 4 行是跨域防串守卫的通用形态，放 Task 4 用 mech 实现，此处只加电气 3 行 + 一条接地）：
```python
    # ---- 领域：electrical（制造公司电气设计；别名/ID 见 2026-09-06 design §起手概念集）----
    ("接触器怎么选型", "elec_contactor", None, "domain electrical"),
    ("断路器整定电流怎么算", "elec_breaker", None, "domain electrical"),
    ("变频器的载波频率怎么设", "elec_inverter", None, "domain electrical"),
    ("外壳怎么接地", "elec_grounding", None, "domain electrical"),
```

MISS 列表（段 ②）末尾追加 3 行（通用未建档短语冻结，reason 均为实测 `subject_unresolved`）：
```python
    # ---- 领域通用 MISS：公司裸词未建档/无概念命中 → 不入缓存 ----
    ("这个螺丝拧不动怎么办", "subject_unresolved"),
    ("为什么经常烧保险", "subject_unresolved"),          # “保险丝”才会识别 elec_fuse；“烧保险”不含“丝”→ None
    ("这个按钮按了没反应", "subject_unresolved"),
```

FP_ELIGIBLE_SAFE 列表（段 ④）末尾追加 1 行（实测 `intent=definition`、残差空 → eligible True，概念直命安全）：
```python
    ("什么是接触器", True),
```
> 「什么是中断」的 FP 行**不在本任务**——`emb_interrupt` 概念于 Task 5 才加入，此阶段无该概念 → 必 False。该 FP 行随 emb 概念在 Task 5 Step 1 一并追加（见 Task 5）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_eval.py::test_term_recognize_subject_language tests/test_eval.py::test_term_miss_subject_none_and_not_eligible tests/test_eval.py::test_term_fp_eligible_flags -q`
Expected: FAIL——新增 RECOGNIZE 行解析 subject_id=None（概念尚不存在）；FP 行 `什么是接触器` 目前 subject None → eligible False ≠ True。

- [ ] **Step 3: 追加 electrical 概念到 concepts.json**

在 `semantic/ontology/concepts.json` 的 `concepts` 数组最后一个对象（`restful`）的 `}` 后加逗号，然后追加以下 12 条（保持 JSON 合法，字段顺序不敏感，`group` 在后）：
```json
    {"id": "elec_schematic", "canonical_zh": "原理图", "canonical_en": "schematic", "aliases": ["原理图", "电气原理图", "电路图", "schematic", "schematics", "circuit diagram"], "group": "electrical"},
    {"id": "elec_harness", "canonical_zh": "线束", "canonical_en": "wiring harness", "aliases": ["线束", "线束设计", "线束布置", "wiring harness", "harness"], "group": "electrical"},
    {"id": "elec_contactor", "canonical_zh": "接触器", "canonical_en": "contactor", "aliases": ["接触器", "交流接触器", "contactor", "ac contactor"], "group": "electrical"},
    {"id": "elec_relay", "canonical_zh": "继电器", "canonical_en": "relay", "aliases": ["继电器", "中间继电器", "relay", "relays"], "group": "electrical"},
    {"id": "elec_breaker", "canonical_zh": "断路器", "canonical_en": "circuit breaker", "aliases": ["断路器", "空气开关", "空开", "circuit breaker", "breaker", "mcb"], "group": "electrical"},
    {"id": "elec_motor", "canonical_zh": "电机", "canonical_en": "electric motor", "aliases": ["电机", "电动机", "马达", "motor", "electric motor"], "group": "electrical"},
    {"id": "elec_plc", "canonical_zh": "PLC", "canonical_en": "programmable logic controller", "aliases": ["plc", "可编程逻辑控制器", "可编程控制器"], "group": "electrical"},
    {"id": "elec_inverter", "canonical_zh": "变频器", "canonical_en": "frequency inverter", "aliases": ["变频器", "vfd", "variable frequency drive"], "group": "electrical"},
    {"id": "elec_transformer", "canonical_zh": "变压器", "canonical_en": "transformer", "aliases": ["变压器", "transformer"], "group": "electrical"},
    {"id": "elec_fuse", "canonical_zh": "熔断器", "canonical_en": "fuse", "aliases": ["熔断器", "保险丝", "fuse"], "group": "electrical"},
    {"id": "elec_emc", "canonical_zh": "电磁兼容", "canonical_en": "electromagnetic compatibility", "aliases": ["电磁兼容", "emc"], "group": "electrical"},
    {"id": "elec_grounding", "canonical_zh": "接地", "canonical_en": "grounding", "aliases": ["接地", "保护接地", "屏蔽接地", "grounding"], "group": "electrical"}
```

- [ ] **Step 4: 跑测试确认绿**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py tests/test_eval.py::test_term_recognize_subject_language tests/test_eval.py::test_term_miss_subject_none_and_not_eligible tests/test_eval.py::test_term_fp_eligible_flags tests/test_eval.py::test_tasks5_section_floors -q`
Expected: PASS（ontology 启动校验含新 12 概念、group 校验、四段新增行全部按实测真值通过）。

- [ ] **Step 5: 提交**

```bash
git add semantic/ontology/concepts.json semantic/tests/eval/term_entity_cases.py
git commit -m "feat(semantic): 电气设计域 12 概念(elec_*+group) + RECOGNIZE/MISS/FP eval 行(实测真值锁定)"
```

---

### Task 4: 机械概念 + 机械 eval 行 + 电气-机械跨域 REJECT 对

**Files:**
- Modify: `semantic/ontology/concepts.json`（追加 10 条 mechanical）
- Modify: `semantic/tests/eval/term_entity_cases.py`（RECOGNIZE + REJECT_PAIRS 增行）

**Interfaces:**
- Consumes: `elec_contactor`（Task 3）。
- Produces: `mech_*` 概念 id（`mech_structure/mech_modeling/mech_drawing/mech_tolerance/mech_fastener/mech_bearing/mech_transmission/mech_material/mech_sheet_metal/mech_shaft`）。

- [ ] **Step 1: 写失败 eval 行（TDD 红）**

RECOGNIZE 列表末尾追加 4 行（含跨域防串守卫行：含裸"电机"但命中最长别名"结构设计"→ mech_structure，实测）：
```python
    # ---- 领域：mechanical（制造公司机械设计）----
    ("轴承的游隙怎么选", "mech_bearing", None, "domain mechanical"),
    ("齿轮的模数怎么确定", "mech_transmission", None, "domain mechanical"),
    ("这个零件的尺寸公差怎么标", "mech_tolerance", None, "domain mechanical"),
    ("电机座结构设计", "mech_structure", None, "longest-match 守卫: 含裸'电机'(elec_motor)但'结构设计'更长"),
```

REJECT_PAIRS 列表（段 ③）末尾追加 1 行（电气 vs 机械，各自可识别、组合必须拒）：
```python
    ("接触器怎么选型", "轴承怎么选型"),          # elec_contactor vs mech_bearing → subject 硬拒
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_eval.py::test_term_recognize_subject_language tests/test_eval.py::test_term_reject_pairs_decision_blocked -q`
Expected: FAIL——新增 RECOGNIZE 行 subject_id=None（mech 概念未加）。REJECT 对目前 `接触器怎么选型` 已可识别、`轴承怎么选型` 未识别 → 也判 shared False（本步可能不红），红点由 RECOGNIZE 保证。

- [ ] **Step 3: 追加 mechanical 概念到 concepts.json**

在 electrical 块末尾（上一步加的最后一条 `elec_grounding` 的 `}`）后加逗号，追加：
```json
    {"id": "mech_structure", "canonical_zh": "结构件", "canonical_en": "mechanical structure", "aliases": ["结构件", "结构设计", "机械结构", "结构"], "group": "mechanical"},
    {"id": "mech_modeling", "canonical_zh": "三维建模", "canonical_en": "3d modeling", "aliases": ["三维建模", "3d建模", "三维模型", "3d模型"], "group": "mechanical"},
    {"id": "mech_drawing", "canonical_zh": "工程图", "canonical_en": "engineering drawing", "aliases": ["工程图", "出图", "二维图", "图纸", "engineering drawing"], "group": "mechanical"},
    {"id": "mech_tolerance", "canonical_zh": "公差", "canonical_en": "tolerance", "aliases": ["公差", "尺寸公差", "形位公差", "公差配合", "tolerance"], "group": "mechanical"},
    {"id": "mech_fastener", "canonical_zh": "紧固件", "canonical_en": "fastener", "aliases": ["螺栓", "螺钉", "螺母", "紧固件", "标准件", "bolt", "screw"], "group": "mechanical"},
    {"id": "mech_bearing", "canonical_zh": "轴承", "canonical_en": "bearing", "aliases": ["轴承", "bearing", "bearings"], "group": "mechanical"},
    {"id": "mech_transmission", "canonical_zh": "传动", "canonical_en": "transmission", "aliases": ["传动", "传动比", "皮带传动", "链传动", "齿轮", "transmission", "gear"], "group": "mechanical"},
    {"id": "mech_material", "canonical_zh": "材料选用", "canonical_en": "material selection", "aliases": ["材料选型", "材料选用", "选材", "material selection"], "group": "mechanical"},
    {"id": "mech_sheet_metal", "canonical_zh": "钣金", "canonical_en": "sheet metal", "aliases": ["钣金", "钣金件", "sheet metal"], "group": "mechanical"},
    {"id": "mech_shaft", "canonical_zh": "转轴", "canonical_en": "shaft", "aliases": ["转轴", "传动轴", "输出轴", "shaft"], "group": "mechanical"}
```

- [ ] **Step 4: 跑测试确认绿**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py tests/test_eval.py::test_term_recognize_subject_language tests/test_eval.py::test_term_reject_pairs_decision_blocked -q`
Expected: PASS。REJECT 对由 `subject_conflict` 拦下（两个 subject 现都可识别）。

- [ ] **Step 5: 提交**

```bash
git add semantic/ontology/concepts.json semantic/tests/eval/term_entity_cases.py
git commit -m "feat(semantic): 机械设计域 10 概念(mech_*+group) + RECOGNIZE(含跨域防串守卫)/电气-机械 REJECT 对"
```

---

### Task 5: 嵌入式软件概念 + 嵌入式 eval 行 + 域内与新-CS REJECT 对

**Files:**
- Modify: `semantic/ontology/concepts.json`（追加 12 条 embedded）
- Modify: `semantic/tests/eval/term_entity_cases.py`（RECOGNIZE + REJECT_PAIRS 增行）

**Interfaces:**
- Consumes: `elec_contactor`（Task 3）、`red_black_tree`（既有 CS 概念）。
- Produces: `emb_*` 概念 id（`emb_mcu/emb_rtos/emb_firmware/emb_driver/emb_interrupt/emb_timer/emb_serial/emb_i2c/emb_spi/emb_can/emb_watchdog/emb_bootloader`）。任务末 `concepts` 总数 74。

- [ ] **Step 1: 写失败 eval 行（TDD 红）**

RECOGNIZE 列表末尾追加 4 行（全实测）：
```python
    # ---- 领域：embedded（嵌入式软件工程师）----
    ("FreeRTOS 任务优先级怎么调", "emb_rtos", None, "domain embedded"),
    ("中断嵌套怎么处理", "emb_interrupt", None, "domain embedded"),
    ("CAN 总线波特率怎么配", "emb_can", None, "domain embedded"),
    ("看门狗溢出时间怎么设置", "emb_watchdog", None, "domain embedded"),
```

REJECT_PAIRS 列表末尾追加 2 行（域内不同主题 + 新概念 vs 既有 CS 概念）：
```python
    ("FreeRTOS 任务优先级怎么调", "中断嵌套怎么处理"),   # 同域不同主题(emb_rtos vs emb_interrupt) → 硬拒
    ("红黑树的插入复杂度", "接触器选型额定电流"),          # 既有 CS(red_black_tree) vs 新域(elec_contactor) → 硬拒
```

FP_ELIGIBLE_SAFE 列表（段 ④）末尾追加 1 行——`emb_interrupt` 现已存在，实测 `intent=definition`、残差空 → eligible True（从 Task 3 挪入，见 Task 3 注）：
```python
    ("什么是中断", True),
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd semantic && python3 -m pytest tests/test_eval.py::test_term_recognize_subject_language tests/test_eval.py::test_term_reject_pairs_decision_blocked -q`
Expected: FAIL——新增 RECOGNIZE 行 subject_id=None（emb 概念未加）。

- [ ] **Step 3: 追加 embedded 概念到 concepts.json**

在 mechanical 块末尾（`mech_shaft` 的 `}`）后加逗号，追加：
```json
    {"id": "emb_mcu", "canonical_zh": "单片机", "canonical_en": "microcontroller", "aliases": ["单片机", "mcu", "微控制器", "stm32"], "group": "embedded"},
    {"id": "emb_rtos", "canonical_zh": "实时操作系统", "canonical_en": "real-time operating system", "aliases": ["rtos", "freertos", "rt-thread", "实时操作系统", "实时系统"], "group": "embedded"},
    {"id": "emb_firmware", "canonical_zh": "固件", "canonical_en": "firmware", "aliases": ["固件", "固件开发", "固件升级", "firmware"], "group": "embedded"},
    {"id": "emb_driver", "canonical_zh": "驱动开发", "canonical_en": "device driver", "aliases": ["驱动开发", "设备驱动", "驱动程序", "device driver"], "group": "embedded"},
    {"id": "emb_interrupt", "canonical_zh": "中断", "canonical_en": "interrupt", "aliases": ["中断", "中断服务函数", "中断处理", "中断嵌套", "interrupt", "isr"], "group": "embedded"},
    {"id": "emb_timer", "canonical_zh": "定时器", "canonical_en": "timer", "aliases": ["定时器", "定时器中断", "timer"], "group": "embedded"},
    {"id": "emb_serial", "canonical_zh": "串口", "canonical_en": "serial communication", "aliases": ["串口", "uart", "rs232", "串口通信", "serial"], "group": "embedded"},
    {"id": "emb_i2c", "canonical_zh": "I2C", "canonical_en": "i2c bus", "aliases": ["i2c", "iic"], "group": "embedded"},
    {"id": "emb_spi", "canonical_zh": "SPI", "canonical_en": "spi bus", "aliases": ["spi"], "group": "embedded"},
    {"id": "emb_can", "canonical_zh": "CAN总线", "canonical_en": "can bus", "aliases": ["can", "can总线", "can通信", "can bus"], "group": "embedded"},
    {"id": "emb_watchdog", "canonical_zh": "看门狗", "canonical_en": "watchdog", "aliases": ["看门狗", "watchdog"], "group": "embedded"},
    {"id": "emb_bootloader", "canonical_zh": "引导程序", "canonical_en": "bootloader", "aliases": ["bootloader", "引导程序", "启动流程"], "group": "embedded"}
```

- [ ] **Step 4: 跑测试确认绿**

Run: `cd semantic && python3 -m pytest tests/test_ontology.py tests/test_eval.py -q`
Expected: PASS（含三段新增 RECOGNIZE、两个 REJECT 对、Task3 MISS/FP 行；`test_category_floors`/`test_tasks5_section_floors`/`test_retrieval_cases_floors` 不变仍绿）。

- [ ] **Step 5: 提交**

```bash
git add semantic/ontology/concepts.json semantic/tests/eval/term_entity_cases.py
git commit -m "feat(semantic): 嵌入式软件域 12 概念(emb_*+group) + RECOGNIZE/域内与新CS REJECT 对; concepts 40→74"
```

---

### Task 6: README 文档同步 + 全量回归 + 收尾提交

**Files:**
- Modify: `semantic/README.md`

**Interfaces:**
- Consumes: 已完成 Task 1-5（74 概念、group 校验、eval 行）。

- [ ] **Step 1: 更新 README「术语识别」一节**

在 `semantic/README.md` 的「三层主体」小节之后（「**唯一性/多主题**」之前）插入一段：
```markdown
**领域分组（制造业三类设计域，2026-09-06）：** `ontology/concepts.json` 追加三类工程 subject 概念（id 带领域前缀 `elec_`/`mech_`/`emb_`，条目带 `group ∈ {electrical, mechanical, embedded}` 标注）。`group` **仅供维护归组/测试归类，永不进运行时判定**——跨域问句能否共享缓存只由 subject_id 决定（如"电机选型"电气/机械工程师同问同一 id 可互中）。validator 强制：group 值须在白名单、领域前缀 id 须带对应 group（漏标/标错启动即失败）。别名全库跨概念唯一（启动校验）。升级概念用 **eval-first**：先在 `tests/eval/term_entity_cases.py` 补 RECOGNIZE（期望命中）+ MISS（防回归），再加 `concepts.json`，`validator.py`+`pytest` 过即合。新增领域只需扩 validator 白名单 + 定新前缀。
```

- [ ] **Step 2: 跑全量语义测试（回归门）**

Run:
```bash
cd semantic && python3 -m pytest tests/ -q
```
Expected: 全绿。若个别 embedding/model 用例因本机模型/资源原因跳过或超时，记录并确认其与本体改动无关（改动只影响 `ontology/`+`tests/eval/term_entity_cases.py`），主回归以 `tests/test_ontology.py` + `tests/test_eval.py` + `tests/test_parse.py` + `tests/test_decision.py` 全绿为准。

- [ ] **Step 3: 一致性抽查（/embed 手工冒烟，可选但推荐）**

Run:
```bash
cd semantic && SEMANTIC_INTRA_OP=1 python3 - <<'PY'
from parse import parse
for t in ["接触器怎么选型", "电机座结构设计", "FreeRTOS 任务优先级怎么调", "什么是接触器"]:
    q = parse(t)
    print(t, "→", q.subject_id, "eligible=", q.fingerprint_eligible)
PY
```
Expected: 依次 `elec_contactor` / `mech_structure` / `emb_rtos` / `elec_contactor(True)`。

- [ ] **Step 4: 提交 README**

```bash
git add semantic/README.md
git commit -m "docs(semantic): README 术语识别补领域分组说明(group/前缀/跨域共享口径/eval-first 增量 SOP)"
```

- [ ] **Step 5: 复核 git 状态与提交序列**

Run:
```bash
git log --oneline -7
git status --short
```
Expected: 最近 6 个提交依次为 Task 1-6 的 feat/chore/docs；`git status` 只剩任务开始前就存在的无关改动（`openai-api-proxy/dev.config.yaml` 修改、`kvstore` 未跟踪），本任务无残留。

---

## Self-Review（规划自检）

**Spec 覆盖** → 逐任务对账：
- 概念组织约定（方案 A：单文件 + group + 领域前缀 + 版本升档）→ Task 1/3/4/5；
- validator group 白名单 + 前缀一致性 → Task 2；
- RECOGNIZE（每域 ≥2 + 跨域防串守卫行）→ Task 3/4/5；
- MISS（仅 None 冻结）→ Task 3（螺丝/保险/按钮，reason=subject_unresolved 实测）；
- REJECT_PAIRS（跨域、域内不同主题、新 vs 既有 CS）→ Task 4/5；
- FP_ELIGIBLE_SAFE（新概念 eligible True 1+ 例）→ Task 3（`什么是接触器`/`什么是中断`，实测 True）；
- README 领域分组 + eval-first SOP → Task 6；
- 非目标（不动 parse/decision/顶层 domain）→ 全程零改动，Global Constraints 声明；
- 增量 eval-first SOP 落文档 → Task 6 Step 1（README 段落含 SOP 文案）。

**占位扫描**：无 TBD/TODO/“见实现取语义”；每条新概念与 eval 行的具体内容与期望真值均写入（沙箱实测）。MISS 的 `subject_unresolved` reason、FP True、REJECT 拒因均来自 /tmp/sembox parse 探针。

**类型/命名一致性**：概念 id、group 取值、别名串在 Task 3-5 的 JSON 与 eval 行间逐字一致（同一来源数组生成）；`validate_groups`/`GROUP_ALLOW` 签名在 Task 2 定义、Task 3-5 经 validator 全量调用覆盖；`ONTOLOGY_VERSION` 三处同步值一致。

**已知边界**：Task 2 Step 1 测试草稿含一段绕口断言，Step 3 后 Step 4 立即改写为四个独立断言块——执行时以 Step 4 版本为准。
