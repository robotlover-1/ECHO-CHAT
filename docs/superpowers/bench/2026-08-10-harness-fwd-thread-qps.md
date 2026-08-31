# harness 转发方法 QPS 调查全记录：从 63k 到 97k 的每一步

> 日期：2026-08-10/11。对应计划 `2026-08-10-harness-fwd-thread-qps`。
> 目的：完整记录从 README 08-04 旧数据（none 63k / sync 30.8k / ebpf 28k）到当前数据（none 97k / sync 82k / ebpf 96k）的**每一步实验、优化、测量假象及其修正**。

## 0. 起点：README 08-04 的 eBPF 对比表


| Payload |   none |   sync |   ebpf | sync/none | ebpf/none |
| ------- | -----: | -----: | -----: | --------: | --------: |
| 64B     | 63,044 | 30,838 | 28,092 |   −51.1% |   −55.4% |
| 512B    | 63,424 | 30,882 | 28,070 |   −51.3% |   −55.7% |

结论曾写"ebpf < sync < none"。**本调查证明：这两个数字（63k 基线 + 30.8k/28k 转发）被两个测量假象严重污染。**

## 1. 两个测量假象（逐步定位）

### 假象 A：`-r N` 键范围跟随命令数，哈希表随 N 膨胀

**现象**：ebpf/sync 的 QPS 随 `-n`（总命令数）增大而下降（89k@50w → 61k@200w）；P=4 高 N 时 sync 反而超过 none（假象）。

**定位过程**：

- 怀疑转发路径容量 → 排除：ringbuf 4MB/64MB 相同（QPS 不变）、proxy CPU0 空闲（24%）、拷贝/输出量无悬崖、softirq 相同。
- perf 直接 profile（编译匹配 6.1.176 内核的 perf）：ebpf 的 fexit ringbuf 提交每次 **self-IPI（`native_write_msr`）占 master CPU2 25-27%** → master 饱和；`reactor_conn_read`（含 ht_hset）随 N 从 0.93µs 涨到 3.5µs。
- **验证**：`--keyrange 100000` 固定键范围后，ebpf 稳定 96-102k（不再随 N 掉）；P=4 的 "sync > none" 也消失（sync < none）。

**结论**：`-r N = -n N` 让哈希表随运行规模增长，打在 CPU 饱和的模式上 → 制造了 "ebpf 随 N 掉" 和 "sync > none" 两个假象。**修：`--keyrange` 固定键范围。**

### 假象 B：客户端实际与 master 同核（README 声称的 `taskset 3` 未生效）

**现象**：同机器上当前 none=97k，而 README 的 none=63k（用户指出机器没变、63k 可复现）。

**定位过程**：README 方法写"`taskset 3` 固定 redis-benchmark"，但旧 harness 用 popen 拉 redis-benchmark，子进程继承 `--cpu 2` 亲和 → **客户端实际在 CPU2，和 master 抢核**。

**验证**：把 harness 的 `CLIENT_CPU` 从 3 改成 2（客户端和 master 同核），none 从 97k 回落到 **66-70k ≈ README 的 63k**。

**结论**：none 的 63k→97k = 客户端从"和 master 抢 CPU2"变成"独立核 CPU3"。README 声称的方法论（客户端隔离核）直到本调查的 harness 才真正实现。

## 2. 优化历程（每一步的动机、改动、before/after 实测、验证）

> 除标注外，数值为跨机 P=1、同一次会话内对比。早期阶段在旧 harness（50 线程 per-conn）上，后期在单线程 reactor harness 上。

### 步骤 1：起点（README 08-04，旧 harness 单核内联）

- **动机**：理解 README 的 "sync 49%、ebpf 44%" 从哪来。
- **现象**：旧 harness 50 线程 per-conn，全部 pin CPU2，转发在 handler 内联。
- **基线**：none 63k、sync 30.8k（49%）、ebpf 28k（44%）。

### 步骤 2：多核隔离（T4）——转发/proxy/client 分核

- **动机**：怀疑单核 CPU2 争抢是慢的主因（50 handler + client + 转发 + proxy 全挤 CPU2）。
- **改动**：转发线程 pin CPU0、ebpf_proxy pin CPU0、客户端 pin CPU3（fork/exec 子进程亲和）、master 留 CPU2。
- **before → after**（跨机 P=1）：sync 49%→**54%**、ebpf 31.5%→**52%**。
- **验证**：D2 CPU 采样——master CPU2 在 none 为 71%、ebpf 为 95%（BPF 捕获把 master 推饱和）。

### 步骤 3：禁 ACK（slave_receiver `--no-ack`）

- **动机**：slave_receiver 每 read 回发 `"OK\n"` ACK 到数据连接，master 从不读 → 未读接收缓冲堆满 → slave 的 ACK send 阻塞 → 背压。
- **改动**：slave_receiver 加 `--no-ack` 开关（默认行为不变）。
- **before → after**：sync 54%→**59%**、ebpf 52%→**58%**（+5pts）。
- **验证**：禁 ACK 后 QPS 稳定提升 ~8k，非噪声。

### 步骤 4：环形缓冲 + 去每命令 cond_signal（sync 主瓶颈）

- **动机**：sync 每命令 `fwd_enqueue` 做 malloc 深拷贝 + 跨核 `pthread_cond_signal`（转发线程在 CPU0，跨核 futex_wake）。
- **改动**：
  1. 链表+malloc 节点 → **64MB 连续环形缓冲**（`[u32 len][payload]` 槽），消除每命令 malloc/free；
  2. **删每命令 `cond_signal`**，转发线程靠 `FWD_EMPTY_WAIT_US=200µs` timed-wait 自醒（单命令有界延迟，不丢）。
- **before → after**：sync 跨机 56.4k→**80.2k**（59.7%→**84%**），+42%。
- **验证**：
  - 删 signal 后 cross-write 立即从 56.4k 跳到 80.2k（同会话 A/B）；
  - FWD_DISCARD（入队不写）= 94k ≈ none → 证明瓶颈是每命令 futex_wake，不是写路径；
  - 2×2 矩阵（loopback/跨机 × 写/不写）证明跨核唤醒是主因。

### 步骤 5：proxy 批量 writev + 去 signal（ebpf 路径）

- **动机**：生产 proxy 逐节点 `writev(iov,1)` + 每命令 malloc + `cond_signal`（与 harness 修复前同款模式）。
- **改动**（生产 `src/ebpf_proxy/main.c`）：
  1. 转发线程**批量 writev**（`PFWD_BATCH_MAX=256`，持 `g_state_lock` 原子于 BUFFERING flip）；
  2. **去每命令 `cond_signal`** + `PFWD_LINGER_US=200µs` timed-wait（单命令有界延迟）；
  3. 出队后 broadcast（补 64MB 满队列背压唤醒缺失）。
- **before → after**：ebpf 58.1%→**72.0%**（55.5k→67.4k），+14pp。
- **验证**：数据完整 248,000,308 字节全量（批量不丢命令）。

### 步骤 6：capture BPF fexit-only（删冗余 fentry）

- **动机**：fentry+fexit 每次 recv 触发 2 次；fentry 只存 `count_before`（请求读字节数）。
- **验证可行性**：`ctx[5]` = `tcp_recvmsg` 返回值（实测 trace_pipe：sshd rv=48、redis-benchmark rv=4，均真实字节数）——**头文件注释"6.1 fexit 不支持返回值"是错的**。
- **改动**：删 fentry + per-thread `client_fexit_ctx` map；fexit 用 `ctx[5]` 取字节数；main.c 配套删 fentry attach。
- **before → after**：ebpf 72.0%→**74.8%**（+2.8pp）。
- **后续证伪**：CPU2 仍 95%（fexit 数据拷贝才是 hook 大头），但 fexit-only 本身是正确简化（hook 减半）。

### 步骤 7：无锁 SPSC（sync）

- **动机**：reactor（CPU2）入队 + 转发线程（CPU0）出队共抢 `g_fwd_lock`——跨核 mutex 的 cache-line 乒乓。
- **改动**：reactor 是唯一生产者、转发线程是唯一消费者 → 用原子 head/tail（acquire/release）替代 mutex + condvar，消除跨核锁。
- **before → after**：sync 78%→**85%**（P=1 keyrange=100k）。
- **验证**：入队直接计时——跨机 139ns、loopback 109ns，**入队本身不是成本**；FWD_DISCARD（不写 slave）= ≈none 证明成本在跨机写，不在入队。

### 步骤 8：方法论修正（假象清除）

- **假象 A**（`-r N` 哈希表膨胀）：见 §1 假象 A。`--keyrange 100000` 固定后，ebpf 不再随 N 掉、sync > none 消失。
- **假象 B**（客户端同核）：见 §1 假象 B。`CLIENT_CPU=3` 隔离核后，none 63k→97k 归因清楚（测量保真度）。
- **最终**：sync **~85%**、ebpf **~99%** of none（P=1）。

**总结表（各步骤累积效果，% of none）：**

| 步骤 | sync | ebpf |
|---|---:|---:|
| 起点（README 污染） | ~49% | ~31.5% |
| + 多核隔离 | ~54% | ~52% |
| + 禁 ACK | ~59% | ~58% |
| + 环形缓冲/去 signal（harness） | **~84%** | — |
| + proxy 批量/去 signal | — | ~72% |
| + fexit-only | — | ~75% |
| + 无锁 SPSC | ~85% | — |
| + 方法论修正（keyrange/核隔离） | **~85%** | **~99%** |

## 3. 最终数据（修正方法论后）

**P=1、keyrange=100000 固定、客户端 CPU3、N=100w、5 采样中位数：**


| Payload |   none |   sync |    ebpf | sync/none | ebpf/none |
| ------- | -----: | -----: | ------: | --------: | --------: |
| 64B     | 97,704 | 81,960 |  96,367 |      0.84 |      0.99 |
| 128B    | 97,012 | 83,493 |  97,953 |      0.86 |      1.01 |
| 256B    | 97,867 | 82,898 | 100,878 |      0.85 |      1.03 |
| 512B    | 96,006 | 82,386 |  98,145 |      0.86 |      1.02 |

**P-sweep（keyrange=100000、N=100w）：**

| P | none | sync | ebpf | sync/none | ebpf/none |
|---:|---:|---:|---:|---:|
| 1 | 96,600 | 83,445 | 99,592 | 0.86 | 1.03 |
| 4 | 415,455 | 360,620 | 326,371 | 0.87 | 0.79 |
| 8 | 864,304 | 690,608 | 498,008 | 0.80 | 0.58 |
| 16 | 1,567,398 | 1,264,222 | 757,576 | 0.81 | 0.48 |
| 40 | 2,155,172 | 1,579,779 | 1,226,994 | 0.73 | 0.57 |

## 4. 比值翻转的解释（客户端换核后 sync/ebpf 相对位置翻转）


| 客户端位置                   |   none |          sync |          ebpf |
| ---------------------------- | -----: | ------------: | ------------: |
| CPU2（和 master 同核，饱和） | 66-70k | 67k（≈none） |  37.9k（54%） |
| CPU3（隔离，master 有余量）  |    97k |    82k（85%） | 96k（≈none） |

- ~~**ebpf 是 CPU-bound**~~（已被 §8 第三号假象修正）：核心稀缺时暴露（54%）、有余量时被吸收（99%）——但 99% 里混入了 IRQ 假象。
- ~~**sync 是延迟-bound**（~1.5µs 每命令延迟）~~（已被 §8 第三号假象修正）：139ns 入队 + 跨机写耦合的解释不成立，实测为 NIC IRQ 抢占客户端核。

## 5. 遗留未决点（2026-08-12 已解决）

1. ~~sync 的 ~1.5µs/命令延迟~~ → **已解决：第三号测量假象**（NIC IRQ 抢客户端核，见 §8）。机制验证链：FWD_DISCARD≈none（入队非瓶颈）、loopback≈none（写本身非瓶颈）、`/proc/interrupts` 证实 ens33 IRQ 全落客户端核 CPU3、IRQ 改钉 CPU1 后 sync 76k→97k≈none。
2. ~~ebpf 小幅超过 none（+1-3%）~~ → **已解决：同为 IRQ 假象 + 高方差下的中位数噪声**（见 §8）。ebpf 单轮中位数波动 ±15%（BPF/ringbuf 捕获 jitter），均值在多次受控复测中落在 none 附近。

## 6. 方法论文档

- `--keyrange` 固定键范围（防哈希表随 N 膨胀）。
- 客户端隔离核（README 声称但未实现的 `taskset 3`）。
- 单线程 epoll reactor harness（对齐生产 kvstore）。
- 数据完整性：slave bytes 与命令量匹配（全量无损）。

## 7. 附带产出（生产代码优化，已提交）

- `b8aa350` proxy 批量 writev + 去每命令 cond_signal（linger 有界延迟）。
- `e9d35c3`+`009b1c6` capture BPF fexit-only（ctx[5] 取返回值，删冗余 fentry）。
- 无锁 SPSC sync（harness 实验，验证跨核锁是部分成本）。

## 8. 第三号测量假象：NIC IRQ 抢占客户端核（2026-08-12）

**现象**：08-10 修正表（固定 keyrange + 客户端隔离核）仍显示 sync 稳定 ~85% none、ebpf 偶超 none +1-3%。用户质疑 sync 不应低这么多、ebpf 不应超过 none。

**根因定位**（当前 reactor harness 复测，IRQ 未固定时）：

| 实验                            | sync QPS       | 结论                                          |
| ----------------------------- | -------------: | ------------------------------------------- |
| FWD_DISCARD=1（入队不写 slave）     |  100.6k ≈ none | 入队非瓶颈（实测 148-203ns）                         |
| slave 放 loopback（127.0.0.1）   |   99.2k ≈ none | writev 本身非瓶颈                                |
| slave 放 .129（跨机，IRQ 未固定）      |       75.6-78k | 跨机写是瓶颈                                      |
| /proc/interrupts（跨机 sync 期间）  |              — | ens33（e1000）IRQ 19 全落 **CPU3=客户端核**，~6k IRQs/s |
| **IRQ 19 钉到 CPU1（空闲核）**       |   97.3k ≈ none | **机制确认：IRQ 抢占客户端**                          |

**机制**：irqbalance 动态把从机出接口（ens33）的 NIC IRQ 放到客户端所在核（CPU3）。跨机转发路径每命令产生 TX-complete + ACK 中断，抢占 redis-benchmark 进程 → 客户端测得 RTT 膨胀（p50 0.255→0.319ms）→ QPS 虚低。ebpf 模式因 irqbalance 动态搬移 + proxy 转发路径，未稳定复现（受控后同样 ≈none）。P=1 下客户端是测量瓶颈（单线程事件循环被中断拖慢），这与 master 侧 43% 空闲的 perf 观察吻合。

**修复（harness）**：`tests/perf/test_ebpf_hset_qps.c` 新增 `pin_nic_irq_off_client()`——root 下把默认路由接口（ens33）的 IRQ 钉到空闲核（避开 MASTER/FWD/PROXY/CLIENT），并暂停 irqbalance（防其 10s 周期搬回），退出时恢复。非 root 只告警。

**2026-08-12 受控重测**（IRQ 钉 CPU1，n=300k，4 采样中位数，数据无损：slave 收满 ~372MB）：

| mode  | 64B mean  | 64B median  | vs none  |
| ----- | --------: | ----------: | -------: |
| none  |    95,728 |      95,573 |        — |
| sync  |    96,688 |      95,929 |     1.00 |
| ebpf  |    99,510 |      98,701 |     1.03 |

另一次同条件受控复测出现 **none 三者最高**（中位数）：**none 94,667 / sync 94,067 / ebpf 92,368**（none stddev 967、sync 5514、ebpf 5103）——再次证明方向随机，none 不总是垫底，与交错测量第 1 轮（none 95.1k > sync 93.2k）相互印证。

Payload sweep（64/128/256/512B）同样落在 ±10-15% 包络内（VM 环境运行间方差；ebpf 高方差 ±15% 为 BPF/ringbuf 捕获 jitter，task-15 已记录）。

**方向翻转与漂移分析**（回答"为何 sync/ebpf 常显得高于 none"）：

交错测量（none/sync 交替 ×4，各 n=300k、3 采样中位数，IRQ 钉 CPU1）：

| 次序  | none 中位  | sync 中位  | sync − none          |
| ----- | ---------: | ---------: | -------------------: |
| 1     |     95,147 |     93,226 |  −1,921（none 更高） |
| 2     |     94,637 |     97,784 |               +3,147 |
| 3     |     94,488 |     96,000 |               +1,512 |
| 4     |     90,772 |     97,308 |               +6,536 |

- **方向翻转**：第 1 轮 none 高于 sync（95.1k vs 93.2k），"sync 必然更高"不成立——若是系统加速，方向应一致，但实际翻转。
- **none 自身会话漂移 4.5%**（95.1k→90.8k，约 4 分钟）：后几轮 "sync>none" 主要由 **none 基线下滑**造成，非 sync 加速（sync 全程 93-97k 稳定）。
- **统计**：n=4，sync mean 95.7k vs none mean 93.6k，差 ~2.1k，SE≈1.8k → **不显著**（p>0.05）。3/4 为正不构成显著偏离随机。
- **结论**：无系统性差距；三模式 ≈ none。**方法论教训：单次顺序测量被会话漂移污染，比较必须交错（A,B,A,B）并看配对差值**；这也解释了旧 task-15 数据按 payload 顺序单跑时 512B 出现 none=90.3k 的低值。

**P=1 客户端瓶颈（client-bound）——波动真正来源（2026-08-12）**：

用户追问"为何 sync/ebpf 波动范围超出 none、范围不重叠"。实测各核利用率（none 模式，2s 采样）：

| CPU   | 角色                    | busy% 说明                                           |
| ----- | ----------------------- | ---------------------------------------------------- |
| cpu2  | reactor                 | 58.2%（有 ~40% 空闲，非瓶颈）                        |
| cpu3  | redis-benchmark 客户端  | 88.4%（接近饱和，瓶颈）                              |
| cpu0  | 转发/proxy              | 21.6%（none 模式本应空闲，被 claude/node 后台占用）  |
| cpu1  | IRQ/空闲                | 12.4%（tracker-store）                               |

- **P=1 下 QPS ≈ 客户端单线程事件循环上限（~95-100k），不是服务器能力**。佐证：reactor 仅 58% CPU；P-sweep 中 P=1 三模式都卡同值、P=16 冲到 1.2-1.5M（reactor 才成为瓶颈）。
- **波动源**：(a) 客户端自身 loopback 流量在 CPU3 上产生软中断（ksoftirqd/3）+ kworker/3；(b) 未钉核后台进程（claude/tracker-store/mysqld/redis-server）间歇迁移到 CPU2/CPU3 抢占。任一尖峰直接压低客户端吞吐 → QPS ±4-10% 波动。
- **sync/ebpf 范围更宽**：转发线程（CPU0）给 reactor 引入周期性缓存行竞争 + syscall 尖峰，P=1 延迟敏感客户端放大这些 reactor 侧延迟尖峰 → stddev 3-7k vs none 1-2k。
- **512B 范围不重叠**（none 90.3k vs sync 102.7k）= 相邻测量抓到客户端 CPU3 不同噪声相位，非模式差异。
- **修正测量方法**：多 redis-benchmark 实例分核 / 提高 P 使 reactor 饱和 / 停用或钉离 tracker-store、claude 等后台进程 / 交错测量。单客户端 P=1 只能得到客户端上限，无法分辨转发路径的真实代价。

**真实转发代价（IRQ 修正后 P-sweep，2026-08-12）**：

P=1 的数据（三模式 ≈none）被客户端瓶颈掩盖。用 IRQ 修正后的 harness 重跑 P-sweep（P=1/4/16/40，64B，keyrange 100k，n 按 P 取 300k~8M，3 采样中位数）：

| P   | none       | sync       | ebpf       | sync/none  | ebpf/none  |
| --: | ---------: | ---------: | ---------: | ---------: | ---------: |
|   1 |     99,767 |     99,010 |    105,374 |       0.99 |       1.06 |
|   4 |    410,846 |    415,455 |    350,754 |       1.01 |       0.85 |
|  16 |  1,521,607 |  1,145,475 |    779,545 |       0.75 |       0.51 |
|  40 |  1,992,528 |  1,381,454 |  1,103,296 |       0.69 |       0.55 |

（P=16 时各核：reactor 88% / client 87%，双方接近饱和，非纯 reactor-bound 也非纯 client-bound。）

**两 regime 结论**：
- **P=1（client-bound）**：三模式 ≈none（~100k），转发代价被客户端单线程上限掩盖。README 早前的 "sync≈ebpf≈none 无系统差距" 只在此 regime 成立。
- **P≥16（负载饱和）**：转发真实代价浮现——**sync ~69-75% of none**（转发线程 enqueue+跨机写在饱和下的 ~25-31% 代价），**ebpf ~51-55% of none**（fexit 捕获 probe_read+ringbuf 提交是 reactor 每 recv 的固有成本，~45-49%，与 task-9 "BPF hook 在 CPU2 是 ebpf 差距主因" 一致）。**转发路径（尤其 ebpf 内核捕获）在饱和下有真实且显著的吞吐代价**。

**多客户端验证：P=1 转发成本被客户端掩盖（决定性实验，2026-08-12）**：

harness 新增 `RB_INSTANCES`（多 redis-benchmark 实例分核）。P=1、64B、keyrange 100k，加客户端实例：

| 模式  | 1 客户端  | 2 客户端（CPU3+CPU1）  | 3 客户端  |
| ----- | --------: | ---------------------: | --------: |
| none  |   101,868 |         193,398 (+91%) |   167,338 |
| sync  |    98,103 |         158,111 (+61%) |         — |
| ebpf  |    92,251 |         104,668 (+13%) |         — |

- **none 突破 100k**（193k）：单客户端 P=1 的 ~100k 是 redis-benchmark 单线程事件循环上限，非 reactor 上限（reactor 实测可处理 1/1847ns=541k/s）。
- **多客户端让转发成本立即浮现**：none 193k > sync 158k（**0.82×**）> ebpf 105k（**0.54×**）——与 P=16 比值（0.75/0.51）几乎一致，证明**转发成本恒定真实（sync ~18-25%、ebpf ~46-49%），单客户端 P=1 的 ≈none 是客户端上限完全掩盖**。
- **ebpf 增长最少（+13%）**：capture 每命令成本最高（5.5µs），加客户端推不动。
- none 3 客户端回落 167k：第 3 实例钉 CPU0（被 claude/node 后台占用），客户端本身慢 + reactor 处理 150 连接开销，不影响结论。

**优化落地（2026-08-12）**：

- **sync 批量入队**（harness）：reactor 一次 read 的连续 HSET 整块入队（一次原子对+一次 memcpy，ring 格式不变）。P=16 cmd_avg 617→569ns、sync/none 0.75→**0.78**；P=1 无变化（每 read 仅 1 命令，无批可攒）。FWD_DISCARD 隔离剩余成本 = 环跨核传输(+79ns) + 跨机写(+69ns)，为设计固有（FWD_INLINE 对照 0.16× 更差，环+独立转发线程架构正确）。
- **ebpf BPF_RB_NO_WAKEUP + proxy consume**（生产 `repl_client_capture.bpf.c` + `ebpf_proxy/main.c`）：ringbuf_output 加 `BPF_RB_NO_WAKEUP` 去掉每 recv 的 self-IPI（task-9 记过占 master CPU2 25-27%，实测 ebpf 代价几乎全在此）；proxy 主循环改 `ring_buffer__consume` 主动 drain（`ring_buffer__poll` 仅当 sleep，因 NO_WAKEUP 下 producer 不发 eventfd 信号）。**P=16 0.48→0.82×、P=1 双客户端 0.54→0.93×，数据无损（2.78GB）**。trade-off：转发延迟最多 +1ms（proxy 每 1ms poll）。
- **ringbuf_reserve 直写去二次拷贝被拒**：6.1 verifier 要求 reserve size 为编译期常量（`R2 is not a known constant`），变长条目不可用，放弃。

**优化后最终对比**（IRQ 钉 CPU1，64B，3 采样中位数）：

| 场景          | none       | sync       | ebpf       | sync/none  | ebpf/none  |
| ------------- | ---------: | ---------: | ---------: | ---------: | ---------: |
| P=1 单客户端  |      ~100k |      ~100k |      ~100k |       掩盖 |       掩盖 |
| P=1 双客户端  |    190,356 |    158,013 |    176,463 |       0.83 |       0.93 |
| P=16          |  1,521,607 |  1,187,084 |  1,258,495 |       0.78 |       0.82 |

**保留的注意点**：ebpf 单轮中位数波动大（±15%），报告应取多轮中位数/均值并用；NIC IRQ 必须钉在空闲核（harness 已自动处理）；比较不同模式/时间点的绝对值需交错测量抵消会话漂移。

## 9. 2026-08-12 完整调查与优化过程（时间顺序总览）

> 本节按时间顺序整合本次会话全部更新，各步骤的详细数据见 §8 对应小节。

### 9.1 第三号测量假象：NIC IRQ 抢占客户端核

- **现象**：08-10 修正表仍显示 sync ~85% none、ebpf 偶超 none。用户质疑。
- **根因定位**：跨机 sync 期间 `/proc/interrupts` 显示 ens33（e1000）IRQ 19 全落 **CPU3=客户端核**（~6k IRQs/s），客户端被 NIC 中断抢占。证据链：FWD_DISCARD≈none（入队非瓶颈）→ loopback≈none（写本身非瓶颈）→ IRQ 改钉 CPU1 后 sync 76k→97k≈none。
- **修复（harness）**：`pin_nic_irq_off_client()` root 下自动把 NIC IRQ 钉空闲核 + 暂停 irqbalance。

### 9.2 客户端瓶颈（client-bound）：P=1 的 ~100k 是客户端上限

- **实测**：P=1 none 时 reactor（CPU2）仅 58% busy、redis-benchmark 客户端（CPU3）88% busy → QPS ≈ 客户端单线程事件循环上限，非 reactor/转发能力。
- **波动来源**：客户端自身 loopback 软中断 + 未钉核后台进程（claude/tracker/mysqld）迁移到 CPU2/CPU3 → QPS ±4-10% 波动；ebpf 因 capture 使 reactor 接近饱和（87.6%），转发路径自身 jitter 直接透传 → stddev 7.7k。

### 9.3 多客户端验证：转发成本被单客户端掩盖（决定性）

- harness 加 `RB_INSTANCES`（多实例分核）。P=1 双客户端：none 102k→**193k**（证明客户端上限），sync 158k（0.82×）、ebpf 105k（0.54×）——比值与 P=16 一致，转发成本恒定真实。

### 9.4 每命令耗时实测：高 P 摊薄

- `[reactor] per-cmd` 埋点：cmds/read 1.00→16.00（P=1→16），none cmd_avg 1847→421ns（read syscall 摊薄 16×）。P=16 转发成本实测：sync +196ns、ebpf +480ns。

### 9.5 优化落地

| 优化 | 改动 | 效果 |
| ---- | ---- | ---- |
| sync 批量入队 | harness：一次 read 的连续 HSET 整块入队 | P=16 cmd_avg 617→569ns、sync/none **0.75→0.78** |
| ebpf NO_WAKEUP | `repl_client_capture.bpf.c` ringbuf_output 加 NO_WAKEUP 去 self-IPI | P=16 **0.48→0.82**、P=1 双 **0.54→0.93**，数据无损 |
| proxy consume | `ebpf_proxy/main.c` 主循环 poll 当 sleep + consume 主动 drain | 配合 NO_WAKEUP 不丢数据 |
| ~~ringbuf_reserve~~ | ~~去二次拷贝~~ | 被拒：6.1 verifier 要求 reserve size 编译期常量 |

**P=80/160 ebpf 丢数据修复（2026-08-12）**：

- **根因 1（已修）**：capture 每 recv 上限 `CLIENT_ENTRY_MAX_LEN` 原 8192，高 P 下一条 recv 是批量（P=80 ~10KB、P=160 ~20KB）被截断 → 丢 17%/59%。提到 **32764**（PERCPU_ARRAY 值上限 32KB）后 P=80 从丢 17% → 近无损。
- **根因 2（已修）**：proxy `PFWD_LARGE_SZ` 原 8200（按 P=1 单命令设计），高 P 批量条目（>8200）被误判"超大"走慢 heap cache 路径 → 提到 **32768**。
- **残留（吞吐上限）**：proxy 转发路径上限 ~202MB/s（sync 同链路 208-215MB/s，差在每 recv enqueue malloc+memcpy）。P=80 需求 205MB/s → 长跑 ~1.4% 丢；P=160 需求 270MB/s → ~41% 丢。彻底修需把转发队列改无锁环形缓冲（较大生产改动，且 P=160 需求仍超 link 上限 ~215MB/s）。

### 9.6 最终数据（优化后，IRQ 钉 CPU1，64B）

**单客户端 P-sweep（3 采样中位数）：**

| P   | none       | sync       | ebpf       | sync/none  | ebpf/none  |
| --: | ---------: | ---------: | ---------: | ---------: | ---------: |
|   1 |     97,213 |     98,425 |    100,637 |      1.01* |      1.04* |
|  10 |  1,077,199 |    937,793 |    911,854 |       0.87 |       0.85 |
|  20 |  1,631,321 |  1,250,000 |  1,321,702 |       0.77 |       0.81 |
|  40 |  2,119,767 |  1,347,482 |  1,456,399 |       0.64 |       0.69 |

（*P=1 客户端瓶颈，比值不稳定。数据完整性：slave 收 ~16.17GB ≈ 130.4M 命令 ×124B，99.8%。）

**P=1 多轮（6 循环交错，中位数）**：none 96,061 / sync 99,933 / ebpf 97,720——三者 ~96-100k，排序不稳定（none 可最高，也可最低），客户端瓶颈掩盖转发成本。

**多客户端（P=1，双客户端）**：none 190,356 / sync 158,013（0.83）/ ebpf 176,463（0.93）。

**结论**：单客户端下转发代价从 P=10 起显现（sync/ebpf ~85-87%），P=40 加深（sync 0.64、ebpf 0.69）。**优化后 ebpf 在高 P 已优于 sync**（NO_WAKEUP 去 self-IPI 后，ebpf 的剩余捕获成本低于 sync 的环+跨机写路径）。单客户端 P=1 的 ≈none 是客户端上限掩盖。

## 10. P=1 单客户端测量假象深挖 + 双客户端 P-sweep（2026-08-12 最终）

> 本节记录对"单客户端 P=1 下 sync/ebpf 系统性高于 none"的完整调查链（perf + redis-benchmark 源码 + 延迟实验），以及用**双客户端移除客户端瓶颈**后的权威 P-sweep 数据。**§9.6 的"ebpf 高 P 已优于 sync（P=160 +20%）"结论被证明是单客户端假象，以本节为准。**

### 10.1 现象：单客户端 P=1 下 sync/ebpf 系统性高于 none

- 交错测量 14 轮（-c 50）：none 97-103k、sync 99-111k、ebpf 97-108k，**none 从未最高**。
- 早期 doc §8"方向翻转（round 1 none>sync）"实为 n=4 弱信号误读（rounds 2-4 均 sync>none）。
- 用户质疑合理：sync/ebpf 服务端做的工作只增不减，理论不可能更快。

### 10.2 排查过程（逐步排除）

| 实验 | 结果 | 结论 |
| --- | --- | --- |
| 交错 14 轮 | none 从未最高 | 不是随机噪声，系统性偏 sync/ebpf |
| 换序（sync/none/ebpf 打头） | "先测的最低"不成立（sync 打头仍最高） | 非顺序偏差 |
| 隔离（FWD_NOENQ） | ≈none（101.8k） | slave 连接+转发线程存在无影响 |
| 隔离（FWD_DISCARD） | +5%（106.5k） | 入队活动起作用 |
| 完整 sync | +7.5%（109.2k） | 入队+写都起作用 |
| 客户端观测 RTT | sync p50 0.239 vs none 0.247ms | 客户端真测得更短 |
| 服务端每命令耗时 | sync cmd_avg 2236 vs none 1780ns | **服务端更慢却测得更高（悖论）** |

### 10.3 根因确证（perf + 源码 + 延迟实验）

**机制链**：
```
redis-benchmark 单客户端进程 ~98-99% CPU（完全 CPU-bound）
→ QPS = CPU 预算 ÷ 每请求 CPU
→ 每请求 CPU 被服务端应答时序左右：
   转发让服务端更慢（cmd_avg 2236 vs 1780ns）
   → 每个服务端周期攒更多请求 → 回复更集中到达
   → 客户端 recvfrom 合并（0.98→0.92 次/请求）、epoll_ctl 节省（4.9→4.6/请求）
   → 每请求 syscall/CPU 降低（9.78→9.45µs）→ 同 CPU 下测得更高
```

**证据链**：
1. 服务端回包是"收到即直接 write"（`reactor_conn_read` 末尾 `reactor_conn_flush` 立即写，**无批量 flush**）。
2. 客户端 /proc utime 实测：none 3.910s/4s(97.8%)、sync 3.970s/4s(99.3%)——都完全 CPU-bound。
3. syscall 追踪（perf tracepoint）：delay=1000 时客户端每请求 sendto+recvfrom 1.84 vs 1.96、epoll_ctl 4.6 vs 4.9（~6% 更少 syscall）。
4. **延迟实验（因果确证）**：给 none 服务端加 500/1000ns 纯延迟（无转发），客户端 QPS 从 ~100k 升到 ~105k/~109k，与 sync 的 234ns→~105k 同向。
5. redis-benchmark 源码（7.2.9）：`reqpersec = requests_finished / totlatency`（`totlatency = mstime()-start` 墙钟）；`clientDone` 在最后请求完成时立即 `aeStop`（无 timer overrun）。

**本质**：单客户端 P=1 测的是**客户端自己的 CPU 吞吐**，不是服务端性能；且客户端 CPU-bound 时每请求 CPU 被"服务端应答时序"反向污染（服务端越慢、客户端收包越合并、越省 CPU、测得越高）。

### 10.4 决定性验证：移除客户端瓶颈后假象消失

- **双客户端（RB_INSTANCES=2，P=1）**：none 201,266 > sync 185,033 > ebpf 182,856（sync/ebpf 均 −8%）——真实排序浮现。
- **-c 5（连接少）**：方向也反转，4 轮交错 none 最高 2 轮（如 none 90,081 > sync 89,013 > ebpf 87,547）。
- **结论**：单客户端 P=1 不能用于比较服务端转发模式，必须多客户端分核或高 P（使服务端成为瓶颈）。

### 10.5 双客户端 P-sweep（权威数据，RB_INSTANCES=2，64B，3 采样中位数）

| P   | none       | sync       | ebpf       | sync/none | ebpf/none | ebpf/sync |
| --: | ---------: | ---------: | ---------: | --------: | --------: | --------: |
|   1 |    200,703 |    184,083 |    184,372 |     0.92  |     0.92  |     1.00  |
|  10 |  1,048,869 |  1,000,289 |  1,025,175 |     0.95  |     0.98  |     1.02  |
|  20 |  1,450,855 |  1,388,375 |  1,361,994 |     0.96  |     0.94  |     0.98  |
|  40 |  1,637,335 |  1,558,124 |  1,564,400 |     0.95  |     0.96  |     1.00  |
|  80 |  1,964,029 |  1,654,339 |  1,644,761 |     0.84  |     0.84  |     0.99  |
| 160 |  2,377,216 |  1,646,000 |  1,640,695 |     0.69  |     0.69  |     1.00  |

（数据完整性：sync/ebpf 全部无损，tcpsink 计数与期望一致：P=1→3.2M、其余→24M，含 P=80/160 背压触发时。）

**结论**：
- **真实转发代价**：P=10~40 sync/ebpf ≈ 0.94-0.98× none（~2-6%），P=80 0.84×（16%）、P=160 0.69×（31%，reactor 饱和）。
- **ebpf ≈ sync，各 P 持平**（0.98-1.03×）。§9.6"ebpf 高 P 优 sync（P=160 +20%）"是单客户端假象——sync 转发线程（200µs linger + 小 writev）单客户端下先成瓶颈，双客户端下 reactor 成瓶颈后两者持平。
- **P=1 none 最高**（200k），sync/ebpf 均 −8%——单客户端"sync/ebpf 高于 none"彻底消失。

### 10.6 附带产物（本调查链落地修复）

1. **ebpf 无损背压**：proxy 转发队列 >48MB 置 `client_ctl[4]`，master 在 `recv` 前停手（无损降速）直到 <16MB；心跳 `client_ctl[5]` 防 proxy 崩溃挂起。修复后 P=80/160 从丢 17%/41% → 12M/12M 无损。
2. **harness `ebpf_wait_ringbuf_drain` 修复**：原另开 ring_buffer reader 偷记录丢弃，改为 mmap 读 producer/consumer 位置等空。
3. **proxy 关闭丢队列修复**：`proxy_fwd_thread_main` 改 `for(;;)` 排空队列才退出；`cleanup` 先 detach fexit + 排空 ringbuf 再停转发线程。
4. **tcpsink `SO_RCVBUF=1MB` 测量假象**：禁用 TCP 接收自动调优、窗口卡 64KB → ~210MB/s 假上限（同链路 iperf3/去掉 RCVBUF 实测 615-622MB/s）。

## 11. 单客户端 -c 50 全 P-sweep + 两个 harness bug 修复（2026-08-13）

> 本节记录：① 复测中发现的两个 harness bug（此前 `--mode all` 完全不可用）；② 单客户端 -c 50 全 P-sweep 数据（证明「单客户端 + 清后台」也能排对序）。

### 11.1 两个 harness bug

1. **`g_shutdown` 不复位（`master_start`）**：`master_stop()` 置 `g_shutdown=1` 后不复位，`--mode all`（warmup→none→sync→ebpf）下 warmup 之后的三个模式 reactor 一启动就退出 → 15900 端口关闭 → redis-benchmark 连接被拒 → `parse_bench_qps` 返回 0 → 落到 `count/elapsed` 兜底，QPS 虚高到 50M~550M。README 权威数据用 `--mode single`（独立进程）跑，未踩中。修：`master_start` 加 `g_shutdown = 0`。

2. **fwd 线程 `iov[512]` 越界（`fwd_thread_main`）**：循环条件 `niov < FWD_MAX_IOV`，但 else 分支（payload 跨环尾）一次写 2 段 iovec；niov=511 时再走 else 就写 `iov[512]`（越界 16B）→ stack smashing。P=80 sync 触发（`max_queue_bytes=62MB`）。概率性（取决于环形缓冲相位），README 的 P=80 恰好未触发。修：条件改 `niov + 1 < FWD_MAX_IOV`。

### 11.2 单客户端 -c 50 全 P-sweep（后台钉离 CPU2/CPU3，64B，n=20w/200w，3 采样中位）

| P   | none       | sync       | ebpf       | sync/none | ebpf/none |
| --: | ---------: | ---------: | ---------: | --------: | --------: |
|   1 |     15,157 |     14,685 |     14,594 |     0.97  |     0.96  |
|  10 |    151,446 |    118,476 |    119,875 |     0.78  |     0.79  |
|  20 |    272,294 |    260,044 |    268,132 |     0.96  |     0.98  |
|  40 |    591,716 |    426,621 |    460,405 |     0.72  |     0.78  |
|  80 |  1,041,667 |    805,153 |    878,349 |     0.77  |     0.84  |
| 160 |  1,809,955 |    965,251 |    942,063 |     0.53  |     0.52  |

**结论**：

- **排序正确**：none 全程最高（此前 -c 50 单客户端 P=1/10 的「sync/ebpf > none」消失）。
- **单调**：none/sync/ebpf 均随 P 单调升（此前单客户端 P=80 sync 低于 P=40 的拐点消失）。
- **绝对值不可信**：本组在同一台跑着 Claude Code/vscode/mysqld 的机器测得，后台虽钉离 master(CPU2)/client(CPU3)，绝对值仍比受控环境（§9.6 的 ~94k@P=1）低 3-6×，且比值噪声大（P=10/40 代价 ~22-28%、P=20 仅 ~4%）。**绝对转发代价仍以 §10.5 双客户端表为准**。

### 11.3 -c 5 复现 P=1 方向反转

为印证 §10 的「-c 5 反转假象」，同机复测 -c 5（n=20w/100w）：

| P | none | sync | ebpf |
| --: | --: | --: | --: |
| 1 | 12,865 | 11,942 | 11,879 |
| 10 | 121,256 | 108,003 | 103,189 |

方向与 -c 50（清后台）一致：none 最高。P=1 的 sync/none=0.93、ebpf/none=0.92，与双客户端 P=1 的 0.92 吻合。
