# AOF 独立线程设计（方案 B：先回复再落盘 + 定频 fsync）

**日期**：2026-08-06
**状态**：设计已确认，待实现

> **方案变更记录**：初稿为方案 A（保留「落盘才回包」）。2026-08-06 实测确认 P=1 被 fsync 延迟锁死在 45k（barrier=0 无效、write-behind 参考项目不具备 always 语义），且「先回复 + 逐条 fsync」因磁盘 fsync 容量（~2300/s）无法支撑高 QPS 而不可行。用户决策改为**方案 B**：先回复（回包不等 fsync）+ AOF 线程按磁盘最大 fsync 率定频刷盘，崩溃窗口 ~1ms 有界。

## 1. 背景与动机

### 1.1 现状

当前 AOF always 默认实现为**异步批量 group commit**（`per_command=0, sync=0`）：单线程 reactor 处理命令，`persist_append_prepare` 追加 AOF 数据到全局 `g_aof_buf`，每个 epoll 周期结束由**主线程**执行 `persist_flush_pending()`（深拷贝整批 → io_uring 提交 linked write+fsync）与 `persist_reap_completions()`。**落盘才回包**：`persist_pending` 挡住连接响应，fsync 完成后才 flush。

### 1.2 实测瓶颈定位（2026-08-06 验证）

- **P=1（`-c 50 -P 1`）**：AOF always QPS ≈ **45k**。落盘才回包下客户端 RTT = 攒批 + fsync + 网络。实测 fdatasync 延迟 ~**410μs**（VMware 虚拟盘串行同步写往返），tmpfs（fsync≈0）P=1 达 **113k**（CPU 地板）。稳态时序：回复只在 fsync 完成后释放 → 两次 fsync 之间无新命令 → **服务器空转，fsync 延迟全额进 RTT**。
- **高 P（P=10~160）**：瓶颈转为主线程 CPU。现基线：P=10 373k / P=20 660k / P=40 891k / P=80 1014k / P=160 1025k。

### 1.3 关键约束：磁盘 fsync 容量

实测单文件 fsync 容量 ~**2300/s**（fio `--fsync=1` IOPS 2293；C 程序 mean 408μs）。推论：

- **「先回复 + 逐条 fsync」不可行**：先回复后 QPS → CPU 地板 ~110k 命令/秒，逐条需 110k fsync/s ≫ 2300/s，队列无限堆积、崩溃窗口爆炸。
- **「先回复」可行的唯一形态是「批量定频」**：QPS 到 CPU 地板（~110k），AOF 线程按磁盘最大 fsync 率持续刷盘（每批覆盖 ~48 命令），崩溃窗口 ≈ 队列深度/fsync 率，有界 ~1ms。**这就是「吃满磁盘」**：P=1 下磁盘 2300 fsync/s × 每批 50 命令 ≈ 115k，与 CPU 地板重合。

### 1.4 已排除的路线（实测结论）

- **barrier=0 挂载**：真实 sda5 上 fsync 延迟不变（410μs），P=1 仍 ~43.6k。虚拟盘瓶颈是同步写往返，不是 barrier。
- **InazumaPlasma write-behind**：正常运行从不 fsync（关闭才 fsync），无窗口概念，比本方案弱，不照搬。
- **多文件分片 AOF**：并行 fsync ~6× 潜力但破坏顺序语义、恢复复杂，不做。

### 1.5 本设计目标

- **P=1 从 45k 提到 ~110k**（CPU 地板）：回包不等 fsync，fsync 延迟移出客户端 RTT。
- **AOF 线程按磁盘最大 fsync 率定频刷盘**：磁盘打满，崩溃窗口有界 ~1ms。
- 高 P 维持/略升（去掉回包 gating 后的 CPU 腾出）。
- **语义变化（必须声明）**：从「ack = 此刻已落盘」降级为「ack ≈ ~1ms 内落盘」（近似 always 的 everysec）。复制链路（`repl_durable_offset_ack` 依赖）需验证。

## 2. 设计约束（硬性）

1. **先回复**：回包不等 fsync，`persist_pending`/release 连接 gating 整体移除。
2. **窗口有界**：崩溃窗口 ≈ 待刷批队列深度 / fsync 速率，通过队列水位背压控制在 ~1ms。
3. **零拷贝**：write-in-place，无整批深拷贝。
4. **SINGLE_ISSUER 保持**：io_uring 所有操作（get_sqe/prep/submit/reap）只在 AOF 线程，主线程永不触达 ring。
5. **conn 状态只在主线程碰**：AOF 线程从不触达 conn（本方案下主线程几乎不碰 AOF 相关 conn 状态）。
6. **同步模式（`--aof-fsync-sync` / `--aof-fsync-sync-batch`）不动**：不建线程，走原主线程同步路径。
7. **小批定频**：批量边界要频繁（~每 50 命令或 loop 尾），避免单批过大把窗口拉大。

## 3. 架构总览

```
主线程 (reactor)                         AOF 线程 (新建)
─────────────────────                  ──────────────────────
命令处理 → persist_append_prepare       poll [g_aof_wake_efd, uring_efd]
  │  append 直接写 g_cur_slot->aof_buf  │  收到 wake → 取 ready 链表槽(FIFO)
  │  → 立即回包（不等 fsync）            │    get_sqe/prep/submit write+fsync linked
  └─ loop 尾 persist_flush_pending()    │  收到 uring_efd → reap CQEs
       = handoff 当前槽 → signal wake   │    双 CQE 完成 → 槽挂 completed
       背压: outstanding>=水位 → 等      │    → signal complete_efd
                                     │  持续刷（ring 满 submit_and_wait）
g_aof_complete_efd / loop 尾:
  persist_reap_completions() = drain completed → 槽归还 free
```

**核心机制**：写路径与回包路径解耦——主线程 append + 立即回包；AOF 线程后台按磁盘上限持续 write+fsync。slot 的 `aof_buf` 就是追加目标（write-in-place，无拷贝）。

## 4. 数据结构与同步原语

```c
persist_slot_t {
    unsigned char *aof_buf;   /* = 追加目标（write-in-place） */
    size_t         aof_len;
    long long      base_offset;   /* 本槽首个字节的 AOF 偏移 */
    int            cqe_seen, cqe_ok, last_error;
    int            in_use;
    persist_slot_t *ready_next, *completed_next;
};

static persist_slot_t g_inflight[PERSIST_INFLIGHT_SIZE];  /* 固定槽数组 */
static persist_slot_t *g_cur_slot = NULL;   /* 主线程当前追加槽（主线程独占） */
static persist_slot_t *g_ready_head;        /* 待 AOF 提交（FIFO） */
static persist_slot_t *g_completed_head;    /* 已落盘待主线程回收 */
static persist_slot_t *g_free_slots;        /* 空槽（主线程独占） */

/* 同步原语 */
pthread_mutex_t g_slot_lock;        /* 保护 ready/completed 链表与 outstanding 计数 */
int  g_aof_wake_efd;                /* 主线程 → AOF 线程 */
int  g_aof_complete_efd;            /* AOF 线程 → 主线程（= 新 persist_uring_fd()） */
volatile int      g_outstanding;    /* 原子：ready + in-flight 批数（窗口控制） */
long long g_aof_write_submitted;        /* 主线程 append 推进（主线程独占，base_offset 定位用） */
volatile long long g_aof_write_offset;  /* 原子（AOF 线程推进，主线程 SAVE 读） */
volatile int      g_persist_fatal_error; /* 原子 */

#define MAX_OUTSTANDING  16    /* 窗口背压水位：ready+in-flight 批数上限 */
```

`MAX_OUTSTANDING = 16` 批 × 平均批 ~3KB ≈ 48KB 待刷数据；突发大批（≤4MB/批）下 16×4MB=64MB 有界。崩溃窗口 ≈ 16/2300 ≈ **7ms 最坏**、稳态（~3 批在队）≈ **1.3ms**。

## 5. 主线程路径改动（kvs_persist.c 主体）

### 5.1 `persist_append_prepare`

```c
if (!g_cur_slot) {
    g_cur_slot = pop_from_free();          /* 主线程独占 */
    while (!g_cur_slot) {                  /* free 空 = AOF 线程未回槽 → 背压 */
        persist_reap_completions(); sched_yield();
        g_cur_slot = pop_from_free();
    }
    g_cur_slot->base_offset = g_aof_write_submitted;
}
grow_slot_buf_if_needed();                 /* kvs_realloc，同现 g_aof_buf 成长逻辑 */
memcpy(slot->aof_buf + slot->aof_len, buf, len);
slot->aof_len += len; g_aof_write_submitted += len;

/* 立即回包：本函数返回后命令已处理，无 persist_pending gating */

if (per_command || slot->aof_len >= AOF_BUF_MAX_SIZE)
    handoff_current_slot();
```

> **删除**：`g_release_head`、`persist_release_conn_t`、`c->persist_pending++`、release 链表全部移除。`persist_purge_conn` 不再需要（无 release 链表）。

### 5.2 `handoff_current_slot()`（批量边界 + 背压）

```c
if (!g_cur_slot || g_cur_slot->aof_len == 0) return;
lock g_slot_lock;
  while (g_outstanding >= MAX_OUTSTANDING) {   /* 窗口背压 */
      unlock; persist_reap_completions(); sched_yield(); lock;
  }
  链接 g_cur_slot 到 g_ready_head 尾部(FIFO); g_cur_slot = NULL; g_outstanding++;
unlock;
write(g_aof_wake_efd, 1);
```

### 5.3 `persist_flush_pending()`（语义保留，三后端零改动）

```c
handoff_current_slot();
```

### 5.4 `persist_reap_completions()`（回收完成槽）

```c
loop {
    lock; pop g_completed_head 全部; g_outstanding 相应减; unlock;
    if empty return;
    对每个槽：槽归还 g_free_slots（主线程独占）；
}
```

（无 conn 处理——回包已发，完成只回收槽位。）

## 6. AOF 线程内部

```c
void *aof_thread_main(void*) {
    persist_uring_init_once();              /* SINGLE_ISSUER|SQPOLL；注册 g_aof_fd */
    for (;;) {
        poll([g_aof_wake_efd, uring_efd], blocking);
        if (wake_efd) {
            drain_efd_counter();
            lock; pop ready 全部(FIFO); unlock;
            处理 work 请求（fd 重注册 / drain / stop）;
            for 每个 ready 槽:
                get_sqe; prep write(fd, slot->aof_buf, slot->aof_len, slot->base_offset);
                get_sqe; prep fsync(DATASYNC); IOSQE_IO_LINK; set_data(slot);
            submit;
        }
        if (uring_efd) {
            reap CQEs;
            对双 CQE 完成的槽:
                err → 置 g_persist_fatal_error(原子) + signal complete_efd;
                ok  → g_aof_write_offset(原子) = slot->base_offset + slot->aof_len;
                       lock; 槽挂 completed 尾部; unlock; signal complete_efd;
            if (drain 请求 && in-flight==0) signal drain_done;
            if (stop) break;
        }
    }
    persist_uring_close();
}
```

- **AOF 线程热路径零分配**：槽与 buffer 都是主线程的，AOF 线程只操作链表指针与 SQE。
- **定频刷盘**：AOF 线程收到批即提交，ring 满时 `io_uring_submit_and_wait` 阻塞等 fsync——以磁盘允许的最快节奏连续刷，自然吃满 2300/s 容量。
- 创建时机：`persist_init()` 中 `aof_fsync == ALWAYS && !aof_fsync_sync` 时创建。

## 7. 跨线程 fence（现同步调用点改造）

| 函数 | 现语义 | 新实现 |
|---|---|---|
| `persist_drain_pending` | 主线程 drain + submit_and_wait | handoff 当前槽 + 发 drain 请求 + 等 drain_done |
| `persist_force_aof_flush` | drain + fsync | 同上（SAVE 用） |
| `persist_reregister_aof_fd` | 主线程操作 uring | 发 fd 重注册 work 请求给 AOF 线程 + 等完成（bgrewrite finalize 用） |
| `persist_close` | drain + 关 uring | drain fence → 发 stop → join → AOF 线程关 uring |
| `persist_purge_conn` | 扫 release 链表 | **移除**（无 release 链表） |

## 8. 后端接线

- **reactor**：`persist_uring_fd()` 改返回 `g_aof_complete_efd`。事件处理器（reactor.c:321-327）只留 `persist_reap_completions()`（事件语义从「批就绪」变为「批完成」）。loop 尾 365-366 保留。
- **proactor / ntyco**：`persist_flush_pending` / `persist_reap_completions` 语义不变，零改动。
- `persist_submit_sqes` 无外部调用者，删除或并入 AOF 线程。

## 9. 错误处理与边界

- **CQE 错误**：AOF 线程置 `g_persist_fatal_error`（原子）+ signal complete_efd。主线程 append 检查后返回 `KVS_PERSIST_ERR`（同现）。
- **窗口控制**：`handoff_current_slot()` 背压循环（5.2）保证 `g_outstanding ≤ MAX_OUTSTANDING`。稳态 ~3 批在队 → 窗口 ~1.3ms；磁盘变慢时窗口按 `outstanding/fsync_rate` 增长，上限 ~7ms。
- **槽位内存边界**：固定 `PERSIST_INFLIGHT_SIZE` 槽数组 + `MAX_OUTSTANDING` 水位；槽缓冲区 ≤4MB（AOF_BUF_MAX_SIZE）。
- **runtime 切策略**：`APPENDFSYNC off` 时 append 路径早退（同现），线程空转；不支持运行期切 sync。

## 10. 验证与基准计划

1. **构建 + 现有测试**：`make`，`make test_persist_aof_demo`，AOF 重放恢复（重启后数据完整——注意：**先回复语义下重启会丢「已回包未落盘」的尾巴，恢复测试需按新语义调整预期**）。
2. **语义正确性（新）**：验证窗口有界——负载下 kill -9，AOF 尾部与「已回包」命令的差距 ≤ 窗口上限。
3. **复制链路**：`repl_durable_offset_ack` 主从 + failover 场景，确认先回复不回破坏复制持久化语义（重点）。
4. **并发正确性**：高 P 运行中 `SAVE` / `BGREWRITEAOF` / 客户端断连，无崩溃、无泄漏、无卡死。
5. **基准**（对齐 README 方法）：`redis-benchmark -n 1000000 -c 50 -P {1,10,20,40,80,160} -d 64`，每轮重启空库。预期：
   - **P=1：~110k（现 45k，+145%）—— 本轮核心收益**；
   - P≥10：与现基线（373k~1025k）持平或略升；
   - 输出新表 + 对比 + 结论（如实报告）。
6. 异步逐条（`--aof-fsync-per-command`）：语义保持（先回复）验证 + 测量。
7. **文档同步**：README「真·落盘才回包」表述需改为「先回复 + 有界窗口」；`docs/save-aof-always-mode-comparison.md` / `docs/optimization-history/aof-concurrent.md` 相关结论需更新。

## 11. 风险与取舍

- **语义变化（最大风险）**：ack 从「已落盘」变「~1ms 内落盘」。这是有意的降级（用户决策），但**客户端感知的持久化保证变了**，README 与相关文档必须同步改。
- **复制链路**：主节点 AOF 先回复对 `repl_durable_offset_ack` / failover 的影响需验证。
- **磁盘饱和**：P=1 下磁盘被 fsync 打满（2300/s），任何磁盘抖动直接反映为窗口/延迟波动。
- **窗口上界**：突发大压下窗口可达 ~7ms（MAX_OUTSTANDING=16），如需更严可调低水位。
- **proactor/ntyco** 响应延迟多一周期（基准用 reactor，可接受）。
- **复杂度**：跨线程链表 + 双 eventfd + fence，集中在 kvs_persist.c。

## 12. 改动文件

| 文件 | 改动 |
|---|---|
| `src/persistence/kvs_persist.c` | 主体：AOF 线程、write-in-place 槽、handoff/背压/drain、fence、原子偏移、删 release gating |
| `src/core/reactor.c` | eventfd 处理器语义微调（只 drain） |
| `include/kvstore/kvstore.h` | 删 `persist_release_conn_t` 相关、`persist_purge_conn`；如需新接口 |
| `README.md` + AOF 相关文档 | 语义表述更新（落盘才回包 → 先回复 + 有界窗口） |
| 基准脚本 | 复用 `tools/bench/run_aof_bench.py` + P-sweep |
