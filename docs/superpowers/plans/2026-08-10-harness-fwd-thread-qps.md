# harness sync 独立转发线程 + 跨机 QPS 复测 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `tests/perf/test_ebpf_hset_qps.c` 的 sync 模式加独立转发线程（handler 入队 + 转发线程发送），用统一 harness 跨机重测 none/sync/ebpf QPS，验证 sync 和 ebpf 都接近 none。

**Architecture:** sync 转发从「handler 内联 `write_full` + 全局锁」改为「深拷贝入队（有界 64MB FIFO + condvar 背压）→ 独立 pthread 消费、释放锁后 `write_full` 到 slave」。ebpf 不动，沿用生产 proxy（已含 T4 转发线程）。

**Tech Stack:** C，pthread，condvar，sshpass（.129 编排），redis-benchmark 7.2.9，eBPF（ebpf 模式需 sudo/CAP_BPF）。

## Global Constraints

- 规格文件：`docs/superpowers/specs/2026-08-10-harness-fwd-thread-qps-design.md`。
- **只改** `tests/perf/test_ebpf_hset_qps.c`（+ 新 bench 文档）；生产代码（src/）不动。
- 有界队列 `MAX_FWD_QUEUE_BYTES = 64MB`，满时 handler `cond_wait` 背压。
- **单消费者 FIFO 保序**（RESP 流必须按序）；写 slave 时**不持队列锁**（dequeue 短持锁）。
- `g_slave_fd_lock` 删除（slave_fd 只由转发线程写）。
- 工作区有无关改动：**只 stage 本任务涉及文件，勿 `git add -A`**。
- 拓扑：master+proxy+client 在 `.128`（本机，非 root，ebpf 用 sudo）；slave 在 `.129`，`/home/pp/slave_receiver`（sshpass 密码 `2983372202`）。
- 杀进程用 `pkill -x`（勿用 `pkill -f`，会自杀 shell）。
- 测量：先小数据量冒烟（`--count 50000 --rounds 4`），正式（`--count 1000000 --rounds 6`）；三模式同编排，取后 R-1 轮中位数。

---

### Task 1: harness sync 转发队列 + 独立转发线程

**Files:**
- Modify: `tests/perf/test_ebpf_hset_qps.c`
- Test: 本机 `slave_receiver` 冒烟（不依赖跨机）

**Interfaces:**
- Consumes: `write_full(int fd, const void *buf, size_t len)`（已存在，错误返回 -1、EPIPE break 返回部分长度）、`g_slave_fd`、`g_mode`。
- Produces: `fwd_enqueue(const unsigned char *buf, size_t len) -> int`（handler 调，0=入队 / -1=丢弃）、`fwd_thread_start()` / `fwd_thread_stop()`（sync setup/teardown 调）。

- [ ] **Step 1: 替换 sync 全局块，加转发队列结构 + 全局**

把现有（[test_ebpf_hset_qps.c:241-243](src/../../tests/perf/test_ebpf_hset_qps.c#L241-L243)）：
```c
/* sync 模式共享 slave fd */
static int g_slave_fd = -1;
static pthread_mutex_t g_slave_fd_lock = PTHREAD_MUTEX_INITIALIZER;
```
替换为：
```c
/* sync 模式共享 slave fd（仅转发线程写） */
static int g_slave_fd = -1;

/* ---- sync 转发队列 + 独立转发线程 ---- */
typedef struct fwd_node_s {
    unsigned char *buf;            /* 深拷贝 raw RESP（内联在节点内） */
    size_t len;
    struct fwd_node_s *next;
} fwd_node_t;

static pthread_mutex_t g_fwd_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_fwd_cond = PTHREAD_COND_INITIALIZER;
static fwd_node_t *g_fwd_head = NULL, *g_fwd_tail = NULL;
static size_t g_fwd_queue_bytes = 0;
static int g_fwd_stop = 0;
static int g_fwd_dead = 0;         /* slave 写失败后置 1，后续入队丢弃 */
static pthread_t g_fwd_thread;
#define MAX_FWD_QUEUE_BYTES (64 * 1024 * 1024)
```

- [ ] **Step 2: 新增入队 + 转发线程 + 启停函数**

在 Step 1 的全局块之后、`client_handler`（约 [test_ebpf_hset_qps.c:401](src/../../tests/perf/test_ebpf_hset_qps.c#L401)）之前插入：

```c
/* handler 调：深拷贝 raw RESP 入队；队列满则等待（背压）。返回 0 成功 / -1 丢弃。 */
static int fwd_enqueue(const unsigned char *buf, size_t len) {
    pthread_mutex_lock(&g_fwd_lock);
    while (g_fwd_queue_bytes + len > MAX_FWD_QUEUE_BYTES && !g_fwd_stop && !g_fwd_dead)
        pthread_cond_wait(&g_fwd_cond, &g_fwd_lock);
    if (g_fwd_stop || g_fwd_dead) { pthread_mutex_unlock(&g_fwd_lock); return -1; }
    fwd_node_t *n = (fwd_node_t *)malloc(sizeof(*n) + len);
    if (!n) { pthread_mutex_unlock(&g_fwd_lock); return -1; }
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

/* 转发线程：dequeue（短持锁）→ 释放锁 → write_full。单消费者保序。 */
static void *fwd_thread_main(void *arg) {
    (void)arg;
    while (1) {
        pthread_mutex_lock(&g_fwd_lock);
        while (!g_fwd_head && !g_fwd_stop)
            pthread_cond_wait(&g_fwd_cond, &g_fwd_lock);
        if (g_fwd_stop && !g_fwd_head) { pthread_mutex_unlock(&g_fwd_lock); break; }
        fwd_node_t *n = g_fwd_head;
        if (n) {
            g_fwd_head = n->next;
            if (!g_fwd_head) g_fwd_tail = NULL;
            g_fwd_queue_bytes -= n->len;
        }
        pthread_mutex_unlock(&g_fwd_lock);

        if (!n) break;
        if (g_slave_fd >= 0) {
            if (write_full(g_slave_fd, n->buf, n->len) != (ssize_t)n->len) {
                fprintf(stderr, "[fwd] slave write failed/partial: %s\n", strerror(errno));
                pthread_mutex_lock(&g_fwd_lock);
                g_fwd_dead = 1;
                pthread_cond_broadcast(&g_fwd_cond);
                pthread_mutex_unlock(&g_fwd_lock);
            }
        }
        free(n);
    }
    return NULL;
}

static void fwd_thread_start(void) {
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_stop = 0;
    g_fwd_dead = 0;
    pthread_mutex_unlock(&g_fwd_lock);
    pthread_create(&g_fwd_thread, NULL, fwd_thread_main, NULL);
}

static void fwd_thread_stop(void) {
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_stop = 1;
    pthread_cond_broadcast(&g_fwd_cond);
    pthread_mutex_unlock(&g_fwd_lock);
    pthread_join(g_fwd_thread, NULL);
    pthread_mutex_lock(&g_fwd_lock);
    for (fwd_node_t *n = g_fwd_head; n; ) {
        fwd_node_t *nx = n->next;
        free(n);
        n = nx;
    }
    g_fwd_head = g_fwd_tail = NULL;
    g_fwd_queue_bytes = 0;
    pthread_mutex_unlock(&g_fwd_lock);
}
```

- [ ] **Step 3: `client_handler` sync 分支改入队**

把 [test_ebpf_hset_qps.c:443-448](src/../../tests/perf/test_ebpf_hset_qps.c#L443-L448)：
```c
                /* sync 转发: 写原始 RESP 字节到 slave */
                if (g_mode == 1 && g_slave_fd >= 0) {
                    pthread_mutex_lock(&g_slave_fd_lock);
                    write_full(g_slave_fd, cb->data + cb->tail,
                               (size_t)consumed);
                    pthread_mutex_unlock(&g_slave_fd_lock);
                }
```
替换为：
```c
                /* sync 转发: 深拷贝入队，由独立转发线程发送（不再阻塞本 handler） */
                if (g_mode == 1 && g_slave_fd >= 0) {
                    (void)fwd_enqueue(cb->data + cb->tail, (size_t)consumed);
                }
```
（入队在 `cb_consume(cb, consumed)` 之前，数据仍有效。）

- [ ] **Step 4: run_one_mode 接线程启停 + slave fd 加 SO_SNDTIMEO**

sync connect 块（[test_ebpf_hset_qps.c:650-666](src/../../tests/perf/test_ebpf_hset_qps.c#L650-L666)）里 `else { int snd = 262144; setsockopt(SO_SNDBUF ...); }` 分支内追加 SO_SNDTIMEO（防止 slave 停止读时转发线程卡死在 `write_full`，导致 `fwd_thread_stop()` 的 join 挂起）：
```c
            struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
            setsockopt(g_slave_fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
```
（SO_SNDTIMEO 超时 → `write()` 返回 EAGAIN → `write_full` 返回 -1 → 转发线程置 `g_fwd_dead`，`pthread_join` 可正常返回。）

connect 块结束处（`if (g_mode == 1) { ... }` 之后）插入：
```c
    /* sync 模式：连上 slave 后启动转发线程 */
    if (g_mode == 1 && g_slave_fd >= 0)
        fwd_thread_start();
```

teardown（[test_ebpf_hset_qps.c:710](src/../../tests/perf/test_ebpf_hset_qps.c#L710) `if (g_slave_fd >= 0) { close(g_slave_fd); g_slave_fd = -1; }`）之前插入：
```c
    if (g_mode == 1 && g_slave_fd >= 0) fwd_thread_stop();
```

- [ ] **Step 5: 构建**

Run: `make test_ebpf_hset_qps 2>&1 | grep -E "error|warning" | head; ls -la tests/perf/test_ebpf_hset_qps`
Expected: 无 error（`g_slave_fd_lock` 已无引用；`g_ht_lock` 等其他锁不受影响）。

- [ ] **Step 6: 本地冒烟（本机 slave_receiver + harness sync）**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
pkill -x slave_receiver 2>/dev/null; pkill -x kvstore 2>/dev/null; sleep 0.3
gcc -O2 -o /tmp/slave_receiver tests/perf/slave_receiver.c
/tmp/slave_receiver 15901 15902 > /tmp/slave_smoke.txt 2>&1 &
sleep 0.5
./tests/perf/test_ebpf_hset_qps --mode sync --payload 64 --count 5000 --rounds 3 \
  --slave-host 127.0.0.1 2>&1 | tail -8
sleep 1
echo "--- slave 收到 ---"; cat /tmp/slave_smoke.txt
pkill -INT -x slave_receiver
```
Expected: harness 打印 sync 模式 QPS（无 `[fwd]` 错误、无崩溃）；slave_receiver 的 `msgs>`0 且 `bytes>`0。

- [ ] **Step 7: Commit**

```bash
git add tests/perf/test_ebpf_hset_qps.c
git commit -m "feat(perf): harness sync 转发改独立线程（入队+转发线程，去阻塞）"
```

---

### Task 2: 跨机小数据量冒烟（-n 50000 -r 4）

**Files:**
- Test: 跨机编排（.128 master/proxy/client / .129 slave）
- 无代码改动；结果记入 `docs/superpowers/bench/2026-08-10-harness-fwd-thread-qps.md`（草稿，Task 3 定稿）。

**Interfaces:**
- Consumes: Task 1 的 `test_ebpf_hset_qps`（sudo，`--cpu 2`）。

- [ ] **Step 1: 构建产物确认**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
make ebpf_proxy 2>&1 | tail -2
grep -c "proxy_fwd_thread_main" src/ebpf_proxy/main.c   # 期望 ≥1（T4 转发线程存在）
ls -la build/ebpf_proxy tests/perf/test_ebpf_hset_qps
```
Expected: `build/ebpf_proxy` 构建成功且含 T4；harness 为 Task 1 新构建。

- [ ] **Step 2: .129 起 slave_receiver（每模式重起）**

```bash
rssh() { sshpass -p '2983372202' ssh -o StrictHostKeyChecking=no pp@192.168.233.129 "$@"; }
rssh "pkill -9 -x slave_receiver 2>/dev/null; sleep 0.2; nohup /home/pp/slave_receiver 15901 > /tmp/slave.txt 2>&1 & sleep 0.8"
rssh "cat /tmp/slave.txt"   # 期望 [slave] DATA port=15901 ...
```

- [ ] **Step 3: 依次跑 none / sync / ebpf（sudo，-n 50000 -r 4）**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
for m in none sync ebpf; do
  rssh "pkill -9 -x slave_receiver 2>/dev/null; sleep 0.2; nohup /home/pp/slave_receiver 15901 > /tmp/slave.txt 2>&1 & sleep 0.8"
  sudo rm -rf /sys/fs/bpf/kvstore_hset_qps_test 2>/dev/null
  echo "===== $m ====="
  sudo taskset -c 2 ./tests/perf/test_ebpf_hset_qps --mode $m --payload 64 \
    --count 50000 --rounds 4 --slave-host 192.168.233.129 --csv /tmp/harness_$m.csv 2>&1 | tail -4
  sleep 1
  echo "slave: $(rssh 'cat /tmp/slave.txt 2>/dev/null | grep msgs')"
done
```
Expected: 三模式都打印 median QPS；sync/ebpf 下 slave `msgs>0 bytes>0`（none 无转发，slave 可为 0）。把三行中位数记录到 bench 文档草稿。

- [ ] **Step 4: 冒烟判定**

对照成功标准：`sync_median / none_median` 与 `ebpf_median / none_median`。
- 若 sync/ebpf 明显 > README 旧比值（sync 49% / ebpf 44%）且接近 none，判定达成方向正确。
- 若 ebpf 异常低或 slave 没收齐，记录现象并暂停（分析根因，不要直接进 Task 3）。

- [ ] **Step 5: 记录草稿（不提交或提交草稿均可）**

把三模式 median + slave msgs/bytes 写入 `docs/superpowers/bench/2026-08-10-harness-fwd-thread-qps.md`（方法 + 冒烟初值），Task 3 定稿。

---

### Task 4: 多核隔离（转发线程/proxy/客户端分核）+ 重跑冒烟

> **为什么需要**：T2 冒烟（成功标准未达成）根因 = 单核 CPU2 争抢。`taskset -c 2` 把 client（popen 子进程继承亲和）+ 50 handler + sync 转发线程 + ebpf proxy 全 pin 到 CPU2，转发/proxy 抢 master 核周期 → sync=49%、ebpf=31.5%。本任务把三类转发工作 pin 到独立核，重测验证「另起线程不抢 master 核 → sync/ebpf 接近 none」。

**Files:**
- Modify: `tests/perf/test_ebpf_hset_qps.c`

**Interfaces:**
- Consumes: Task 1 的 `fwd_thread_start()` / `proxy_start()` / `run_redis_benchmark()`。
- Produces: 核隔离常量 `MASTER_CPU / FWD_THREAD_CPU / PROXY_CPU / CLIENT_CPU`；重跑冒烟结果。

- [ ] **Step 1: 新增核隔离常量**

在 [test_ebpf_hset_qps.c:55](src/../../tests/perf/test_ebpf_hset_qps.c#L55) 附近（`BPF_PIN_PATH` 定义处）新增：
```c
/* 核隔离（T4 多核方案）：master handler 固定 CPU2（外部 taskset -c 2），
 * 转发线程 / proxy / 客户端各自独立核，不与 master 核争抢。 */
#define MASTER_CPU     2
#define FWD_THREAD_CPU 0
#define PROXY_CPU      0
#define CLIENT_CPU     3
```

- [ ] **Step 2: `fwd_thread_start()` pin 转发线程到 FWD_THREAD_CPU**

把 Task 1 加的 `fwd_thread_start()`（约 [test_ebpf_hset_qps.c:141-147](src/../../tests/perf/test_ebpf_hset_qps.c#L141)）替换为：
```c
static void fwd_thread_start(void) {
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_stop = 0;
    g_fwd_dead = 0;
    pthread_mutex_unlock(&g_fwd_lock);
    pthread_create(&g_fwd_thread, NULL, fwd_thread_main, NULL);
    /* 转发线程 pin 到独立核 FWD_THREAD_CPU，不与 handler 抢 master 核（失败仅告警） */
    cpu_set_t cs; CPU_ZERO(&cs); CPU_SET(FWD_THREAD_CPU, &cs);
    if (pthread_setaffinity_np(g_fwd_thread, sizeof(cs), &cs) != 0)
        fprintf(stderr, "[fwd] pthread_setaffinity_np(%d) failed: %s\n",
                FWD_THREAD_CPU, strerror(errno));
}
```

- [ ] **Step 3: `proxy_start()` fork 子进程 pin 到 PROXY_CPU**

`proxy_start()`（[test_ebpf_hset_qps.c:256-268](src/../../tests/perf/test_ebpf_hset_qps.c#L256-L268)）的 `if (pid == 0) {` 分支内、`execl` 之前插入：
```c
        /* proxy pin 到独立核 PROXY_CPU，不与 master 核争抢 */
        cpu_set_t cs; CPU_ZERO(&cs); CPU_SET(PROXY_CPU, &cs);
        if (sched_setaffinity(0, sizeof(cs), &cs) != 0)
            fprintf(stderr, "[test] proxy sched_setaffinity(%d) failed: %s\n",
                    PROXY_CPU, strerror(errno));
```

- [ ] **Step 4: `run_redis_benchmark()` 本机分支 popen 改 fork/exec + pin 客户端到 CLIENT_CPU**

[test_ebpf_hset_qps.c:658-678](src/../../tests/perf/test_ebpf_hset_qps.c#L658-L678) 的：
```c
    FILE *fp = popen(cmd, "r");
    if (!fp) { perror("popen"); return r; }
```
替换为（本机客户端子进程重置亲和到 CLIENT_CPU，否则 popen 子进程继承 master 的 CPU2）：
```c
    /* 本机客户端：fork + pin CLIENT_CPU + 管道读 stdout（popen 子进程会继承 master 亲和，需重置） */
    int pfd[2];
    if (pipe(pfd) < 0) { perror("pipe"); return r; }
    pid_t cpid = fork();
    if (cpid < 0) { perror("fork"); close(pfd[0]); close(pfd[1]); return r; }
    if (cpid == 0) {
        /* 子进程：stdout → 管道，pin 到 CLIENT_CPU，exec sh -c */
        close(pfd[0]);
        dup2(pfd[1], STDOUT_FILENO);
        close(pfd[1]);
        cpu_set_t cs; CPU_ZERO(&cs); CPU_SET(CLIENT_CPU, &cs);
        if (sched_setaffinity(0, sizeof(cs), &cs) != 0)
            fprintf(stderr, "[bench] sched_setaffinity(%d) failed: %s\n",
                    CLIENT_CPU, strerror(errno));
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    close(pfd[1]);
    FILE *fp = fdopen(pfd[0], "r");
    if (!fp) { perror("fdopen"); close(pfd[0]); return r; }
```
并把末尾的 `pclose(fp);`（[test_ebpf_hset_qps.c:678](src/../../tests/perf/test_ebpf_hset_qps.c#L678)）替换为：
```c
    fclose(fp);
    waitpid(cpid, NULL, 0);
```
（`waitpid` 已在 `proxy_stop()` 使用，`sys/wait.h` 可用。）

- [ ] **Step 5: 构建**

Run: `make test_ebpf_hset_qps 2>&1 | grep -E "error|warning" | head`
Expected: 无 error（`sched.h` 已在 main 的 `sched_setaffinity` 处使用；`strerror` 需 `string.h`，已包含）。

- [ ] **Step 6: 重跑跨机冒烟（-n 50000 -r 4，none/sync/ebpf）**

同 Task 2 Step 2/3 的编排（.129 slave_receiver 每模式重起、`sudo rm -rf` BPF pin），命令不变：`sudo taskset -c 2 ./tests/perf/test_ebpf_hset_qps --mode $m --payload 64 --count 50000 --rounds 4 --slave-host 192.168.233.129 --csv /tmp/mc_$m.csv`。
记录每模式 median + slave msgs/bytes。

- [ ] **Step 7: 判定 + Commit**

对照成功标准：`sync_median/none_median` 与 `ebpf_median/none_median`，目标 ≥85%。
- 达成 → 记录结果到 bench 文档草稿，`git add tests/perf/test_ebpf_hset_qps.c && git commit -m "feat(perf): harness 多核隔离（转发/proxy/客户端分核，消除同核争抢）"`。
- 未达成 → 记录实际比值与现象，暂停分析，不进入 Task 3。

---

### Task 5: 转发线程批量 writev（消除跨机逐命令段开销）

> **为什么需要**：T4 多核 + 禁 ACK 后 sync/ebpf 仍卡 ~56k（none 94.7k）。D3（localhost vs 跨机）证明瓶颈=**跨机逐命令 write 的段开销**（TCP_NODELAY ~90B/条，每段一个报文 + 往返 ACK，~56k 段/s 封顶；慢写 → 转发队列背压 → master 被拖到 drain 速率）。本任务把 sync 转发线程的逐节点 `write_full` 改为**攒批 `writev`**（一次多条命令一个系统调用，摊薄每段开销），使跨机 sync 接近 loopback 水平（~100k）≈ none。

**Files:**
- Modify: `tests/perf/test_ebpf_hset_qps.c`（`fwd_thread_main` + 头文件）

**Interfaces:**
- Consumes: Task 1 的 `fwd_thread_main` / `fwd_enqueue` / `g_fwd_head` / `g_fwd_queue_bytes`。
- Produces: 批量转发（批大小 `FWD_BATCH_NODES` / `FWD_BATCH_BYTES`）。

- [ ] **Step 1: 新增头文件 + 批大小常量**

文件顶部 include 区（约 [test_ebpf_hset_qps.c:20-40](src/../../tests/perf/test_ebpf_hset_qps.c#L20-L40)）确认 `#include <sys/uio.h>`（`writev`/`struct iovec`）；若缺失则添加。

`MAX_FWD_QUEUE_BYTES` 定义处（约 [test_ebpf_hset_qps.c:50](src/../../tests/perf/test_ebpf_hset_qps.c#L50)）新增：
```c
#define FWD_BATCH_NODES 256   /* 转发线程每次 writev 最多命令数 */
#define FWD_BATCH_BYTES 16384 /* 转发线程每次 writev 目标字节数 */
```

- [ ] **Step 2: 重写 `fwd_thread_main` 为攒批 writev**

把 Task 1 的 `fwd_thread_main()`（约 [test_ebpf_hset_qps.c:104-122](src/../../tests/perf/test_ebpf_hset_qps.c#L104)）替换为：
```c
/* 转发线程：批量出队 → 一次 writev 多条命令（摊薄跨机每段开销）→ 释放。单消费者保序。 */
static void *fwd_thread_main(void *arg) {
    (void)arg;
    fwd_node_t *batch[FWD_BATCH_NODES];
    struct iovec iov[FWD_BATCH_NODES];
    while (1) {
        int nbatch = 0;
        size_t bbytes = 0;
        pthread_mutex_lock(&g_fwd_lock);
        while (!g_fwd_head && !g_fwd_stop)
            pthread_cond_wait(&g_fwd_cond, &g_fwd_lock);
        if (g_fwd_stop && !g_fwd_head) { pthread_mutex_unlock(&g_fwd_lock); break; }
        /* 批量出队：最多 FWD_BATCH_NODES 个节点或 FWD_BATCH_BYTES 字节 */
        while (nbatch < FWD_BATCH_NODES && g_fwd_head &&
               (bbytes < FWD_BATCH_BYTES || nbatch == 0)) {
            fwd_node_t *n = g_fwd_head;
            g_fwd_head = n->next;
            if (!g_fwd_head) g_fwd_tail = NULL;
            g_fwd_queue_bytes -= n->len;
            batch[nbatch] = n;
            iov[nbatch].iov_base = n->buf;
            iov[nbatch].iov_len = n->len;
            nbatch++;
            bbytes += n->len;
        }
        pthread_mutex_unlock(&g_fwd_lock);

        if (nbatch == 0) break;
        if (g_slave_fd >= 0) {
            ssize_t w = writev(g_slave_fd, iov, nbatch);
            if (w != (ssize_t)bbytes) {
                fprintf(stderr, "[fwd] writev partial/failed: %zd/%zu %s\n",
                        w, bbytes, strerror(errno));
                pthread_mutex_lock(&g_fwd_lock);
                g_fwd_dead = 1;
                pthread_cond_broadcast(&g_fwd_cond);
                pthread_mutex_unlock(&g_fwd_lock);
            }
        }
        for (int i = 0; i < nbatch; i++) free(batch[i]);
    }
    return NULL;
}
```
（行为变化：原逐节点 write_full 改为批量 writev；写失败判据从 `write_full != len` 变为 `writev != 批总字节`。语义等价，测试场景 slave 健康时 writev 全写。）

- [ ] **Step 3: 构建**

Run: `make test_ebpf_hset_qps 2>&1 | grep -E "error|warning" | head`
Expected: 无 error（`sys/uio.h` 已包含；`strerror` 用 `string.h`，已包含）。

- [ ] **Step 4: 重跑跨机冒烟（-n 500000 -r 4，--no-ack slave）**

编排同 Task 4（.129 `slave_receiver 15901 15902 --no-ack`、每模式重起、`sudo rm -rf` BPF pin、`sudo taskset -c 2 ./test_ebpf_hset_qps --mode $m --count 500000 --rounds 4 --slave-host 192.168.233.129`）。记录 none/sync/ebpf median + slave msgs/bytes。

- [ ] **Step 5: 判定 + Commit**

对照成功标准：`sync/none`、`ebpf/none` 目标 ≥85%。
- 达成 → `git add tests/perf/test_ebpf_hset_qps.c && git commit -m "feat(perf): harness sync 转发线程批量 writev（消除跨机逐命令段开销）"`。
- 未达成 → 记录实际比值与现象，暂停分析（ebpf 侧若仍低，单独查 proxy 的 batch 配置）。

---

### Task 6: 转发线程空队列改短超时等待（攒批 drain，消除每命令跨核唤醒）

> **为什么需要**：Task 5 批量 writev 无效（sync 仍 56.8k）。独立 writer 探针证明写路径极快（逐条 676k/s、批量 6.7M/s）。真根因=harness 转发线程用**无限期 `pthread_cond_wait`**：队列接近空时每次 enqueue 都跨核（CPU2→CPU0）signal+唤醒转发线程，每命令 ~18µs → 封顶 56k。修复=空队列等待改为**短超时 `pthread_cond_timedwait`**，让生产者攒批、转发线程批量 drain，把每命令跨核唤醒降两个量级。这正是生产 `kvs_repl.c` 转发线程的做法。

**Files:**
- Modify: `tests/perf/test_ebpf_hset_qps.c`（`fwd_thread_main` 空队列等待）

**Interfaces:**
- Consumes: Task 1/5 的 `fwd_thread_main` / `g_fwd_cond` / `g_fwd_head` / `g_fwd_stop`。
- Produces: 攒批 drain（超时 `FWD_EMPTY_WAIT_US`，默认 200µs）。

- [ ] **Step 1: 新增超时常量**

`FWD_BATCH_BYTES` 定义处（约 [test_ebpf_hset_qps.c:50](src/../../tests/perf/test_ebpf_hset_qps.c#L50)）新增：
```c
#define FWD_EMPTY_WAIT_US 200   /* 空队列等待上限（µs）：攒批 drain，避免每命令跨核唤醒 */
```

- [ ] **Step 2: `fwd_thread_main` 空队列等待改短超时**

`fwd_thread_main()` 里（约 [test_ebpf_hset_qps.c:139-141](src/../../tests/perf/test_ebpf_hset_qps.c#L139)）：
```c
        while (!g_fwd_head && !g_fwd_stop)
            pthread_cond_wait(&g_fwd_cond, &g_fwd_lock);
```
替换为：
```c
        while (!g_fwd_head && !g_fwd_stop) {
            /* 有界等待：空队列时短暂休眠让生产者攒批，避免每命令一次跨核唤醒；
             * 有信号/攒够时间则返回继续 drain。 */
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            ts.tv_nsec += (long)FWD_EMPTY_WAIT_US * 1000L;
            if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
            pthread_cond_timedwait(&g_fwd_cond, &g_fwd_lock, &ts);
        }
```

- [ ] **Step 3: 构建 + 本地冒烟**

Run: `make test_ebpf_hset_qps 2>&1 | grep -E "error|warning" | head` → 无 error。
本地冒烟（同 Task 5）：`slave_receiver --no-ack` + harness sync 127.0.0.1，slave 收满、无 `[fwd]` 错误。

- [ ] **Step 4: 跨机复测（-n 500000 -r 4，--no-ack slave）**

编排同 Task 5 Step 4（.129 slave_receiver 每模式重起、`sudo rm -rf` BPF pin、`sudo taskset -c 2`）。记录 none/sync/ebpf median + slave msgs/bytes。

- [ ] **Step 5: 判定 + Commit**

`sync/none`、`ebpf/none` 目标 ≥85%。
- 达成 → `git add tests/perf/test_ebpf_hset_qps.c && git commit -m "feat(perf): harness 转发线程空队列改短超时等待（攒批 drain，消除每命令跨核唤醒）"`。
- 未达成 → 记录实际比值与现象，暂停分析。

---

### Task 7: 转发队列改固定环形缓冲（消除每命令 malloc/free）

> **为什么需要**：discard 矩阵证明瓶颈在 master 侧每命令入队（malloc + 跨核锁 + memcpy + cond_signal ≈ 7µs）。最值优化=砍掉 **malloc/free**：把链表+malloc 节点队列换成**预分配 64MB 连续环形缓冲**（`[u32 len][payload]` 槽），handler 直接 memcpy 进环、转发线程从环读并 writev，两侧都无分配器开销。保留 `g_fwd_lock`+`g_fwd_cond` 保正确性（若锁仍为 cap，后续 Task 8 上无锁 CAS）。生产 `kvs_repl.c` 同款模式，本任务只改 harness。

**Files:**
- Modify: `tests/perf/test_ebpf_hset_qps.c`（队列 globals / `fwd_enqueue` / `fwd_thread_main` / `fwd_thread_start` / `fwd_thread_stop`）

**Interfaces:**
- Consumes: 现有 `fwd_enqueue(buf,len)` 接口（handler 调用不变）、`fwd_thread_start/stop`。
- Produces: 环形缓冲 globals + 辅助函数；`fwd_thread_main` 批量 writev（head 延后推进防覆写）。

- [ ] **Step 1: 替换队列 globals（约 [test_ebpf_hset_qps.c:254-268](src/../../tests/perf/test_ebpf_hset_qps.c#L254)）**

把 `fwd_node_t` 结构、`g_fwd_head/g_fwd_tail/g_fwd_queue_bytes`、`MAX_FWD_QUEUE_BYTES`、`FWD_BATCH_NODES` 整块替换为：
```c
/* ---- sync 转发环形缓冲（Task 7：消除每命令 malloc/free，保留锁+condvar） ---- */
#define FWD_RING_SIZE (64 * 1024 * 1024)      /* 64MB 连续环形缓冲 */
#define FWD_BATCH_BYTES 16384                 /* 转发线程每次 writev 目标字节数 */
#define FWD_EMPTY_WAIT_US 200                 /* 空队列等待上限（µs）：攒批 drain */
#define FWD_MAX_IOV 512                       /* writev iovec 上限（每槽最多 2 段：payload wrap） */

static unsigned char *g_fwd_ring = NULL;      /* 预分配，fwd_thread_start 分配 */
static size_t g_fwd_ring_head = 0;            /* 转发线程读位置 */
static size_t g_fwd_tail = 0;                 /* handler 写位置（持锁） */
static pthread_mutex_t g_fwd_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_fwd_cond = PTHREAD_COND_INITIALIZER;
static int g_fwd_stop = 0;
static int g_fwd_dead = 0;                    /* slave 写失败后置 1，后续入队丢弃 */
static pthread_t g_fwd_thread;

/* 已占用字节 (tail - head) mod size */
static size_t fwd_occupied(void) {
    size_t h = g_fwd_ring_head, t = g_fwd_tail;
    return t >= h ? t - h : FWD_RING_SIZE - (h - t);
}
static size_t fwd_space(void) { return FWD_RING_SIZE - fwd_occupied(); }
/* pos 处已提交的连续字节数（不跨环尾） */
static size_t fwd_committed_at(size_t pos) {
    size_t to_tail = pos <= g_fwd_tail ? g_fwd_tail - pos : FWD_RING_SIZE - (pos - g_fwd_tail);
    size_t to_end = FWD_RING_SIZE - pos;
    return to_tail < to_end ? to_tail : to_end;
}
```
（确认文件顶部已 `#include <stdint.h>` 供 `uint32_t` 使用；harness 已用 `uint64_t`，应已包含。）

- [ ] **Step 2: 替换 `fwd_enqueue`（约 [test_ebpf_hset_qps.c:432-447](src/../../tests/perf/test_ebpf_hset_qps.c#L432)）**

```c
static int fwd_enqueue(const unsigned char *buf, size_t len) {
    size_t need = len + sizeof(uint32_t);
    pthread_mutex_lock(&g_fwd_lock);
    while (fwd_space() < need && !g_fwd_stop && !g_fwd_dead)
        pthread_cond_wait(&g_fwd_cond, &g_fwd_lock);
    if (g_fwd_stop || g_fwd_dead) { pthread_mutex_unlock(&g_fwd_lock); return -1; }
    /* 写 4B 长度头（可能跨环尾） */
    uint32_t l = (uint32_t)len;
    size_t hf = FWD_RING_SIZE - g_fwd_tail;
    if (sizeof(l) <= hf) memcpy(g_fwd_ring + g_fwd_tail, &l, sizeof(l));
    else {
        memcpy(g_fwd_ring + g_fwd_tail, &l, hf);
        memcpy(g_fwd_ring, (const char *)&l + hf, sizeof(l) - hf);
    }
    g_fwd_tail = (g_fwd_tail + sizeof(l)) % FWD_RING_SIZE;
    /* 写 payload（可能跨环尾） */
    size_t pf = FWD_RING_SIZE - g_fwd_tail;
    if (len <= pf) memcpy(g_fwd_ring + g_fwd_tail, buf, len);
    else {
        memcpy(g_fwd_ring + g_fwd_tail, buf, pf);
        memcpy(g_fwd_ring, buf + pf, len - pf);
    }
    g_fwd_tail = (g_fwd_tail + len) % FWD_RING_SIZE;
    pthread_cond_signal(&g_fwd_cond);
    pthread_mutex_unlock(&g_fwd_lock);
    return 0;
}
```

- [ ] **Step 3: 替换 `fwd_thread_main`（约 [test_ebpf_hset_qps.c:452-500](src/../../tests/perf/test_ebpf_hset_qps.c#L452)）**

```c
static void *fwd_thread_main(void *arg) {
    (void)arg;
    static int fwd_discard = -1;      /* 诊断：FWD_DISCARD=1 跳过 writev */
    static size_t max_qbytes = 0;
    if (fwd_discard < 0)
        fwd_discard = getenv("FWD_DISCARD") != NULL;
    struct iovec iov[FWD_MAX_IOV];
    while (1) {
        int niov = 0;
        size_t batch_bytes = 0;
        size_t new_head;
        pthread_mutex_lock(&g_fwd_lock);
        while (fwd_occupied() == 0 && !g_fwd_stop) {
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            ts.tv_nsec += (long)FWD_EMPTY_WAIT_US * 1000L;
            if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
            pthread_cond_timedwait(&g_fwd_cond, &g_fwd_lock, &ts);
        }
        if (g_fwd_stop && fwd_occupied() == 0) { pthread_mutex_unlock(&g_fwd_lock); break; }
        /* 锁内扫描长度前缀槽 → 攒 iovec；head 延后推进（防 producer 覆写待写区） */
        size_t pos = g_fwd_ring_head;
        while (fwd_committed_at(pos) >= sizeof(uint32_t) && batch_bytes < FWD_BATCH_BYTES) {
            uint32_t l;
            size_t hf = fwd_committed_at(pos);
            if (hf >= sizeof(l)) memcpy(&l, g_fwd_ring + pos, sizeof(l));
            else {
                memcpy(&l, g_fwd_ring + pos, hf);
                memcpy((char *)&l + hf, g_fwd_ring, sizeof(l) - hf);
            }
            pos = (pos + sizeof(l)) % FWD_RING_SIZE;
            if (fwd_committed_at(pos) < l) break;   /* payload 未完整（锁内防御，正常不发生） */
            size_t pf = fwd_committed_at(pos);
            if (l <= pf) {
                iov[niov].iov_base = g_fwd_ring + pos; iov[niov].iov_len = l; niov++;
            } else {
                iov[niov].iov_base = g_fwd_ring + pos; iov[niov].iov_len = pf; niov++;
                iov[niov].iov_base = g_fwd_ring;      iov[niov].iov_len = l - pf; niov++;
            }
            pos = (pos + l) % FWD_RING_SIZE;
            batch_bytes += sizeof(l) + l;
            if (niov >= FWD_MAX_IOV - 1) break;
        }
        new_head = pos;
        if (fwd_occupied() > max_qbytes) max_qbytes = fwd_occupied();
        pthread_mutex_unlock(&g_fwd_lock);

        if (batch_bytes > 0 && !fwd_discard && g_slave_fd >= 0) {
            ssize_t w = writev(g_slave_fd, iov, niov);
            if (w != (ssize_t)batch_bytes) {
                fprintf(stderr, "[fwd] writev partial/failed: %zd/%zu %s\n",
                        w, batch_bytes, strerror(errno));
                pthread_mutex_lock(&g_fwd_lock);
                g_fwd_dead = 1;
                pthread_cond_broadcast(&g_fwd_cond);
                pthread_mutex_unlock(&g_fwd_lock);
            }
        }
        /* writev 完成后推进 head，释放空间并唤醒生产者 */
        pthread_mutex_lock(&g_fwd_lock);
        g_fwd_ring_head = new_head;
        if (batch_bytes > 0) pthread_cond_broadcast(&g_fwd_cond);
        pthread_mutex_unlock(&g_fwd_lock);
    }
    fprintf(stderr, "[fwd] exit: max_queue_bytes=%zu (%s)\n",
            max_qbytes, fwd_discard ? "discard-mode" : "write-mode");
    return NULL;
}
```
> **关键正确性**：head 在锁内扫描时**不推进**，writev 完成后再加锁推进——否则 producer 在 writev 期间会覆写已出队的待写区。writev 期间 `[旧head, new_head)` 仍视为已占用，producer 只写 `[tail, head)` 空闲区，无重叠。

- [ ] **Step 4: 替换 `fwd_thread_start` / `fwd_thread_stop`（约 [test_ebpf_hset_qps.c:509-536](src/../../tests/perf/test_ebpf_hset_qps.c#L509)）**

```c
static void fwd_thread_start(void) {
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_stop = 0;
    g_fwd_dead = 0;
    if (!g_fwd_ring) g_fwd_ring = (unsigned char *)malloc(FWD_RING_SIZE);
    pthread_mutex_unlock(&g_fwd_lock);
    pthread_create(&g_fwd_thread, NULL, fwd_thread_main, NULL);
    /* 转发线程 pin 到独立核 FWD_THREAD_CPU，不与 handler 抢 master 核（失败仅告警） */
    cpu_set_t cs; CPU_ZERO(&cs); CPU_SET(FWD_THREAD_CPU, &cs);
    if (pthread_setaffinity_np(g_fwd_thread, sizeof(cs), &cs) != 0)
        fprintf(stderr, "[fwd] pthread_setaffinity_np(%d) failed: %s\n",
                FWD_THREAD_CPU, strerror(errno));
}

static void fwd_thread_stop(void) {
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_stop = 1;
    pthread_cond_broadcast(&g_fwd_cond);
    pthread_mutex_unlock(&g_fwd_lock);
    pthread_join(g_fwd_thread, NULL);
    pthread_mutex_lock(&g_fwd_lock);
    g_fwd_ring_head = 0;
    g_fwd_tail = 0;
    free(g_fwd_ring);
    g_fwd_ring = NULL;
    pthread_mutex_unlock(&g_fwd_lock);
}
```

- [ ] **Step 5: 构建 + 本地冒烟**

Run: `make test_ebpf_hset_qps 2>&1 | grep -E "error|warning" | head` → 无 error。
本地冒烟（同 Task 5/6）：`slave_receiver --no-ack` + harness sync 127.0.0.1，slave 收满、无 `[fwd]` 错误、无崩溃。**验证 FWD_DISCARD=1 诊断开关仍可用**（`FWD_DISCARD=1` 跑 sync 127.0.0.1，确认输出 `[fwd] exit ... discard-mode`）。

- [ ] **Step 6: 跨机复测（-n 500000 -r 4，--no-ack slave）**

编排同 Task 5/6（.129 slave_receiver 每模式重起、`sudo rm -rf` BPF pin、`sudo taskset -c 2`）。记录 none/sync/ebpf median + slave msgs/bytes + `[fwd] exit max_queue_bytes`。

- [ ] **Step 7: 判定 + Commit**

`sync/none`、`ebpf/none` 目标 ≥85%。
- 达成 → `git add tests/perf/test_ebpf_hset_qps.c && git commit -m "feat(perf): harness 转发队列改固定环形缓冲（消除每命令 malloc/free）"`。
- 未达成 → 记录实际比值与现象，暂停分析（若环形缓冲砍掉 malloc 后锁仍是 cap → Task 8 无锁 CAS；ebpf 侧若仍低单独查 proxy）。

---

### Task 8: 生产 proxy 转发批量 writev + 去每命令 cond_signal（ebpf 路径）

> **为什么需要**：ebpf 仍 58%（55.5k vs none 95k）。生产 `src/ebpf_proxy/main.c` 的转发路径与 harness 修复前同款：入队 `malloc`+`memcpy`+每命令 `pthread_cond_signal`（[main.c:84-96](src/../../src/ebpf_proxy/main.c#L84)），转发线程逐节点 `writev(iov,1)`（[main.c:99-132](src/../../src/ebpf_proxy/main.c#L99)）。harness 已验证"去 signal + 批量"有效（sync 56k→80k）。本任务把 proxy 转发线程改为**批量 writev（`PFWD_BATCH_MAX`）+ 空队列改 `PFWD_LINGER_US` timed-wait（去每命令 signal）+ 出队后 broadcast 唤醒背压入队者**。批量写持 `g_state_lock` 校验 FORWARDING 后一次 writev（沿用 IMPORTANT-2 的原子性），单条命令靠 linger 兜底（≤200µs 即下送，不丢不滞）。

**Files:**
- Modify: `src/ebpf_proxy/main.c`（常量 / `proxy_fwd_enqueue` / `proxy_fwd_send_one`→`proxy_fwd_send_batch` / `proxy_fwd_thread_main`）

**Interfaces:**
- Consumes: `proxy_slave_writev(ctx, iov, iovcnt)`（已支持 iovcnt>1）、`cache_enq_wrap(buf,len)`、`g_state`/`g_state_lock`。
- Produces: `proxy_fwd_send_batch(batch[], nbatch)`；批量转发线程（linger 有界延迟）。

- [ ] **Step 1: 新增常量 + 确认头文件**

`MAX_PFWD_QUEUE_BYTES` / `PFWD_LARGE_SZ` 定义处（约 [main.c:76-81](src/../../src/ebpf_proxy/main.c#L76)）新增：
```c
#define PFWD_BATCH_MAX 256        /* 转发线程每次 writev 最多节点数 */
#define PFWD_LINGER_US 200        /* 空队列等待上限（µs）：单条命令有界延迟，免每命令 futex_wake */
```
确认 `#include <sys/uio.h>`（`struct iovec`）与 `#include <time.h>`（`clock_gettime`）已存在（`proxy_fwd_send_one` 已用 iovec，应已包含；`time.h` 若缺失则补）。

- [ ] **Step 2: `proxy_fwd_enqueue` 去掉每命令 `pthread_cond_signal`**

[main.c:84-96](src/../../src/ebpf_proxy/main.c#L84) 末尾的 `pthread_cond_signal(&g_pfwd_cond);` 删除（consumer 靠 `PFWD_LINGER_US` timed-wait 自醒），并加注释：
```c
    g_pfwd_tail = n; g_pfwd_bytes += len;
    /* 无 cond_signal：转发线程靠 PFWD_LINGER_US timed-wait 自醒（免每命令 futex_wake，
     * 单条命令有界延迟 ≤PFWD_LINGER_US）。 */
    pthread_mutex_unlock(&g_pfwd_lock);
```

- [ ] **Step 3: `proxy_fwd_send_one` 替换为 `proxy_fwd_send_batch`**

[main.c:99-115](src/../../src/ebpf_proxy/main.c#L99) 的 `proxy_fwd_send_one` 整块替换为：
```c
/* 转发线程处理一批节点：持 g_state_lock 校验 FORWARDING 后批量 writev（原子于 BUFFERING flip）。
 * 批内节点要么整体在 FORWARDING 下送、要么整体进 cache，不穿插全量流。
 * 注意：writev 部分写（w>=0 且 <total）按既有语义视为已送（slave 健康+SO_SNDTIMEO 下全写），
 * 与原逐节点版本一致。 */
static void proxy_fwd_send_batch(proxy_fwd_node_t **batch, int nbatch) {
    struct iovec iov[PFWD_BATCH_MAX];
    for (int i = 0; i < nbatch; i++) {
        iov[i].iov_base = batch[i]->buf;
        iov[i].iov_len = batch[i]->len;
    }
    pthread_mutex_lock(&g_state_lock);
    if (g_state == STATE_FORWARDING) {
        ssize_t w = proxy_slave_writev(&g_slave, iov, nbatch);
        pthread_mutex_unlock(&g_state_lock);
        if (w < 0) {
            for (int i = 0; i < nbatch; i++) cache_enq_wrap(batch[i]->buf, batch[i]->len);
        }
    } else {
        pthread_mutex_unlock(&g_state_lock);
        for (int i = 0; i < nbatch; i++) cache_enq_wrap(batch[i]->buf, batch[i]->len);
    }
}
```

- [ ] **Step 4: `proxy_fwd_thread_main` 批量出队 + linger timed-wait**

[main.c:119-132](src/../../src/ebpf_proxy/main.c#L119) 的 `proxy_fwd_thread_main` 整块替换为：
```c
static void *proxy_fwd_thread_main(void *arg) {
    (void)arg;
    proxy_fwd_node_t *batch[PFWD_BATCH_MAX];
    while (!g_pfwd_stop) {
        int nbatch = 0;
        pthread_mutex_lock(&g_pfwd_lock);
        while (!g_pfwd_head && !g_pfwd_stop) {
            /* 有界等待：空队列时短暂休眠让 ringbuf 回调攒批（linger），
             * 免每命令 futex_wake；单条命令最多等 PFWD_LINGER_US 即下送。 */
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            ts.tv_nsec += (long)PFWD_LINGER_US * 1000L;
            if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
            pthread_cond_timedwait(&g_pfwd_cond, &g_pfwd_lock, &ts);
        }
        while (nbatch < PFWD_BATCH_MAX && g_pfwd_head) {
            proxy_fwd_node_t *n = g_pfwd_head;
            g_pfwd_head = n->next;
            if (!g_pfwd_head) g_pfwd_tail = NULL;
            g_pfwd_bytes -= n->len;
            batch[nbatch++] = n;
        }
        /* 出队后唤醒被 64MB 满队列背压阻塞的入队者（原代码缺此唤醒，属修复） */
        if (nbatch > 0) pthread_cond_broadcast(&g_pfwd_cond);
        pthread_mutex_unlock(&g_pfwd_lock);

        if (nbatch == 0) break;   /* stop 且队列空 */
        proxy_fwd_send_batch(batch, nbatch);
        for (int i = 0; i < nbatch; i++) free(batch[i]);
    }
    return NULL;
}
```

- [ ] **Step 5: 构建**

Run: `make ebpf_proxy 2>&1 | grep -E "error|warning" | head`
Expected: 无 error（`proxy_fwd_send_one` 已无引用；`clock_gettime`/`time.h` 就绪；`cache_enq_wrap` 签名不变）。

- [ ] **Step 6: 跨机 ebpf 复测（-n 500000 -r 4，--no-ack slave）**

编排同 Task 7 Step 6（.129 slave_receiver 重起、`sudo rm -rf` BPF pin、`sudo taskset -c 2 ./test_ebpf_hset_qps --mode ebpf`）。记录 ebpf median + slave msgs/bytes。同时跑 none/sync 作参照。

- [ ] **Step 7: 判定 + Commit**

`ebpf/none` 目标 ≥85%（或相对 58.1% 有显著提升，如实记录）。
- 达成/提升 → `git add src/ebpf_proxy/main.c && git commit -m "feat(ebpf-proxy): 转发线程批量 writev + 去每命令 cond_signal（linger 有界延迟）"`。
- 未达 → 记录实际比值与现象，暂停分析（若 malloc 仍 cap → 下一步 proxy 环形缓冲；若 BPF hook 固有 → 如实记录）。

---

### Task 9: capture BPF 改 fexit-only（删冗余 fentry，hook 触发减半）

> **为什么需要**：验证完成——ebpf ~28% 差距 = BPF hook 在 master 核 CPU2（none 71% → ebpf 95%，proxy 23% 空闲）。hook 每次 recv 触发 fentry+fexit 两次，且 fentry 只用来存 `count_before`。**已实测（kernel 6.1.176）`ctx[5]` = `tcp_recvmsg` 返回值**（sshd rv=48 / redis-benchmark rv=4 均真实字节数），fentry 冗余。删掉 fentry + per-thread map，fexit 直接用 `ctx[5]`，hook 触发减半、去掉 map 查/改/删 → CPU2 预计回收 ~12pp → ebpf 逼近 85%。

**Files:**
- Modify: `src/replication/bpf/repl_client_capture.bpf.c`
- Test: 跨机 ebpf 复测（harness `--obj-path` 运行时加载重建的 .o，无需重编 proxy）

**Interfaces:**
- Consumes: `client_ctl`（pid 过滤）、`client_tmpbuf`、`client_cache_ringbuf`、`iov_head` 结构。
- Produces: fexit-only `fexit_tcp_recvmsg`（`ctx[5]` 取返回值）；删 `fentry_tcp_recvmsg` / `client_fexit_ctx` map / `fexit_ctx` 结构。

- [ ] **Step 1: 删除 fentry + 相关声明，更新注释/统计键**

- 删除文件头注释里的"kernel 6.1 的 fexit 不提供返回值"段，改为说明 fexit-only 方案（`ctx[5]`=返回值，已实测验证）。
- 删除 `struct fexit_ctx` 定义、`client_fexit_ctx` map 声明、`fentry_tcp_recvmsg` 整个函数。
- 把 ST_* 统计键重定义为（无 fentry 专属键）：
```c
#define ST_HIT        0   /* fexit 命中 pid 过滤 */
#define ST_HEAD_FAIL  1   /* 读 iov_head (msg+32) 失败 */
#define ST_RETVAL_LE0 2   /* ctx[5] 返回值 <= 0 */
#define ST_IOVEC      3   /* 走 IOVEC 分支 */
#define ST_UBUF       4   /* 走 UBUF 分支 */
#define ST_USER_FAIL  5   /* bpf_probe_read_user 读数据失败 */
#define ST_RB_OK      6   /* ringbuf_output 成功 */
```
（`client_stats` map `max_entries` 可保持 8 不变。）

- [ ] **Step 2: 重写 `fexit_tcp_recvmsg`（用 `ctx[5]`，去掉 map 依赖）**

把整个 fexit 函数替换为：
```c
SEC("fexit/tcp_recvmsg")
int fexit_tcp_recvmsg(__u64 *ctx)
{
    __u64 *ctl_pid = bpf_map_lookup_elem(&client_ctl, &(__u32){1});
    if (!ctl_pid || !*ctl_pid)
        return 0;

    __u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (pid != (__u32)(*ctl_pid))
        return 0;

    cstat_inc(ST_HIT);

    /* tcp_recvmsg 5 参数（sk,msg,len,flags,addr_len），BPF trampoline 把返回值放 ctx[5]。
     * 已实测（kernel 6.1.176）：ctx[5] = 实际读字节数。不再需要 fentry 保存 count_before。 */
    long long retval = (long long)ctx[5];
    if (retval <= 0) {
        cstat_inc(ST_RETVAL_LE0);
        return 0;
    }
    if (retval > CLIENT_ENTRY_MAX_LEN)
        retval = CLIENT_ENTRY_MAX_LEN;

    unsigned long msg_ptr = (unsigned long)ctx[1];
    if (!msg_ptr)
        return 0;

    struct iov_head head;
    if (bpf_probe_read_kernel(&head, sizeof(head),
            (const void *)(msg_ptr + 32)) != 0) {
        cstat_inc(ST_HEAD_FAIL);
        return 0;
    }

    __u32 tmp_key = 0;
    unsigned char(*entry)[CLIENT_ENTRY_HDR_SZ + CLIENT_ENTRY_MAX_LEN];
    entry = bpf_map_lookup_elem(&client_tmpbuf, &tmp_key);
    if (!entry) return 0;

    int data_len;
    unsigned long user_ptr;
    if (head._nr > 0) {
        cstat_inc(ST_IOVEC);
        if (!head.ptr) return 0;
        struct { unsigned long b; unsigned long l; } vec;
        if (bpf_probe_read_kernel(&vec, sizeof(vec), (const void *)head.ptr) != 0)
            return 0;
        if (!vec.b || vec.l == 0) return 0;
        unsigned long long safe_len = vec.l;
        if (safe_len > (unsigned long long)retval)
            safe_len = (unsigned long long)retval;
        if (safe_len > CLIENT_ENTRY_MAX_LEN)
            safe_len = CLIENT_ENTRY_MAX_LEN;
        if (safe_len == 0) return 0;
        data_len = (int)safe_len;
        user_ptr = (unsigned long)vec.b;
    } else {
        cstat_inc(ST_UBUF);
        if (!head.ptr || head._count == 0) return 0;
        /* ITER_UBUF: head.ptr = ubuf 基址（不随拷贝推进），数据在 [ubuf, ubuf+retval)。 */
        data_len = (int)retval;
        user_ptr = head.ptr;
    }
    if (data_len <= 0 || user_ptr == 0) return 0;

    __u32 payload_len = (__u32)data_len;
    __builtin_memcpy(*entry, &payload_len, 4);

    if (bpf_probe_read_user((*entry) + 4, (__u32)data_len,
            (const void *)user_ptr) != 0) {
        cstat_inc(ST_USER_FAIL);
        return 0;
    }

    bpf_ringbuf_output(&client_cache_ringbuf, *entry,
                        CLIENT_ENTRY_HDR_SZ + data_len, 0);
    cstat_inc(ST_RB_OK);
    return 0;
}
```
> 关键正确性：`ctx[1]`=msghdr（参数第 2 个）、`ctx[5]`=返回值（5 参数后），均已实测。数据拷贝逻辑（IOVEC/UBUF 分支、tmpbuf、probe_read_user、ringbuf）与原版一致。去掉 `out:` 标签与 map delete（无 map）。

- [ ] **Step 3: 重建 capture .o**

Run: `make client_capture_bpf 2>&1 | grep -E "error" | head`
Expected: 编译无 error（`build/replication/bpf/repl_client_capture.bpf.o` 更新）。

- [ ] **Step 4: 跨机 ebpf 复测（-n 500000 -r 4，--no-ack slave）**

编排同 Task 8（.129 slave_receiver 重起、`sudo rm -rf` BPF pin、`sudo taskset -c 2 ./test_ebpf_hset_qps --mode ebpf`）。记录 ebpf median + slave msgs/bytes（数据完整性：bytes ≈ 248000000）。同时跑 none 作参照。

- [ ] **Step 5: 判定 + Commit**

`ebpf/none` 目标 ≥85%。
- 达成/提升 → `git add src/replication/bpf/repl_client_capture.bpf.c && git commit -m "feat(ebpf): capture BPF 改 fexit-only（ctx[5] 取返回值，删冗余 fentry，hook 触发减半）"`。
- 未达 → 记录实际比值与现象，如实归因。

---

### Task 12: harness 改单线程 epoll reactor（对齐生产，A1）

> **为什么需要**：harness 原为 50 线程 per-conn（简单基准服务器），生产 kvstore 是单线程 epoll reactor——线程模型不一致使转发方法测量失真（内联锁争抢、调度开销）。本任务把 harness 的 `accept_thread`+per-conn `client_handler` 重写为**单线程 epoll reactor**（一个线程非阻塞处理所有连接，对齐生产），重新测 none/sync-线程/sync-内联/ebpf，得生产相关的转发方法结论。

**Files:**
- Modify: `tests/perf/test_ebpf_hset_qps.c`（新增 reactor；替换 `accept_thread` / `client_handler` / `master_start` / `master_stop`）

**Interfaces:**
- Consumes: 现有 `conn_buf_t` / `resp_scanner_t` / `ht_hset` / `fwd_enqueue` / `fwd_inline_forward` / `fwd_thread_start` / `RESP_OK`。
- Produces: `reactor_main`（单线程 epoll 循环）、`reactor_conn_t`（per-conn 读/回包缓冲）、`reactor_conn_read` / `reactor_conn_flush`。

- [ ] **Step 1: 头文件 + per-conn 结构 + 工具**

确认 `#include <sys/epoll.h>` 与 `#include <fcntl.h>`（`fcntl` 设 `O_NONBLOCK`）存在；`conn_buf_t` 定义在文件顶部（约 [test_ebpf_hset_qps.c:376](src/../../tests/perf/test_ebpf_hset_qps.c#L376)）。新增：

```c
/* ---- 单线程 epoll reactor（A1：对齐生产单线程 reactor）---- */
typedef struct reactor_conn_s {
    int fd;
    conn_buf_t in;                 /* 读缓冲 */
    char out[CONN_BUF_SZ];         /* 回包缓冲（非阻塞写） */
    size_t out_len, out_off;       /* 待发 / 已发偏移 */
    int epollout_reg;              /* 已注册 EPOLLOUT */
} reactor_conn_t;

static void reactor_conn_close(reactor_conn_t *c, int epfd) {
    epoll_ctl(epfd, EPOLL_CTL_DEL, c->fd, NULL);
    close(c->fd);
    free(c);
}

/* 追加回包并尝试非阻塞 flush；EAGAIN → 注册 EPOLLOUT；发完 → 注销 EPOLLOUT */
static void reactor_conn_flush(reactor_conn_t *c, int epfd) {
    while (c->out_off < c->out_len) {
        ssize_t w = write(c->fd, c->out + c->out_off, c->out_len - c->out_off);
        if (w < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                if (!c->epollout_reg) {
                    struct epoll_event ev = {.events = EPOLLOUT, .data.u64 = (uint64_t)(uintptr_t)c};
                    epoll_ctl(epfd, EPOLL_CTL_MOD, c->fd, &ev);
                    c->epollout_reg = 1;
                }
                return;
            }
            reactor_conn_close(c, epfd); return;
        }
        c->out_off += (size_t)w;
    }
    if (c->out_len > 0) c->out_len = c->out_off = 0;
    if (c->epollout_reg) {
        struct epoll_event ev = {.events = EPOLLIN | EPOLLRDHUP, .data.u64 = (uint64_t)(uintptr_t)c};
        epoll_ctl(epfd, EPOLL_CTL_MOD, c->fd, &ev);
        c->epollout_reg = 0;
    }
}
```

- [ ] **Step 2: reactor_conn_read（解析 + ht_hset + 转发 + 缓冲回包）**

把 `client_handler` 的解析/处理逻辑搬到这里（回包改为缓冲到 `c->out`，非阻塞 flush）：
```c
static void reactor_conn_read(reactor_conn_t *c, int epfd) {
    char io[65536];
    ssize_t n = read(c->fd, io, sizeof(io));
    if (n <= 0) { reactor_conn_close(c, epfd); return; }
    if (cb_space(&c->in) < (int)n) cb_compact(&c->in);
    if (cb_space(&c->in) < (int)n) { reactor_conn_close(c, epfd); return; }
    memcpy(c->in.data + c->in.head, io, (size_t)n);
    c->in.head += (int)n;

    resp_scanner_t rs;
    rs_init(&rs);
    while (1) {
        int consumed = 0;
        int avail = cb_avail(&c->in);
        int rc = rs_scan(&rs, c->in.data + c->in.tail, avail, &consumed);
        if (rc <= 0) break;
        if (rs.arg_count >= 4 && rs.arg_lens[0] == 4 && memcmp(rs.args[0], "HSET", 4) == 0) {
            (void)ht_hset(rs.args[1], rs.arg_lens[1], rs.args[2], rs.arg_lens[2]);
            /* 转发：默认入队(线程版) / FWD_INLINE 内联 / FWD_NOENQ 跳过 */
            if (g_mode == 1 && g_slave_fd >= 0 && getenv("FWD_NOENQ") == NULL) {
                if (g_fwd_inline) fwd_inline_forward(c->in.data + c->in.tail, (size_t)consumed);
                else (void)fwd_enqueue(c->in.data + c->in.tail, (size_t)consumed);
            }
        }
        if (c->out_len + RESP_OK_LEN > sizeof(c->out)) { reactor_conn_close(c, epfd); return; }
        memcpy(c->out + c->out_len, RESP_OK, RESP_OK_LEN);
        c->out_len += RESP_OK_LEN;
        cb_consume(&c->in, consumed);
        rs_init(&rs);
    }
    reactor_conn_flush(c, epfd);
}
```

- [ ] **Step 3: reactor_main（单线程 epoll 循环）+ 替换 master_start/master_stop**

新增 `reactor_main`；删除 `accept_thread` 与 `client_handler`；`master_start` 改为 `pthread_create(&tid, NULL, reactor_main, NULL)`；`master_stop` 关闭 epfd/监听 fd：
```c
static void *reactor_main(void *arg) {
    (void)arg;
    int epfd = epoll_create1(0);
    int lfl = fcntl(g_listen_fd, F_GETFL, 0);
    fcntl(g_listen_fd, F_SETFL, lfl | O_NONBLOCK);
    struct epoll_event ev = {.events = EPOLLIN, .data.u64 = 0};   /* 0 = listen */
    epoll_ctl(epfd, EPOLL_CTL_ADD, g_listen_fd, &ev);

    while (!g_shutdown) {
        struct epoll_event evs[256];
        int n = epoll_wait(epfd, evs, 256, 100);
        for (int i = 0; i < n; i++) {
            reactor_conn_t *c = (reactor_conn_t *)(uintptr_t)evs[i].data.u64;
            if (c == NULL) {
                /* listen：accept 所有待接连接 */
                while (1) {
                    int cfd = accept(g_listen_fd, NULL, NULL);
                    if (cfd < 0) break;
                    set_nodelay(cfd);
                    int fl = fcntl(cfd, F_GETFL, 0);
                    fcntl(cfd, F_SETFL, fl | O_NONBLOCK);
                    reactor_conn_t *nc = calloc(1, sizeof(*nc));
                    nc->fd = cfd;
                    struct epoll_event cev = {.events = EPOLLIN | EPOLLRDHUP,
                                              .data.u64 = (uint64_t)(uintptr_t)nc};
                    epoll_ctl(epfd, EPOLL_CTL_ADD, cfd, &cev);
                }
            } else if (evs[i].events & (EPOLLIN | EPOLLRDHUP)) {
                reactor_conn_read(c, epfd);
            } else if (evs[i].events & EPOLLOUT) {
                reactor_conn_flush(c, epfd);
            } else {   /* EPOLLERR | EPOLLHUP */
                reactor_conn_close(c, epfd);
            }
        }
    }
    close(epfd);
    close(g_listen_fd);
    g_listen_fd = -1;
    return NULL;
}
```
（`master_start` 里 `pthread_create(&tid, NULL, reactor_main, NULL)`；`master_stop` 里 `g_shutdown=1` 后 join reactor 线程即可。）

- [ ] **Step 4: 构建**

Run: `make test_ebpf_hset_qps 2>&1 | grep -E "error|warning" | head` → 无 error（`client_handler`/`accept_thread` 已删无引用；`uintptr_t` 需 `<stdint.h>`，已含）。

- [ ] **Step 5: 本机冒烟（-c 50 -P 1，none/sync-线程）**

本机 slave_receiver + harness `--mode none`/`--mode sync --slave-host 127.0.0.1`，确认：QPS 正常、slave 收到数据（线程版 `[fwd] exit`）、无崩溃/死锁。再跑 `FWD_INLINE=1 sync` 确认内联路径（无转发线程、slave 收到）。

- [ ] **Step 6: 跨机复测（-c 50，P=1 与 P=16，none/sync-线程/sync-内联/ebpf）**

编排同前（.129 slave_receiver 每模式重起、`sudo rm -rf` BPF pin、`sudo taskset -c 2`）。记录各模式 QPS + 比值 + slave bytes。

- [ ] **Step 7: 判定 + Commit**

对比单线程 reactor 下 sync-线程 vs sync-内联 vs none：**内联是否仍差？线程版是否接近 none？** 如实记录。
- 达成/有结论 → `git add tests/perf/test_ebpf_hset_qps.c && git commit -m "feat(perf): harness 改单线程 epoll reactor（对齐生产，重测转发方法）"`。
- 若内联在单线程下明显优于线程版 → 记录为生产设计建议（内联非阻塞可考虑）。

---

- Create: `docs/superpowers/bench/2026-08-10-harness-fwd-thread-qps.md`

**Interfaces:**
- Consumes: Task 2 冒烟通过的编排。

- [ ] **Step 1: 正式三模式（-n 1000000 -r 6，5 采样中位数）**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
for m in none sync ebpf; do
  rssh "pkill -9 -x slave_receiver 2>/dev/null; sleep 0.2; nohup /home/pp/slave_receiver 15901 > /tmp/slave.txt 2>&1 & sleep 0.8"
  sudo rm -rf /sys/fs/bpf/kvstore_hset_qps_test 2>/dev/null
  echo "===== $m ====="
  sudo taskset -c 2 ./tests/perf/test_ebpf_hset_qps --mode $m --payload 64 \
    --count 1000000 --rounds 6 --slave-host 192.168.233.129 --csv /tmp/harness_$m.csv 2>&1 | tail -4
  sleep 1
  echo "slave: $(rssh 'cat /tmp/slave.txt 2>/dev/null | grep msgs')"
done
```
Expected: 每模式 5 采样（round 0 预热），取 median（CSV median 列）。

- [ ] **Step 2: 写 bench 文档定稿**

`docs/superpowers/bench/2026-08-10-harness-fwd-thread-qps.md`，含：
- 方法与拓扑（.128 harness `--cpu 2` sudo + client 本机 7.2.9 + .129 slave_receiver；`-n 1000000 -c 50 -P 1 -d 64 -r 1000000`；5 采样中位数）。
- 结果表：none / sync / ebpf median，vs none 百分比，与 README 旧值（63,044 / 30,838 / 28,092）对照。
- 数据完整性：sync/ebpf 的 slave msgs/bytes。
- 结论：对照成功标准（sync、ebpf ≥85% none？），如实记录，未达则归因（BPF hook 固有 / slave 消费 / CPU2 核竞争）。

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/bench/2026-08-10-harness-fwd-thread-qps.md
git commit -m "bench: harness sync 独立转发线程后 none/sync/ebpf 跨机 QPS 复测"
```

---

## 自检记录

- **Spec 覆盖**：spec §3 设计（队列/线程/去锁/启停）→ T1；§4 测量方法（构建前置、-n 50000 冒烟、-n 1000000 正式、.129 slave_receiver、sudo）→ T2/T3；§5 成功标准 → T2 Step 4 判定 + T3 结论。全覆盖。
- **占位符**：无；所有代码块与命令为最终内容。`--obj-path`/`--pin-path` 由 harness `proxy_start()` 内部处理（默认 `build/replication/bpf/repl_client_capture.bpf.o`，已确认存在），T2/T3 无需手动传参。
- **类型一致性**：`fwd_enqueue(buf,len)`、`fwd_thread_start/stop`、`g_fwd_head/tail`、`g_fwd_dead` 在 T1 各步骤与 spec §3 一致。
