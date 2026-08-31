# 复制转发独立线程设计

**日期**：2026-08-07
**状态**：设计确认，待实现

## 1. 背景与动机

### 1.1 现象

master→slave 主从转发 QPS 对比（README eBPF fentry+fexit 表，64B payload）：None 63k / sync 30.8k / ebpf 28k。sync 和 ebpf 均掉到 None 的 ~50%，用户期望接近 None。

### 1.2 根因（已核实）

转发在「接收/处理线程」的关键路径上：

- **sync 路径**：master 的 [repl_broadcast](src/main/kvstore.c#L509) 在 reactor 命令处理线程执行——`queue_bytes` 进 slave out_ring，loop 尾由 reactor 的 `flush_conn_output` 统一发送。reactor 同时处理「50 客户端收包 + slave 发包」，双 I/O 挤占关键路径。
- **ebpf 路径**：ebpf_proxy 主循环（[main.c:227](src/ebpf_proxy/main.c#L227)）单线程 `ring_buffer__poll`（读 master 拦截的收包）+ `batch_flush`（writev 到 slave）。**slave fd 是阻塞 socket**（[proxy_slave.c:39](src/ebpf_proxy/proxy_slave.c#L39)，SO_SNDTIMEO 1s、无 O_NONBLOCK）——慢 slave 时 writev 阻塞 → poll 卡住 → ringbuf 满 → master 的 `tcp_recvmsg` 被 BPF 钩子阻塞 → master 收包变慢 → QPS 掉。

### 1.3 场景定位

- **sync**：仅测试数据对比用；生产不依赖。
- **ebpf（master+proxy+slave）**：实际生产路径。

### 1.4 目标

- 转发从「接收/处理线程」剥离到**独立转发线程**（各自单线程 + FIFO，保持顺序）。
- sync 和 ebpf 的 QPS 接近 None（reactor / proxy 主线程不再被转发拖累）。
- 全量同步与数据完整性不回归。

## 2. 设计约束（硬性）

1. **顺序保持**：复制流必须按序（slave 顺序应用）。各转发路径单线程 + FIFO。
2. **增量与全量互斥**：全量同步期间（`g_repl_fullsync_in_progress`）增量转发被抑制——同一时刻只有一个写者，避免 fd 并发写。
3. **队列限长反压**：转发队列/发送缓冲有上限，满时反压生产端（reactor / proxy 主线程），不无限堆积内存。
4. **非阻塞发送**（master 转发线程）：慢 slave 不卡住其它 slave 与 reactor。
5. **conn 关闭**：slave 断开时清理转发线程的发送缓冲，防 UAF。
6. 保持公共 API 与现有复制协议不变。

## 3. Part 1：master 转发线程（sync 路径）

### 3.1 数据流

```
reactor 命令处理:
  repl_broadcast → 深拷贝 raw → 入队(conn_t*, buf, len)     [不再 queue_bytes 到 slave out_ring]

转发线程（单线程）:
  消费队列 → 按 slave 维护发送缓冲（转发线程拥有）
    → 非阻塞 write(slave_fd)，EPOLLOUT 自管（自有 epoll，发送缓冲非空时等 EPOLLOUT）
    → 更新 c->repl_offset_sent / c->repl_last_send_ms

slave 读路径（REPLACK/全量命令）留在 reactor
reactor flush_conn_output 仍 flush replica conn（全量 repl_send_chunked 走 out_ring）
```

### 3.2 组件

- **转发队列**：FIFO，项 `{ conn_t *c, unsigned char *buf, size_t len }`，`buf` 为深拷贝（reactor 的 raw 在 conn inbuf，会复用）。**队列限长**（如 `MAX_FWD_QUEUE_BYTES`，默认 64MB），满时 reactor 的 `repl_broadcast` 等待（背压）。
- **每 slave 发送缓冲**：转发线程维护 `{ fd, out_buf, out_len, out_cap }`（独立于 conn_t 的 out_ring）。非阻塞写。
- **转发线程 epoll**：自有 epoll，注册 slave fd 的 EPOLLOUT；缓冲非空时等 EPOLLOUT 继续写；空闲时阻塞在「队列 + epoll」双事件。
- **slave 连接表**：转发线程从 `g_replicas` 读取当前 slave 集（持 `g_repl_lock`）。reactor 增删 slave、转发线程只读表 + 写各自 fd。

### 3.3 并发边界

- 转发线程只碰：slave fd、自己的发送缓冲、`c->repl_offset_sent` / `c->repl_last_send_ms`（这些字段从 reactor 移交给转发线程独占，reactor 不再写）。
- reactor 仍管：conn 读路径（REPLACK 解析）、全量写（`repl_send_chunked` → out_ring → flush_conn_output）。
- **互斥**：全量期间增量抑制（现 `g_repl_fullsync_in_progress` 逻辑保留）→ 同一 slave fd 增量（转发线程）与全量（reactor）不同时写。
- **conn 关闭**：reactor 断开 slave 时，通知转发线程移除该 conn 的发送缓冲 + 从队列剔除待发项（对齐现有 `persist_purge_conn` 思路）。转发线程的缓冲在 conn 关闭后由转发线程清理。

### 3.4 背压与错误

- 队列满：`repl_broadcast` 等待（如条件变量/自旋+短暂等待），限长防止内存爆炸。
- 发送失败（EAGAIN 持续 / 连接断开）：转发线程把该项喂入 backlog（`repl_backlog_feed`，现逻辑），由 slave 通过 REPLACK 追回。

## 4. Part 2：ebpf_proxy 转发线程（ebpf 路径）

### 4.1 数据流

```
proxy 主线程:  ring_buffer__poll → 回调累计 payload 进批缓冲 → 批量入队(FIFO)
转发线程（单线程）:  消费队列 → writev 到 slave（阻塞 OK，带 SO_SNDTIMEO）
```

### 4.2 组件

- **转发队列**：FIFO，项为一批 payload（深拷贝进稳定缓冲，沿用现 `g_batch_buf` 的拷贝习惯）。**队列按字节限长**（`MAX_FWD_QUEUE_BYTES`，默认 64MB），满时 proxy 主线程等待（背压——此时等价于现「ringbuf 满反压」语义，但只卡 proxy 不卡 master 收包）。
- **转发线程**：消费队列，组 iovec，`writev` 到 slave（阻塞 fd 可直接阻塞；慢 slave 只卡转发线程，不卡 `ring_buffer__poll`）。
- 保留：fullsync 状态机（BUFFERING/FORWARDING）、cache（超大 payload / 断连重连缓存）、`REPLDONE` 信号、slave 断连重连逻辑。

### 4.3 并发边界

- proxy 主线程：只读 ringbuf + 入队。
- 转发线程：出队 + writev。
- `g_batch_buf`/`g_batch_count`/`g_cache`/`g_state` 的跨线程访问需锁（主线程写、转发线程读），或把批缓冲整个移交（主线程入队整批指针、转发线程 writev 后回收）。

### 4.4 背压与错误

- 队列满：proxy 主线程等待（背压）。
- writev 失败：沿用现 `batch_flush` 逻辑（EAGAIN → cache；错误 → cache + break）。

## 5. 验证计划

1. **构建 + 现有测试**：`make`，复制相关测试。
2. **sync 对比**：master + slave（TCP），`redis-benchmark -c 50 -P 1 -d 64 HSET` 打 master，None vs sync 对比——**sync 应接近 None**（不再被 slave 发送拖累）。
3. **ebpf 对比**：master + proxy + slave，None vs ebpf——**ebpf 应接近 None**（proxy 的 ringbuf 反压解除）。
4. **数据完整性**：slave 全收 + FNV-1a hash 与 master 一致（README 口径）。
5. **全量同步回归**：fullsync 不破坏（增量/全量互斥验证）。
6. **慢 slave**：限速 slave，验证队列限长反压 + 非阻塞不卡 reactor/其它 slave。
7. **conn 关闭**：运行中断开 slave，无 UAF、无泄漏、无卡死。

## 6. 风险与取舍

- **并发复杂度**：转发线程 + 队列 + conn 关闭竞态（最高风险，参考 `persist_purge_conn` 思路）。
- **顺序**：单转发线程 FIFO 保证；队列项必须按 master 处理顺序入队。
- **全量互斥**：若互斥逻辑有漏，增量/全量并发写 fd 会破坏数据——重点测试。
- **sync 仅测试用**：master 转发线程的价值在测试对比；生产收益在 Part 2（ebpf）。

## 7. 改动文件（预估）

| 文件 | 改动 |
|---|---|
| `src/replication/kvs_repl.c` | master 转发线程、队列、发送缓冲、EPOLLOUT、背压 |
| `src/main/kvstore.c` | `repl_broadcast` 改入队；slave 关闭通知转发线程 |
| `src/core/reactor.c` | flush 逻辑配合（replica 增量不再走 out_ring）；可能注册转发线程 epoll fd |
| `src/ebpf_proxy/main.c` | proxy 转发线程、队列、主线程入队 |
| `src/ebpf_proxy/proxy_slave.c` | 如需线程安全调整 |
