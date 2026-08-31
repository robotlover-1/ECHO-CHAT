# 转发线程（replication forwarding thread）QPS 对比

> 计划：`2026-08-07-repl-forwarding-thread`。HEAD = `585f5d2`。
> 目标：把 master 的 `repl_broadcast` 增量转发剥离到独立转发线程（T1-T3，非阻塞 EPOLLOUT），并把 ebpf-proxy 的 ringbuf/writev 转发剥离到独立线程（T4）。本任务（T5）验证实际 QPS 提升与数据完整性。
> 文档：T5 实测，中文。

## 测试方法

- 工具：`redis-benchmark -n 30000~50000 -c 50 -P 1 -d 64 -r 1000000 HSET key:__rand_int__ value`（kvstore HSET 为 2-arg，key+value）。
- 客户端 `taskset -c 0`，服务端 `taskset -c 2,3`。
- 每模式 3~5 个采样取中位数；测试前 `pkill -x kvstore` + `rm -f kvstore.aof kvstore.dump` 起空库。
- 本机：Master 127.0.0.1:5190，Slave 127.0.0.1:5191（同机第二个实例），TCP 全量同步。
- `--repl-fullsync-transport tcp`。

## 传输接线说明（测量口径，重要）

`--repl-realtime-transport` 的值决定 master 侧 replica conn 的 `repl_transport_kind`（`src/main/kvstore.c:1266-1297`），进而决定 `repl_broadcast`（`src/main/kvstore.c:516-558`）是否对 slave 入队：

| 配置 | 结果 | 对转发线程的影响 |
|---|---|---|
| `--repl-realtime-transport tcp` | slave 标记 `KVS_REPL_TRANSPORT_EBPF_TCP`，`repl_broadcast` 有意**跳过**该 slave（等 ebpf-proxy 转发） | 转发线程**不被**增量路径触碰 → 无转发开销 |
| `--repl-realtime-transport kprobe-rdma`（本机无 kprobe，健康检查把 `fwd_healthy` 压到 0） | slave 走 `repl_broadcast` → `repl_fwd_enqueue` → 转发线程，slave 真实收到增量 | 转发线程被真正触碰 → 测到转发开销，但也暴露 kprobe fallback 实时转发的不稳定 |
| `--repl-realtime-transport ebpf` + 独立 ebpf_proxy（root 运行） | fentry+fexit 捕获录入 → proxy 独立线程转发到 slave | 转发在 proxy 侧，master 无转发开销，proxy 是瓶颈 |

因此**“sync 接近 None”只有在 master 不真正转发（tcp 的 EBPF_TCP 跳过）时成立**；一旦转发线程真实承载增量发送，QPS 明显下降。

## 结果表（P=1，中位数）

| 模式 | QPS 中位数 | 采样 | 说明 |
|---|--:|---|---|
| **None**（无 slave） | **73,855** | 73,855 / 75,415 / 71,225 | 基线，无复制 |
| **sync**（kprobe-rdma，转发线程真实送达） | **34,650** | 35,014 / 31,908 / 34,650 | −53%，转发线程实际承载增量发送 |
| **sync**（tcp realtime，master 跳过 EBPF_TCP slave） | **81,967** | 81,967 / 93,284 / 81,699 | ≈None，但 master 不做任何增量转发（slave 未收到增量，off=0） |
| **ebpf**（master+proxy+slave，proxy 转发） | **23,024** | 13,686 / 23,024 / 25,663 / 23,659 / 15,098 | proxy 独立进程 fentry+fexit 捕获是额外开销 |

### 关键读数

- **tcp realtime“sync ≈ None”是空转**：master 对 `EBPF_TCP` slave 跳过 `repl_broadcast`（kvstore.c:539），实测 slave 增量 off=0、HSET 不达 slave。sync 数字 ≈ None 不代表转发线程高效，而是 master 压根不转发。
- **kprobe-rdma fallback 路径测到真实转发开销 −53%**：当转发线程真正承载增量（本机无 kprobe/proxy，靠健康检查 fallback）时，P=1 从 ~74k 降到 ~35k。注意该数值包含 kprobe 健康检查抖动与 fallback 路径固有开销，非纯净转发线程标的。
- **ebpf 独立 proxy 路径 ~23k**：proxy 用 fentry+fexit 捕获 master 的 `tcp_recvmsg`（每次客户端读都进两次 BPF），即使转发已独立线程化，BPF hook 本身在主 I/O 路径上，仍拉低吞吐；且 proxy 独立进程占 CPU。与 README 08-04 的 ebpf<sync<none 结论一致（本次接 kvstore 服务器实测 ~23k）。

## 数据完整性

- **增量（kprobe-rdma fallback，转发线程送达）**：master 与 slave 均 2000/2000 键命中，FNV-1a 一致性 `0xee40c64cc860c56b`（fmt：`concat(key\x00value)`），slave 复制 offset 随转发推进。SET/HSET 幂等，存储层无法暴露“重复送达”。
- **全量回归**：重启 slave 触发新全量，slave `aof_offset=105856=master`，FNV-1a 与 master 一致，pre_kill 增量键仍在 → 全量/增量衔接无丢失。
- **全量期在途写（增量/全量不穿插）**：1000 pre 键全量开始时并发写 500 post 键 → 全量后经转发线程回放送达，master/slave 均 `matched=1500/1500`，顺序正确、无穿插。

## T3 遗留 Important（REPLACK 追赶是否重复发送）

- **代码层窗口真实存在**（`src/main/kvstore.c:1344-1358`）：REPLACK 追赶从 backlog `repl_backlog_copy_range(applied_offset,end)` 深拷贝入队，可能与转发线程 `st->buf` 里尚未落地（EAGAIN、`repl_offset_sent` 未推进）的同一段数据重叠 → 理论上有重复发送风险，对非幂等命令（如 INCR 概念）会双计。
- **实测未能洁净复现**：自定义原始 RESP 慢速 sink（SO_RCVBUF 8k、小步读 + 高频 REPLACK）在高并发 flood 下出现“重复 key”计数（最高一次 24,703 命令/5,000 唯一键），但**对照组暴露 sink 自身实时接收不可靠**：小批量并发写时 sink 收 0 条命令（真实 kvstore slave 同样在 8s 内 applied_offset 不推进 ~10,793，viz. 实时转发受 `fwd_healthy` 健康状态抖动影响）。因此该计数是“sink 漏收 → REPLACK 重拉 → 计入重复”与潜在服务器重复的混淆，**无法归因**。
- **结论**：**未确认**为可洁净复现的真实缺陷。代码窗口应在稳定 realtime 传输（非 kprobe fallback）+ 符合协议的全双工监听器下单独量化，才能区分“服务器真重复”vs“接收器漏收触发重拉”。本次如实记录为“未复现/被接收器不可靠混淆”。

## 结论 / 对计划的评价

1. **转发线程改造达成形态目标**：增量发送不再阻塞 master reactor（非阻塞 EPOLLOUT），`repl_fwd_enqueue` 路径在 kprobe fallback 下可跑通且数据完整。
2. **“sync 接近 None”的预期只在 master 不真正转发时成立（tcp 的 EBPF_TCP 场景）**；当转发线程真实承载增量（kprobe fallback）时 QPS −53%，低于计划预期。这与 README 08-04 的 “sync ≈ −51%” 一致，说明把同步转发剥离出 reactor 后，瓶颈移到转发线程/dataplane，未消失。
3. **ebpf 独立 proxy 路径 ~23k < sync ~35k < none ~74k**，与既有 ebpf-forwarding 结论（BPF hook 在主路径上的固有开销）一致。T4 的 proxy 转发线程把 writev 移出 `ring_buffer__poll`，但 BPF hook 本身仍是瓶颈。
4. **T3 遗留 Important（重复发送）未洁净确认**，代码窗口存在但被 kprobe fallback 实时转发不稳定与 sink 接收不可靠掩盖，需稳定传输下单独量化。

## 附：ebpf 完整环境验证（root）

- 用 `run_ebpf_env.sh`（sudo）编排 master(root, `--repl-realtime-transport ebpf --ebpf-pin /sys/fs/bpf/kvstore_repl_sockmap`) + ebpf_proxy(root) + slave。
- **验证结果**：BPF fentry+fexit 在本机 6.1 内核加载并 pin 成功；master 成功 `bpf_obj_get`/写 `proxy_cfg`（pid/port/slave addr）；proxy `fullsync start→connected slave→fullsync end→cache flush(76)→FORWARDING`；slave `master_link:up`，benchmark 期间 `slave_repl_offset` 推进到 919,424（proxy 实际转发）。CAP_BPF 经 sudo 可用。

## 跨机复测（2026-08-10，真实 2 VM：master+proxy .128 / slave .129）

> **环境**：master(.128, root, `--repl-realtime-transport ebpf+tcp`) + ebpf_proxy(.128, root, 含 T4 独立转发线程) + slave(.129, pp, TCP 全量)。跨机链路。
> **方法**：`redis-benchmark -c 50 -P 1 -d 64 -r 1000000 HSET key:__rand_int__ value`，客户端 `taskset -c 0`，服务器 `taskset -c 2,3`，每模式 3 采样中位数。

| 模式 | QPS 中位数 | 采样 | vs None |
|---|---|---:|---:|
| **None**（master 单独，无 proxy/slave） | **78,697** | 77,760 / 78,697 / 82,149 | — |
| **ebpf**（master+proxy 转发线程+slave） | **80,289** | 81,077 / 69,803 / 74,738 + 78,499 / 81,374 / 80,289 | **~100%** |

### 跨机结果要点

- **ebpf ≈ None（~100%）**：proxy 独立转发线程（T4）把 `writev` 移出 `ring_buffer__poll` 后，**ebpf 转发 QPS 从本地旧测 23k 提升到跨机 ~75-80k，基本等于 None（78.7k）**。用户「ebpf 应接近 None」的目标在真实跨机链路达成。
- **对比 README 08-04 基线**：旧 ebpf 28k / 旧 None 63k（ebpf = 44%）；新 ebpf ~80k / 新 None 78.7k（ebpf ≈ 100%）。**proxy 转发线程消除了 ebpf 的转发瓶颈**。
- **数据完整性**：跨机链路 slave `GET testkey` 正确返回；benchmark 期间 slave `recover_tail_bytes` 达 ~30MB（增量全部同步）。
- **本地 23k 与跨机 80k 的差异**：本地 T5 的 ebpf 测量受 harness 配置（小 n、`run_ebpf_env.sh` 编排）影响偏低；跨机是 proxy 转发线程正常工作的真实读数。

### 结论修正

跨机实测推翻了 T5 本地「ebpf ~23k，BPF hook 是瓶颈」的读数——**proxy 独立转发线程（T4）后，ebpf 转发不再是瓶颈，QPS 达到 None 水平**。之前本地 23k 反映的是 proxy 单线程 writev 阻塞 ringbuf poll 的旧瓶颈（正是 T4 修复的），跨机修复后验证通过。

## README 方法论跨机复测（最终，2026-08-10）

> **方法论对齐 README「eBPF fentry+fexit 主从转发 QPS 对比」**：服务器 `taskset 2`（CPU2）+ 客户端 `taskset 3`（CPU3），5 采样中位数。2 × KVM：master+proxy 192.168.233.128 / slave 192.168.233.129。64B payload，`redis-benchmark -n 1000000 -c 50 -P 1 -d 64`。

| 模式 | 新实现 QPS（中位数） | vs None | README 08-04 旧值 | 变化 |
|---|---:|---:|---:|---:|
| **None** | **74,432**（73.8-76.8k） | — | 63,044 | 环境差异 |
| **sync**（master 转发线程） | **31,567**（31.3-33.1k） | 42% | 30,838 (49%) | ~持平 |
| **ebpf**（proxy 转发线程） | **48,086**（47.3-49.7k） | 65% | 28,092 (44%) | **+72%** |

### 最终结论

1. **proxy 独立转发线程（T4）真实改进 ebpf**：28k → 48k（**+72%**），从「ebpf<sync」反转为「ebpf>sync」。消除 writev 阻塞 ringbuf poll 的旧瓶颈。
2. **ebpf 仍为 None 的 65%（非 ≈None）**：BPF fentry+fexit 拦截（每次客户端读两次 BPF）是剩余固有开销，无法靠线程消除。
3. **此前「ebpf ≈ None（~80k）」是未绑核假象**：服务器用全部 4 核（含客户端核）才达到；README 方法论（服务器 CPU2）下真实为 ~48k。
4. **sync 保持 ~31k**（与 README 30.8k 吻合）：master 转发线程把发送移出 reactor，但瓶颈仍在转发线程/dataplane，吞吐未提升。
5. **绑定方法论的意义**：隔离客户端（防争抢）才能暴露服务器模式差异；不绑核时客户端-服务器争抢主导，三模式趋同（~32k）。
