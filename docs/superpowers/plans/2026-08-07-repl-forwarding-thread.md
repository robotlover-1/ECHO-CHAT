# 复制转发独立线程实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把主从复制的数据转发从「接收/处理线程」剥离到独立转发线程（master 转发线程 + ebpf_proxy 转发线程），使 sync 和 ebpf 的转发 QPS 接近 None。

**Architecture:** 两个独立转发线程，各自单线程 + FIFO 队列保持复制顺序。master 侧：`repl_broadcast` 深拷贝入队，转发线程按 slave 维护发送缓冲非阻塞 send（自有 EPOLLOUT）。proxy 侧：主线程 `ring_buffer__poll` 累计入队，转发线程 `writev` 到 slave（阻塞 OK，不卡 poll）。

**Tech Stack:** C，pthread，epoll，eventfd，io_uring（proxy ringbuf）。

## Global Constraints

- 规格文件：`docs/superpowers/specs/2026-08-07-repl-forwarding-thread-design.md`。
- **顺序保持**：各转发路径单线程 + FIFO，复制流必须按序。
- **增量与全量互斥**：全量同步期间（`g_repl_fullsync_in_progress`）增量转发抑制——同一 slave fd 同一时刻只有一个写者。
- **队列限长反压**：转发队列按字节限长 `MAX_FWD_QUEUE_BYTES = 64MB`，满时生产端等待。
- **conn 关闭清理**：slave 断开时清理转发线程的待发项/发送缓冲，防 UAF。
- 保持公共 API 与现有复制协议不变。同步模式/AOF 不动。
- 每任务结束 `make` 通过 + 对应验证，再 commit。**只 stage 本任务涉及文件**（工作区有无关未提交改动，勿 `git add -A`）。

---

### Task 1: master 转发队列 + 转发线程骨架

**Files:**
- Modify: `src/replication/kvs_repl.c`

**Interfaces:**
- Produces: `repl_fwd_enqueue(conn_t *c, const unsigned char *buf, size_t len)`（reactor 调）、`repl_fwd_start()` / `repl_fwd_stop()`（master 启停）、`g_fwd_queue_bytes` / `MAX_FWD_QUEUE_BYTES`、转发线程 `repl_fwd_thread_main()`。

- [ ] **Step 1: 新增转发队列数据结构与线程声明**

在 `kvs_repl.c` 顶部（`g_repl_lock` 附近）新增：

```c
/* ---- 复制转发队列（T：转发从 reactor 剥离，单转发线程 FIFO） ---- */
typedef struct repl_fwd_node_s {
    conn_t *c;
    unsigned char *buf;          /* 深拷贝（reactor 的 raw 在 conn inbuf，会复用） */
    size_t len;
    struct repl_fwd_node_s *next;
} repl_fwd_node_t;

static pthread_mutex_t g_fwd_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_fwd_cond = PTHREAD_COND_INITIALIZER;
static repl_fwd_node_t *g_fwd_head = NULL, *g_fwd_tail = NULL;
static size_t g_fwd_queue_bytes = 0;
static int g_fwd_stop = 0;
static pthread_t g_fwd_thread;
#define MAX_FWD_QUEUE_BYTES (64 * 1024 * 1024)
```

- [ ] **Step 2: 入队 + 出队函数**

```c
/* reactor 调：深拷贝 raw 入队，队列满则等待（背压）。返回 0 成功 / -1 停止中。 */
int repl_fwd_enqueue(conn_t *c, const unsigned char *buf, size_t len) {
    pthread_mutex_lock(&g_fwd_lock);
    while (g_fwd_queue_bytes + len > MAX_FWD_QUEUE_BYTES && !g_fwd_stop)
        pthread_cond_wait(&g_fwd_cond, &g_fwd_lock);
    if (g_fwd_stop) { pthread_mutex_unlock(&g_fwd_lock); return -1; }
    repl_fwd_node_t *n = (repl_fwd_node_t *)kvs_malloc(sizeof(*n) + len);
    if (!n) { pthread_mutex_unlock(&g_fwd_lock); return -1; }
    n->c = c;
    n->buf = (unsigned char *)(n + 1);
    n->len = len;
    memcpy(n->buf, buf, len);
    n->next = NULL;
    if (g_fwd_tail) g_fwd_tail->next = n; else g_fwd_head = n;
    g_fwd_tail = n;
    g_fwd_queue_bytes += len;
    pthread_cond_signal(&g_fwd_cond);
    pthread_mutex_unlock(&g_fwd_lock);
    return 0;
}

static repl_fwd_node_t *repl_fwd_dequeue(void) {
    pthread_mutex_lock(&g_fwd_lock);
    while (!g_fwd_head && !g_fwd_stop)
        pthread_cond_wait(&g_fwd_cond, &g_fwd_lock);
    repl_fwd_node_t *n = g_fwd_head;
    if (n) { g_fwd_head = n->next; if (!g_fwd_head) g_fwd_tail = NULL; g_fwd_queue_bytes -= n->len; }
    pthread_mutex_unlock(&g_fwd_lock);
    return n;
}
```

- [ ] **Step 3: 转发线程骨架（发送逻辑 Task 2 填）**

```c
static void *repl_fwd_thread_main(void *arg) {
    (void)arg;
    while (!g_fwd_stop) {
        repl_fwd_node_t *n = repl_fwd_dequeue();
        if (!n) break;                    /* 停止 */
        /* TODO(Task 2): 按 n->c 追加到其发送缓冲并触发非阻塞 send */
        kvs_free(n);
    }
    return NULL;
}

int repl_fwd_start(void) {
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_stop = 0;
    pthread_mutex_unlock(&g_fwd_lock);
    return pthread_create(&g_fwd_thread, NULL, repl_fwd_thread_main, NULL) == 0 ? 0 : -1;
}
void repl_fwd_stop(void) {
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_stop = 1;
    pthread_cond_broadcast(&g_fwd_cond);
    pthread_mutex_unlock(&g_fwd_lock);
    pthread_join(g_fwd_thread, NULL);
    /* 清理残余队列 */
    for (repl_fwd_node_t *n = g_fwd_head; n; ) {
        repl_fwd_node_t *nx = n->next; kvs_free(n); n = nx;
    }
    g_fwd_head = g_fwd_tail = NULL; g_fwd_queue_bytes = 0;
}
```

- [ ] **Step 4: 构建**

```bash
make -j4 2>&1 | grep -E "error" | head -3
```
Expected: 编译通过（线程未接入，仅新增符号，可能有 unused 警告——`repl_fwd_start/stop` 暂未调用，可接受或加 `__attribute__((unused))`）。

- [ ] **Step 5: Commit**

```bash
git add src/replication/kvs_repl.c
git commit -m "feat(repl): 转发队列 + 转发线程骨架"
```

---

### Task 2: repl_broadcast 改入队 + 转发线程非阻塞发送

**Files:**
- Modify: `src/replication/kvs_repl.c`
- Modify: `src/main/kvstore.c`（`repl_broadcast` 改入队）

**Interfaces:**
- Consumes: Task 1 的 `repl_fwd_enqueue` / 队列。
- Produces: 转发线程每 slave 发送缓冲 `repl_fwd_send_state_t`、EPOLLOUT 处理、`repl_fwd_purge_conn(conn_t *c)`（Task 3 用）、master 启停接线。

- [ ] **Step 1: `repl_broadcast` 改为入队（不再 queue_bytes）**

改 `src/main/kvstore.c` 的 `repl_broadcast`：遍历 `g_replicas` 时，对**增量可发**的 slave（非 draining、非 fullsync_pending、非 ebpf+tcp、非 fwd_healthy），把 `(c, raw, rawlen)` 入队；backlog/offset 更新交给转发线程（Task 3 明确）。**保留** `g_repl_fullsync_in_progress` 时的 `repl_backlog_feed` 分支与 ebpf/fwd_healthy 跳过分支：

```c
void repl_broadcast(const unsigned char *raw, size_t rawlen) {
    pthread_mutex_lock(&g_repl_lock);
    conn_t **pp = &g_replicas;
    while (*pp) {
        conn_t *c = *pp;
        if (c->repl_draining) { *pp = c->next_replica; c->next_replica = NULL; c->is_replica = 0; continue; }
        if (c->repl_fullsync_pending) { pp = &c->next_replica; continue; }
        if (g_repl_fullsync_in_progress) {
            repl_backlog_feed(raw, rawlen);
            pp = &c->next_replica;
            continue;
        }
        if (c->repl_transport_kind == KVS_REPL_TRANSPORT_EBPF_TCP) { pp = &c->next_replica; continue; }
        if (c->fwd_healthy) { pp = &c->next_replica; continue; }
        /* 转发剥离：入队由转发线程发送 */
        if (repl_fwd_enqueue(c, raw, rawlen) != 0) {
            repl_backlog_feed(raw, rawlen);   /* 入队失败（停止/内存）→ backlog 兜底 */
        }
        pp = &c->next_replica;
    }
    pthread_mutex_unlock(&g_repl_lock);
}
```

> 移除原 `repl_realtime_send(c, raw, rawlen)` 调用。`c->repl_offset_sent` / `c->repl_last_send_ms` 更新移到转发线程（Task 3）。

- [ ] **Step 2: 转发线程每 slave 发送状态 + EPOLLOUT**

```c
typedef struct repl_fwd_send_state_s {
    conn_t *c;
    unsigned char *buf;   /* 累积发送缓冲（转发线程拥有，独立于 conn 的 out_ring） */
    size_t len, cap;
    struct repl_fwd_send_state_s *next;
} repl_fwd_send_state_t;

static repl_fwd_send_state_t *g_fwd_states = NULL;   /* 转发线程独占 */
```

转发线程为每个 slave 维护 `send_state`（从 `g_replicas` 惰性建立）。发送：非阻塞 `write(c->fd, buf+off, len-off)`；`EAGAIN` 时注册该 fd 的 EPOLLOUT 到转发线程自有 epoll，等待可写再续发；发送完清零。转发线程循环：

```c
static void repl_fwd_process_one(repl_fwd_node_t *n) {
    repl_fwd_send_state_t *st = repl_fwd_get_or_create(n->c);
    if (!st) return;
    /* 追加到发送缓冲 */
    if (st->len + n->len > st->cap) {
        size_t nc = st->cap ? st->cap * 2 : 4096;
        while (nc < st->len + n->len) nc *= 2;
        unsigned char *nb = kvs_realloc(st->buf, nc);
        if (!nb) { repl_backlog_feed(n->buf, n->len); return; }
        st->buf = nb; st->cap = nc;
    }
    memcpy(st->buf + st->len, n->buf, n->len);
    st->len += n->len;
    repl_fwd_drain_send(st);   /* 非阻塞 send + EAGAIN → EPOLLOUT */
}
```

`repl_fwd_drain_send(st)`：循环 `write(st->c->fd, st->buf+off, st->len-off)`；`n>0` 推进；`EAGAIN/EWOULDBLOCK` 注册 EPOLLOUT 并 break；其它错误 → 丢弃该 slave 发送状态（断连，交给 Task 3 的 conn-close 清理）。成功发完 → `st->c->repl_offset_sent = repl_master_offset(); st->c->repl_last_send_ms = kvs_now_ms();`。

转发线程主循环改为「队列 + 自有 epoll 双事件」：`poll([wake_fd, epoll_fd])`，队列事件 → `repl_fwd_process_one`；EPOLLOUT 事件 → `repl_fwd_drain_send` 对应 slave。

- [ ] **Step 3: 转发线程自有 epoll 初始化**

```c
static int g_fwd_epfd = -1;
static int g_fwd_wake_efd = -1;   /* 队列唤醒（repl_fwd_dequeue 已用 condvar，此 efd 用于 epoll 侧唤醒，可选） */
```

> 简化：转发线程以 `condvar` 等队列 + 每轮非阻塞 `epoll_wait(0)` 扫 EPOLLOUT 即可；`repl_fwd_drain_send` 在 EAGAIN 时把 fd 加入 `g_fwd_epfd`，队列消费间隙 `epoll_wait(g_fwd_epfd, 0)` 处理可写。实现允许用 condvar 超时轮询替代双事件 poll。

- [ ] **Step 4: master 启停接线**

在 `repl_broadcast` 依赖处确保转发线程已启动。master 启动（`main()` 里 repl 初始化后）调 `repl_fwd_start()`；关闭（`repl_close` / master 退出路径）调 `repl_fwd_stop()`。找到 master 的 repl 初始化/关闭函数并接线。

- [ ] **Step 5: 构建 + 基础验证**

```bash
make -j4 2>&1 | grep -E "error" | head -3
```
Run: 起 master + slave（TCP 全量同步成功），`redis-cli` 写几条，确认 slave 收到、`repl_offset` 推进、无崩溃。
Expected: 编译通过、增量复制工作、无 UAF/卡死。

- [ ] **Step 6: Commit**

```bash
git add src/replication/kvs_repl.c src/main/kvstore.c
git commit -m "feat(repl): repl_broadcast 改入队，转发线程非阻塞发送"
```

---

### Task 3: conn 关闭清理 + 全量互斥 + offset 移交

**Files:**
- Modify: `src/replication/kvs_repl.c`
- Modify: `src/core/reactor.c`（`close_conn` 调 purge）
- Modify: `src/main/kvstore.c`（REPLACK 快速追赶 / REPLDONE 回放改入队）

**Interfaces:**
- Consumes: Task 2 的 `g_fwd_states` / 转发线程 / `repl_fwd_enqueue`。
- Produces: `repl_fwd_purge_conn(conn_t *c)`（reactor close_conn 调）、追赶数据经 `repl_fwd_enqueue` 路由、转发线程读 `g_replicas` 的锁纪律、`repl_offset_sent/repl_last_send_ms` 移交确认。

- [ ] **Step 1: `repl_fwd_purge_conn`**

```c
/* reactor 的 close_conn 调：从转发线程队列 + 发送状态移除该 conn（防 UAF）。
 * 转发线程正在处理该 conn 的项时，由 g_fwd_lock 串行化。 */
void repl_fwd_purge_conn(conn_t *c) {
    pthread_mutex_lock(&g_fwd_lock);
    /* 队列中剔除该 conn 的项 */
    repl_fwd_node_t **pp = &g_fwd_head;
    while (*pp) {
        repl_fwd_node_t *n = *pp;
        if (n->c == c) { *pp = n->next; if (g_fwd_tail == n) g_fwd_tail = NULL; g_fwd_queue_bytes -= n->len; kvs_free(n); }
        else pp = &n->next;
    }
    pthread_mutex_unlock(&g_fwd_lock);
    /* 转发线程侧发送状态：用一个 flag（如 conn 已置 repl_draining）让转发线程下次见到就丢弃 */
    c->repl_draining = 1;   /* reactor 已调 repl_remove_slave 置 0，需在 close_conn 顺序上保证 */
}
```

> 注意：`close_conn` 先调 `repl_remove_slave(c)`（置 `is_replica=0`），再 free conn_t。转发线程可能在 `close_conn` free 之后仍引用 `n->c`——需保证：`repl_fwd_purge_conn` 在 free 前调用，且转发线程在处理某 conn 的发送状态时若发现 `c->is_replica==0`（已移除）则丢弃。实现时在转发线程的 `repl_fwd_drain_send` 里检查 `st->c->is_replica`。

- [ ] **Step 2: reactor 接线**

[reactor.c:74-76](src/core/reactor.c#L74) `close_conn` 的 `if (c->is_replica) repl_remove_slave(c);` 后、free 前调用 `repl_fwd_purge_conn(c);`。

- [ ] **Step 3: 追赶路径（REPLACK 快速追赶 / REPLDONE 回放）改走转发线程（T2 review Important 2）**

T2 review 发现：REPLACK 快速追赶（[kvstore.c:1344-1355](src/main/kvstore.c#L1344)）与 REPLDONE 回放（[kvstore.c:1375-1379](src/main/kvstore.c#L1375)）经 `repl_backlog_write_range` → `repl_send_chunked` → reactor out_ring → `send(c->fd)`，与转发线程并发写同一 fd——无串行化，会穿插/破坏复制流顺序。

**Fix**：追赶数据也入转发队列，转发线程成为 c->fd 的唯一写者：
- REPLACK/REPLDONE 的追赶数据：从 `g_repl_backlog` 读该 offset 范围 → **深拷贝**（backlog 是环形缓冲会被覆盖）→ `repl_fwd_enqueue(c, copied, len)`（复用入队，保持 FIFO 顺序）。
- 顺序保证：reactor 单线程处理命令——REPLACK 处理（入队追赶数据）先于后续客户端写（入队增量）→ 转发线程 FIFO 先发追赶再发增量。
- 移除追赶路径对 `repl_send_chunked`（直接 send）的调用；确认 `repl_send_chunked` 只用于全量（与增量互斥）。
- `c->repl_offset_sent / repl_last_send_ms`：确认 reactor 不再写（grep 确认无其它写点），交由转发线程独占。

- [ ] **Step 4: 全量互斥确认**

- 全量期间 `repl_broadcast` 已被 `g_repl_fullsync_in_progress` 抑制入队 → 转发线程不会发增量；reactor 的 `repl_send_chunked`（全量）走 conn out_ring。确认两者不同时写同一 slave fd（追赶已路由转发线程后，唯一并发窗口只剩全量 vs 增量，由抑制保证）。

- [ ] **Step 5: 构建 + 验证**

```bash
make -j4 2>&1 | grep -E "error" | head -3
```
Run: 起 master+slave 增量同步中 `redis-cli -p <master> CLIENT KILL` 断 slave（或 slave 端直接关），确认无 UAF/崩溃/泄漏；重连后继续同步。
Run: 全量同步（重启 slave 触发）成功，增量/全量不穿插。
Run: 制造追赶场景（暂停 slave 读、恢复），确认 REPLACK 追赶数据经转发线程按序送达（无穿插、offset 正确）。
Expected: 通过。

- [ ] **Step 6: Commit**

```bash
git add src/replication/kvs_repl.c src/core/reactor.c src/main/kvstore.c
git commit -m "feat(repl): conn 关闭清理 + 追赶路由转发线程 + 全量互斥 + offset 移交"
```

---

### Task 4: ebpf_proxy 转发队列 + 转发线程

**Files:**
- Modify: `src/ebpf_proxy/main.c`
- Modify: `src/ebpf_proxy/proxy_slave.c`（如需）

**Interfaces:**
- Consumes: 现有 `g_batch_buf`/`g_batch_count`、`batch_flush`、`cache_append`、`g_state`、`g_slave`。
- Produces: 转发队列 `proxy_fwd_enqueue` / 转发线程 `proxy_fwd_thread_main`、`proxy_fwd_start/stop`。

- [ ] **Step 1: 转发队列 + 线程**

```c
typedef struct proxy_fwd_node_s {
    unsigned char *buf; size_t len;
    struct proxy_fwd_node_s *next;
} proxy_fwd_node_t;

static pthread_mutex_t g_pfwd_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_pfwd_cond = PTHREAD_COND_INITIALIZER;
static proxy_fwd_node_t *g_pfwd_head = NULL, *g_pfwd_tail = NULL;
static size_t g_pfwd_bytes = 0;
static int g_pfwd_stop = 0;
static pthread_t g_pfwd_thread;
#define MAX_PFWD_QUEUE_BYTES (64 * 1024 * 1024)

static void proxy_fwd_enqueue(const unsigned char *buf, size_t len) {
    pthread_mutex_lock(&g_pfwd_lock);
    while (g_pfwd_bytes + len > MAX_PFWD_QUEUE_BYTES && !g_pfwd_stop)
        pthread_cond_wait(&g_pfwd_cond, &g_pfwd_lock);
    if (g_pfwd_stop) { pthread_mutex_unlock(&g_pfwd_lock); return; }
    proxy_fwd_node_t *n = (proxy_fwd_node_t *)kvs_malloc(sizeof(*n) + len);
    if (!n) { pthread_mutex_unlock(&g_pfwd_lock); return; }
    n->buf = (unsigned char *)(n + 1); n->len = len;
    memcpy(n->buf, buf, len); n->next = NULL;
    if (g_pfwd_tail) g_pfwd_tail->next = n; else g_pfwd_head = n;
    g_pfwd_tail = n; g_pfwd_bytes += len;
    pthread_cond_signal(&g_pfwd_cond);
    pthread_mutex_unlock(&g_pfwd_lock);
}
```

- [ ] **Step 2: 转发线程（writev 到 slave，阻塞 OK）**

```c
static void *proxy_fwd_thread_main(void *arg) {
    (void)arg;
    while (!g_pfwd_stop) {
        pthread_mutex_lock(&g_pfwd_lock);
        while (!g_pfwd_head && !g_pfwd_stop) pthread_cond_wait(&g_pfwd_cond, &g_pfwd_lock);
        proxy_fwd_node_t *n = g_pfwd_head;
        if (n) { g_pfwd_head = n->next; if (!g_pfwd_head) g_pfwd_tail = NULL; g_pfwd_bytes -= n->len; }
        pthread_mutex_unlock(&g_pfwd_lock);
        if (!n) break;
        if (g_state == STATE_FORWARDING && proxy_slave_is_connected(&g_slave)) {
            struct iovec iov = { n->buf, n->len };
            writev(proxy_slave_fd(&g_slave), &iov, 1);   /* 失败走现 batch_flush 的 cache 逻辑 */
        } else {
            cache_append(&g_cache, n->buf, n->len);
        }
        kvs_free(n);
    }
    return NULL;
}
```

- [ ] **Step 3: 主线程入队替代 batch_flush**

`ringbuf_callback` 里：FORWARDING 时不再填 `g_batch_buf`，改为 `proxy_fwd_enqueue(payload, plen)`（超大 payload 仍 `cache_append`）。`main_loop` 移除 `batch_flush()` 调用（转发线程负责发送）。BUFFERING 分支保留 `cache_append`。

- [ ] **Step 4: 启停接线**

`main()` 启动转发线程 `pthread_create(&g_pfwd_thread, ...)`；退出时 `g_pfwd_stop=1; cond_broadcast; join`。

- [ ] **Step 5: 构建 + 验证**

```bash
make -j4 2>&1 | grep -E "error" | head -3
```
Run: master+proxy+slave 全量同步 + 增量，slave 全收、数据一致。
Expected: 编译通过、ebpf 转发工作、数据完整。

- [ ] **Step 6: Commit**

```bash
git add src/ebpf_proxy/main.c src/ebpf_proxy/proxy_slave.c
git commit -m "feat(ebpf-proxy): 转发队列 + 独立转发线程"
```

---

### Task 5: 构建 + 全量验证

**Files:**
- Create: `docs/superpowers/bench/2026-08-07-repl-fwd-thread.md`

- [ ] **Step 1: 全量构建 + 现有测试**

```bash
make -j4 2>&1 | grep -E "error" | head -3
make test_persist_aof_demo  # 回归（不影响但确认）
```

- [ ] **Step 2: sync 对比（master+slave TCP）**

起 master（`--repl-realtime-transport tcp`）+ slave（TCP 全量同步完成）。`redis-benchmark -c 50 -P 1 -d 64 HSET` 打 master，记 None（无 slave）与 sync 的 QPS。预期：sync 接近 None（不再被 slave 发送拖累）。

- [ ] **Step 3: ebpf 对比（master+proxy+slave）**

起 master + ebpf_proxy（fentry+fexit）+ slave。同样 benchmark，记 None 与 ebpf 的 QPS。预期：ebpf 接近 None（proxy ringbuf 反压解除）。

- [ ] **Step 4: 数据完整性 + 全量回归**

slave 全收 + FNV-1a hash 与 master 一致（README 口径）。重启 slave 触发全量，确认增量/全量不穿插、数据完整。

- [ ] **Step 5: 慢 slave / 断连**

限速 slave（`tc netem` 或小 SO_RCVBUF）验证队列限长反压不卡 reactor；运行中断 slave 验证无 UAF。

- [ ] **Step 6: 记录结果 + Commit**

写 `docs/superpowers/bench/2026-08-07-repl-fwd-thread.md`（None/sync/ebpf 对比表）。如实报告提升与回退。

```bash
git add docs/superpowers/bench/2026-08-07-repl-fwd-thread.md
git commit -m "bench: 转发线程后 sync/ebpf QPS 对比"
```

---

## 自检记录

- **Spec 覆盖**：spec §3（master 转发线程）→ T1/T2/T3；§4（proxy 转发线程）→ T4；§5（验证）→ T5。全覆盖。
- **占位符**：Task 2 Step 2 的 EPOLLOUT 细节给了实现自由度（condvar+epoll_wait(0) 轮询替代双事件 poll），非占位——是明确允许的简化。
- **类型一致性**：`repl_fwd_enqueue/dequeue/start/stop/purge_conn`、`proxy_fwd_enqueue`、`g_fwd_lock/g_pfwd_lock` 各任务间一致。
