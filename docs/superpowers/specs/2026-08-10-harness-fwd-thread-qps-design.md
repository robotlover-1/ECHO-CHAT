# harness（test_ebpf_hset_qps）sync 独立转发线程 + 跨机 QPS 复测设计

**日期**：2026-08-10
**状态**：设计确认

## 1. 背景与动机

### 1.1 现状

README「eBPF fentry+fexit 主从转发 QPS 对比」（08-02/08-04 测量）用 `tests/perf/test_ebpf_hset_qps` 当 master（不跑 kvstore）：

- **none** = harness serve 路径（`--cpu 2`）：63,044。
- **sync** = handler 内联 `write_full(g_slave_fd, ...)` + 全局锁 `g_slave_fd_lock`（[test_ebpf_hset_qps.c:443-448](src/../../tests/perf/test_ebpf_hset_qps.c#L443-L448)）：30,838 ≈ **49% of none**。
- **ebpf** = harness + 生产 ebpf-proxy（fentry+fexit 截获 master `tcp_recvmsg` → ringbuf → 转发）：28,092 ≈ **44% of none**。

`2026-08-07-repl-forwarding-thread` 已把生产转发线程化：master 转发线程（`repl_fwd_thread_main`，kvs_repl.c）与 proxy 转发线程（`proxy_fwd_thread_main`，main.c，T4）。但新测的 None 用了真实 kvstore（74.4k），旧值用 harness（63k），**master 实现不同，三行都不能直接前后对比**。

### 1.2 目标

用**同一个 harness 当 master**（none/sync/ebpf 三模式 master 完全一致），重测 QPS：

- **sync**：harness 内 handler 不再内联阻塞转发，改「入队 + 独立转发线程」。
- **ebpf**：harness 不改，继续用生产 proxy（T4 已有独立转发线程）。
- **成功标准：sync 和 ebpf 都接近 none**（转发阻塞被消除的验证）。实测比值如实记录，未达则分析根因。

### 1.3 根因（已核实）

- **sync 慢**：转发在 handler 请求路径上（内联 `write_full`），且所有 handler 共用一个全局锁串行；slave 慢时 `write_full` 阻塞 handler。
- **ebpf 慢（旧）**：proxy 主线程 `ring_buffer__poll` 内同步 `writev`，慢 slave 阻塞 poll → ringbuf 满 → BPF 钩子阻塞 master 收包。T4 已把 `writev` 移出 poll。

## 2. 方案

**方案 A（选定）：有界 FIFO 队列 + 独立阻塞转发线程（镜像生产 T1-T3 的简化版）**

- handler 深拷贝 raw RESP 入队，临界区= memcpy（纳秒级）。
- 独立 pthread 消费队列，**dequeue 短持锁后释放锁再 `write_full`**——写 slave 完全移出请求路径。
- 队列限长 64MB + condvar 背压（满时 handler 等），保证慢 slave 不爆内存、测量诚实。
- 单消费者 → FIFO 顺序天然保持（RESP 流必须按序）。
- 只有一个 slave，阻塞 `write_full` 只卡转发线程自己，卡不到 handler——**无需**非阻塞 + epoll（生产多 slave 才需要）。

排除：方案 B（非阻塞 + epoll，单 slave 无收益，复杂）；方案 C（攒批定时 flush，破坏顺序）。

## 3. 设计

### 3.1 数据流

```
handler（per-conn，50 线程）:
  HSET 解析 → ht_hset → fwd_enqueue(深拷贝 raw RESP, len)     ← 取代 write_full

转发线程（单 pthread）:
  dequeue（短持锁）→ 释放锁 → write_full(g_slave_fd, buf, len) → free(node)
```

### 3.2 组件与并发

- 队列：`fwd_node_t { unsigned char *buf; size_t len; next; }`（buf 深拷贝、内联在节点内），head/tail + `g_fwd_lock` + `g_fwd_cond` + `g_fwd_queue_bytes`，限长 `MAX_FWD_QUEUE_BYTES = 64MB`。
- handler 入队：`fwd_enqueue(buf, len)`；队列满 → `cond_wait` 背压。
- 转发线程：`fwd_thread_main`，单消费者；`g_slave_fd` 只由它写。
- `g_slave_fd_lock` **删除**（无并发写者）。
- 顺序：单消费者 FIFO。
- 亲和：转发线程继承 `--cpu`（pthread 继承进程 affinity mask），不改测量口径。

### 3.3 启动 / 停止 / 错误

- sync setup：连 slave（现有代码）→ `fwd_thread_start()`。
- teardown：`fwd_thread_stop()`（置 stop、cond_broadcast、join、free 残余节点）→ close slave fd。
- SIGPIPE：main 已 `signal(SIGPIPE, SIG_IGN)`（[test_ebpf_hset_qps.c:753](src/../../tests/perf/test_ebpf_hset_qps.c#L753)），无需新增。
- `write_full` 语义：错误返回 -1；EPIPE break 返回部分长度。转发线程判断 `write_full(fd, buf, len) != len` → slave 断开/写失败，置 `g_fwd_dead`，丢弃后续入队（测试代码不重连）。

### 3.4 改动文件

| 文件 | 改动 |
|---|---|
| `tests/perf/test_ebpf_hset_qps.c` | 新增转发队列 + 线程；handler sync 分支改入队；删除 `g_slave_fd_lock`；sync setup/teardown 接线程 |
| `docs/superpowers/bench/2026-08-10-harness-fwd-thread-qps.md` | 新结果文档 |

## 4. 测量方法（对齐 README / 之前做法）

- **拓扑（跨机）**：master(.128) harness `--cpu 2`（sudo）；ebpf 模式同进程起生产 proxy（sudo，CAP_BPF，先 `rm -rf /sys/fs/bpf/...` pin）；slave(.129) `nohup /home/pp/slave_receiver 15901 > /tmp/slave.txt &`。
- **客户端**：.128 本机 `/opt/redis-7.2.9/bin/redis-benchmark -n N -c 50 -P 1 -d 64 -r N HSET key:__rand_int__ value`，`taskset -c 3`。
- **采样**：harness `-r R`（round 0 预热，后 R-1 轮取中位数）。
- **两阶段**：
  - 首轮冒烟（小数据量）：`-n 50000 -r 4`（1 预热 + 3 采样），验证链路 + 初值。
  - 正式（可选，冒烟稳定后）：`-n 1000000 -r 6`（5 采样中位数，对齐 README 口径）。
- **数据完整性**：sync/ebpf 模式测后读 .129 `/tmp/slave.txt` 的 `msgs=/bytes=`，与命令量匹配。
- **构建前置**：`make` 重新编译 `build/ebpf_proxy`（须含 T4 转发线程 `proxy_fwd_thread_main`）与 `tests/perf/test_ebpf_hset_qps`（须含本次 sync 转发线程改动），避免测到旧二进制。
- 结果写入 `docs/superpowers/bench/2026-08-10-harness-fwd-thread-qps.md`。

## 5. 成功标准

1. **sync vs none**：QPS 比值显著高于旧内联转发的 ~49%；**目标 ≥ 85% none**（阻塞消除的验证）。未达则分析（slave 消费速度 / CPU2 核竞争 / 队列背压触发）。
2. **ebpf vs none**：用生产 proxy（T4）读数，**目标 ≥ 85% none**（与 sync 同标准）；BPF fentry+fexit hook 开销是剩余固有成本，若低于 85% 如实记录并与 README 旧 28k（44%）对照归因。
3. **数据完整性**：sync/ebpf 下 slave_receiver `msgs/bytes` 与命令量匹配。

## 6. 风险与取舍

- **核竞争**：转发线程与 50 个 handler 同核（CPU2），sync 可能因调度压力略低于 none——这是真实代价，如实记录。
- **背压掩蔽**：若 slave 消费慢，sync QPS = 转发线程/slave 上限，而非 master 上限——slave_receiver 消费极快（纯读丢弃），预期不触发。
- **测试代码定位**：改动仅在测试 harness，生产代码不动；sync 本就不需要生产实现（spec `2026-08-07` §6：sync 仅测试用）。
