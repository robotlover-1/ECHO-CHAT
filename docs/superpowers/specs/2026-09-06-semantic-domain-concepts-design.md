# ECHO-CHAT 语义检索 · 三类设计领域本体概念扩展（电气 / 机械 / 嵌入式软件）设计文档

- 日期：2026-09-06
- 状态：已批准（brainstorming 四节逐节确认：组织方案=方案 A；概念清单=起手草案；工程/测试=§2/§3；边界与增量=§4）
- 位置：ECHO-CHAT `semantic/`（`ontology/concepts.json` 扩容 + validator 轻校验 + eval 验收集 + README）
- 关联：`docs/superpowers/specs/2026-09-04-semantic-term-recognition-design.md`（术语实体化机制，本设计复用其识别口径）、`2026-09-04-semantic-phase2-3-e5-hybrid-design.md`（e5 向量 + decision 纯规则 + 指纹）

> **定位**：本阶段是**语义本体扩容**，不改识别/判定机制。只为制造公司三类工程师（电气设计、机械设计、嵌入式软件）的高频问句补 `subject` 本体概念与别名，让 `/embed` 能把它们解析出 subject_id，从而进入既有缓存命中（VSEARCH + decision + 指纹）流程。**跨领域是否共享缓存答案仍只由 subject_id 决定**（同 id 即共享，异 id 即拒）；新增的 `group` 标注**永不进运行时判定**，仅作维护归组/测试归类。

## 背景与目标

ECHO-CHAT 是制造业公司内部的问答助手：电气/机械设计工程师与嵌入式软件工程师遇到问题问 AI，很多人问题相似 → 语义缓存把"改写等价"的问句命中共享，省 token 且更快。

现状本体 40 个概念全部是 CS（红黑树/线程池/std::vector…），三类工程问句解析不出 subject_id → 无法复用缓存。目标：**先配置三类领域最常用的本体概念**，让高频同类问句可被识别并安全去重；内容后续按问答日志增量扩展。

识别机制要点（既有，本文复用、不改动）：
- subject = 全句**最长别名命中 + 中文句式**提取；命中后从残差中擦除（见 `parse.py`）；
- 命中硬门：subject_id 同 **且** language/operation/intent 同 **且** 残差词相等（见 `decision.py`）→ 主题即"会被反复整问的主题"，粒度决定安全与命中率；
- 别名全库跨概念**必须唯一**（validator 内置，冲突启动即抛）→ 三个领域新增概念若撞别名会立刻暴露；
- 语义指纹带 `ontology_version`，版本升档后旧指纹成孤儿、不跨版混用。

## 概念组织约定（方案 A）

1. **单文件扩容**：继续使用 `ontology/concepts.json`，现有 40 条概念不动、不加字段。
2. **id 领域前缀**：新概念 id 以 `elec_` / `mech_` / `emb_` 开头，便于维护与防撞（沿用现有 snake_case 英文 id 风格）。
3. **`group` 标注（纯组织性）**：新概念带可选 `group ∈ {electrical, mechanical, embedded}`。parser/decision **完全忽略**该字段；只用于人类维护归组与测试分类。意义：既给"电机选型"这类跨域高频题留了单一 id 归属（运行时两域工程师可互中），又保留将来按域筛概念的抓手。不做"顶层 domain 判定维度"。
4. **`ontology_version` 升档**：`2026-09-03.1 → 2026-09-06.1`。指纹 payload 已带版本 → 旧指纹自动孤儿，**无需清库**、无跨版污染。
5. **粒度原则**：概念 = "会被反复整问的主题"，别名覆盖工程师真实打字说法（中文规范名 + 简称 + 英文缩写）。歧义/跨域单字裸别名刻意不用短语化写法规避误吸（详见下表 ⚠ 注）。

## 起手概念集（草案，可增删）

三类各 ~10-12 条"最常用"起手。标注：
- ⚠ = 评审时已明确**刻意取舍**的点（别名宽窄/归属/合并），首期按现写法落地，后续按问答日志再调；
- 别名**全库唯一**、可子串重叠（靠最长匹配分派），如"电气图纸"(elec_schematic) 与"图纸"(mech_drawing) 可并存。

### electrical（group=electrical）
| id（canonical_zh） | 别名（起手） |
|---|---|
| elec_schematic（原理图） | 原理图, 电气原理图, 电路图, schematic, schematics, circuit diagram |
| elec_harness（线束） | 线束, 线束设计, 线束布置, wiring harness, harness ⚠线束/接线是否合并待定 |
| elec_contactor（接触器） | 接触器, 交流接触器, contactor, ac contactor |
| elec_relay（继电器） | 继电器, 中间继电器, relay, relays |
| elec_breaker（断路器） | 断路器, 空气开关, 空开, circuit breaker, breaker, mcb |
| elec_motor（电机） | 电机, 电动机, 马达, motor, electric motor ⚠机械域亦问电机；识别归此，运行时不隔离 |
| elec_plc（PLC） | plc, 可编程逻辑控制器, 可编程控制器 |
| elec_inverter（变频器） | 变频器, vfd, variable frequency drive |
| elec_transformer（变压器） | 变压器, transformer |
| elec_fuse（熔断器） | 熔断器, 保险丝, fuse |
| elec_emc（电磁兼容） | 电磁兼容, emc |
| elec_grounding（接地） | 接地, 保护接地, 屏蔽接地, grounding |

### mechanical（group=mechanical）
| id（canonical_zh） | 别名（起手） |
|---|---|
| mech_structure（结构件） | 结构件, 结构设计, 机械结构, 结构 ⚠裸"结构"可能误吸，观察后删 |
| mech_modeling（三维建模） | 三维建模, 3d建模, 三维模型, 3d模型 |
| mech_drawing（工程图） | 工程图, 出图, 二维图, 图纸, engineering drawing ⚠"图纸"可出现在电气句，靠最长匹配归此 |
| mech_tolerance（公差） | 公差, 尺寸公差, 形位公差, 公差配合, tolerance |
| mech_fastener（紧固件） | 螺栓, 螺钉, 螺母, 紧固件, 标准件, bolt, screw ⚠紧固件/标准件是否合一待定 |
| mech_bearing（轴承） | 轴承, bearing, bearings |
| mech_transmission（传动） | 传动, 传动比, 皮带传动, 链传动, 齿轮, transmission, gear |
| mech_material（材料选用） | 材料选型, 材料选用, 选材, material selection ⚠不含裸"材料"防误吸 |
| mech_sheet_metal（钣金） | 钣金, 钣金件, sheet metal |
| mech_shaft（转轴） | 转轴, 传动轴, 输出轴, shaft ⚠不含裸"轴"（与轴承/坐标轴抢） |

### embedded（group=embedded）
| id（canonical_zh） | 别名（起手） |
|---|---|
| emb_mcu（单片机） | 单片机, mcu, 微控制器, stm32 |
| emb_rtos（RTOS） | rtos, freertos, rt-thread, 实时操作系统, 实时系统 |
| emb_firmware（固件） | 固件, 固件开发, 固件升级, firmware |
| emb_driver（驱动开发） | 驱动开发, 设备驱动, 驱动程序, device driver ⚠不含裸"驱动"（电机驱动/LED驱动跨域） |
| emb_interrupt（中断） | 中断, 中断服务函数, 中断处理, 中断嵌套, interrupt, isr |
| emb_timer（定时器） | 定时器, 定时器中断, timer |
| emb_serial（串口） | 串口, uart, rs232, 串口通信, serial ⚠rs485 是否并入待定 |
| emb_i2c（I2C） | i2c, iic |
| emb_spi（SPI） | spi |
| emb_can（CAN 总线） | can, can总线, can通信, can bus |
| emb_watchdog（看门狗） | 看门狗, watchdog |
| emb_bootloader（引导程序） | bootloader, 引导程序, 启动流程 |

> 上表为设计批准的首期内容。落码时逐条核对与既有 40 概念的别名唯一性（validator 会兜底）；对 ⚠ 取舍点在实现 PR 里保持与表一致，不作为隐含变更。

## 工程改动

| 文件 | 改动 |
|---|---|
| `semantic/ontology/concepts.json` | ① `ontology_version` → `2026-09-06.1`；② 概念条目加可选 `group` 键（旧 40 条不加）；③ 追加上表三类 ~34 条概念（id 前缀 + group 标注） |
| `semantic/ontology/validator.py` | 轻校验：`group` 若出现须 ∈ 白名单 `{electrical, mechanical, embedded}`；id 带领域前缀的概念其 `group` 必须一致（防漏标/标错）。既有断言（别名全局唯一、≥40 下限、禁单字符拉丁别名）不动 |
| `semantic/ontology/loader.py` | 预期零改动；先确认 `group` 额外键被容忍（loader 按需取字段） |
| `semantic/parse.py` / `semantic/decision.py` | 零改动（subject 识别为通用最长别名匹配，新概念自动入现有流程） |
| `semantic/README.md` | "术语识别"一节补领域分组说明（group/前缀/跨域共享口径/增量 SOP） |
| Go 编排 / VSEARCH / 指纹 schema | 零改动 |

## 测试与验证

在 `semantic/tests/eval/term_entity_cases.py` 按既有四段补充，逐条走完整 parse：
- **RECOGNIZE**：每领域 ≥3 条，断言命中目标 id，例如 `("接触器怎么选型","elec_contactor")`、`("FreeRTOS 任务优先级怎么调","emb_rtos")`、`("齿轮齿数怎么定","mech_transmission")`；
- **MISS**（防误吸，重点）：跨域/裸别名陷阱句不得串主题，例如 `("电机座结构设计","mech_structure")`、`("外壳要不要接地","elec_grounding")`、`("电机驱动的 IGBT 怎么选","elec_contactor" 不得命中 → 见实现取实际语义)`；
- **REJECT_PAIRS / 共享 OK 对**：跨域不共享（`接触器选型` vs `轴承选型`）与域内改写共享（`接触器怎么选型` ↔ `如何选型接触器`）双向断言；
- **FP_ELIGIBLE_SAFE**：新概念属 `alias_of` → 补 1 例断言 `fingerprint_eligible=True`、可走 fp 快路径。

**回归门**：新增概念后 `pytest` 全绿（含既有 40 概念的全部用例）——证明扩容不破坏旧行为；`ontology_version` 升档使旧指纹自动孤儿（数据不删，仅不命中）。

## 边界与非目标

- 不加顶层 `domain` 判定字段——跨域是否共享仅看 subject_id，`group` 永不进运行时；
- 不改 parse/decision/embedding/指纹 schema/Go 编排/VSEARCH；
- 不做"文档/知识库语料侧"的领域分类或检索（本仓库无此系统）；
- 概念内容不追求全量——本次为"常用起手集"，后续按问答日志迭代。

## 上线与后续增量（SOP）

- 本体在 semantic **进程启动时加载** → 合入后需**重启 semantic 服务**方生效；版本升档使旧指纹自动孤儿，无需清库。
- 后续加概念走 **eval-first**（写入 README）：
  1. 从公司问答日志抽高频问句；
  2. 先在 `term_entity_cases.py` 补 RECOGNIZE（期望命中）+ MISS（防回归）；
  3. 再向 `concepts.json` 加概念/别名；
  4. `validator.py` + `pytest` 通过后提交。
- group 白名单集中于 validator 一处，后续新增领域只需扩白名单 + 定新 id 前缀。
