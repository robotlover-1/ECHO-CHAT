# AOF 独立线程 + 先回复实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 AOF 异步路径移到独立线程（write-in-place 零拷贝），并把语义从「落盘才回包」改为「先回复 + 有界窗口」，P=1 QPS 从 ~45k 提到 ~110k。

**Architecture:** 主线程只做 append（写进 `g_cur_slot`）+ 立即回包；AOF 线程独占 io_uring（SINGLE_ISSUER）按磁盘最大 fsync 率持续提交 write+fsync。分两个里程碑：M1（T1-T2）保持落盘才回包语义做线程迁移（行为等价、安全），M2（T3）切先回复 + 背压窗口。

**Tech Stack:** C，liburing（io_uring SQPOLL），pthread，eventfd，epoll（reactor）。

## Global Constraints

- 规格文件：`docs/superpowers/specs/2026-08-06-aof-thread-design.md`（方案 B）。
- 保留公共 API 签名：`persist_append_prepare / persist_flush_pending / persist_reap_completions / persist_drain_pending / persist_uring_fd / persist_force_aof_flush / persist_close / persist_purge_conn`（T3 才删 purge）。
- 同步模式（`aof_fsync_sync`）不建线程，走原主线程同步路径（仍用 `g_aof_buf`），**不动**。
- io_uring `IORING_SETUP_SINGLE_ISSUER`：T2 起只有 AOF 线程 get_sqe/prep/submit/reap，主线程永不触达 ring。
- conn 状态（`persist_pending`、`out_ring`、release 链表）只在主线程碰；AOF 线程从不触达 conn。
- 零拷贝：append 直接写 `g_cur_slot->aof_buf`，无整批深拷贝。
- 背压水位 `MAX_OUTSTANDING=16`（待刷批数），崩溃窗口最坏 ~7ms / 稳态 ~1.3ms。
- 每任务结束必须 `make` 通过 + 对应验证，再 commit。**只 stage 本任务涉及的文件**（工作区有无关未提交改动，勿 `git add -A`）。

---

### Task 1: write-in-place 槽化改造（主线程，保持落盘才回包语义）

把现 `g_aof_buf` 深拷贝模型改成 slot write-in-place。主线程仍持有 io_uring 并完成提交/收割（语义不变，仅结构变化）。这是 T2 线程迁移的安全底座。**同步模式保留原 `g_aof_buf` 路径不动。**

**Files:**
- Modify: `src/persistence/kvs_persist.c`

**Interfaces:**
- Produces: `handoff_current_slot()`、`submit_one_slot()`、`drain_completed()`、`slot_push_free/pop_free/push_ready/pop_ready/push_completed/pop_completed_all`（内部 static）、`g_cur_slot` / `g_ready_head` / `g_completed_head` / `g_free_slots` / `g_slot_lock` / `g_outstanding` / `MAX_OUTSTANDING` / `AOF_SLOT_INIT` / `AOF_SLOT_MAX`。公共函数签名不变。

- [ ] **Step 1: 新增槽数据声明（保留同步模式的 g_aof_buf）**

在现 `persist_slot_t` 上增加链表指针与 base_offset、aof_cap：

```c
typedef struct persist_slot_s {
    unsigned char *aof_buf;         /* 追加目标（write-in-place） */
    size_t         aof_len;
    size_t         aof_cap;         /* aof_buf 已分配容量 */
    long long      base_offset;     /* 本槽首个字节的 AOF 偏移 */
    int            cqe_seen, cqe_ok, last_error;
    int            in_use;
    persist_release_conn_t *release;   /* 本批连接（仅主线程碰），T3 删除 */
    struct persist_slot_s *ready_next, *completed_next;
} persist_slot_t;

static persist_slot_t *g_cur_slot = NULL;
static persist_slot_t *g_ready_head = NULL, *g_ready_tail = NULL;
static persist_slot_t *g_completed_head = NULL, *g_completed_tail = NULL;
static persist_slot_t *g_free_slots = NULL;
static pthread_mutex_t g_slot_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_outstanding = 0;             /* ready + in-flight 批数 */
#define MAX_OUTSTANDING 16
#define AOF_SLOT_INIT (64 * 1024)
#define AOF_SLOT_MAX  (4 * 1024 * 1024)

static long long g_aof_write_submitted = 0;  /* 主线程 append 推进 */
```

**保留** `g_aof_buf/g_aof_buf_len/g_aof_buf_cap`（仅同步模式用，T1-T3 期间不清除；T3 先回复后同步模式仍用）。`g_release_head` 语义改挂到 `g_cur_slot->release`。`g_inflight[PERSIST_INFLIGHT_SIZE]` 保留（槽 in_use 数组）。**`persist_init` 中 async-always 时把 `g_inflight[0..PERSIST_INFLIGHT_SIZE)` 全部 `slot_push_free` 进 `g_free_slots`（种子池，否则 append 卡死）**。`g_inflight_count` 不再维护（被 `g_outstanding` 取代），相关引用同步删除。

- [ ] **Step 2: 槽链表辅助函数（放 persist_inflight_release 附近）**

```c
static void slot_push_free(persist_slot_t *s) {   /* 仅主线程；保留 aof_buf/aof_cap 以便复用 */
    s->aof_len = 0; s->base_offset = 0;
    s->cqe_seen = s->cqe_ok = s->last_error = 0;
    s->in_use = 0; s->release = NULL;
    s->ready_next = g_free_slots; g_free_slots = s;
}
static persist_slot_t *slot_pop_free(void) {      /* 仅主线程 */
    persist_slot_t *s = g_free_slots;
    if (s) { g_free_slots = s->ready_next; s->ready_next = NULL; }
    return s;
}
static void slot_push_ready(persist_slot_t *s) {  /* 持 g_slot_lock 调用 */
    s->ready_next = NULL;
    if (g_ready_tail) g_ready_tail->ready_next = s; else g_ready_head = s;
    g_ready_tail = s; g_outstanding++;
}
static persist_slot_t *slot_pop_ready(void) {     /* 持 g_slot_lock 调用 */
    persist_slot_t *s = g_ready_head;
    if (s) { g_ready_head = s->ready_next; if (!g_ready_head) g_ready_tail = NULL; s->ready_next = NULL; }
    return s;
}
static void slot_push_completed(persist_slot_t *s) { /* 持 g_slot_lock 调用 */
    s->completed_next = NULL;
    if (g_completed_tail) g_completed_tail->completed_next = s; else g_completed_head = s;
    g_completed_tail = s; g_outstanding--;
}
static persist_slot_t *slot_pop_completed_all(void) { /* 持 g_slot_lock 调用 */
    persist_slot_t *h = g_completed_head;
    g_completed_head = g_completed_tail = NULL;
    return h;
}
```

- [ ] **Step 3: 重写 `persist_append_prepare`（异步 → write-in-place；同步路径保留原逻辑）**

```c
int persist_append_prepare(conn_t *c, const unsigned char *buf, size_t len,
                           unsigned char *resp, size_t resp_len) {
    (void)resp; (void)resp_len;
    if (g_aof_fd < 0) return g_aof_disabled ? KVS_PERSIST_OK : KVS_PERSIST_ERR;
    if (g_cfg.aof_fsync != KVS_AOF_FSYNC_ALWAYS) return KVS_PERSIST_OK;
    if (g_persist_fatal_error) return KVS_PERSIST_ERR;
    /* 同步模式（--aof-fsync-sync / --aof-fsync-sync-batch）：保留原逻辑不动，
     * append 到 g_aof_buf + per_command 分支（persist_sync_append_write / persist_flush_pending）。 */
    if (g_cfg.aof_fsync_sync) {
        /* 原 persist_append_prepare 的同步分支原样保留（含 g_aof_buf 成长 + per_command 处理） */
        ...原代码不动...
    }

    /* ---- 异步（默认/逐条）：write-in-place ---- */
    if (!g_cur_slot) {
        g_cur_slot = slot_pop_free();
        while (!g_cur_slot) {          /* free 空 = 上一批未回收 → 背压 */
            persist_reap_completions(); sched_yield();
            g_cur_slot = slot_pop_free();
        }
        g_cur_slot->aof_len = 0;
        g_cur_slot->base_offset = g_aof_write_submitted;
    }
    if (g_cur_slot->aof_len + len > g_cur_slot->aof_cap) {
        size_t need = g_cur_slot->aof_len + len;
        size_t new_cap = g_cur_slot->aof_cap ? g_cur_slot->aof_cap : AOF_SLOT_INIT;
        while (new_cap < need) new_cap *= 2;
        if (new_cap > AOF_SLOT_MAX) return KVS_PERSIST_ERR;
        unsigned char *nb = g_cur_slot->aof_buf
            ? kvs_realloc(g_cur_slot->aof_buf, new_cap)
            : (unsigned char *)kvs_malloc(new_cap);
        if (!nb) return KVS_PERSIST_ERR;
        g_cur_slot->aof_buf = nb;
        g_cur_slot->aof_cap = new_cap;
    }
    memcpy(g_cur_slot->aof_buf + g_cur_slot->aof_len, buf, len);
    g_cur_slot->aof_len += len;
    g_aof_write_submitted += (long long)len;

    /* release gating（T3 删除）：conn 挂当前槽，去重 */
    if (c) {
        int already = 0;
        for (persist_release_conn_t *r = g_cur_slot->release; r; r = r->next)
            if (r->c == c) { already = 1; break; }
        if (!already) {
            persist_release_conn_t *r = kvs_malloc(sizeof(*r));
            if (r) { r->c = c; r->next = g_cur_slot->release; g_cur_slot->release = r; c->persist_pending++; }
        }
    }

    if (g_cfg.aof_fsync_per_command || g_cur_slot->aof_len >= AOF_SLOT_MAX)
        persist_flush_pending();
    return KVS_PERSIST_OK;
}
```

> 同步分支的 `...原代码不动...` = 现 `persist_append_prepare` 里的 `g_aof_buf` 成长 + `aof_fsync_per_command` 分支（`persist_sync_append_write` / `persist_flush_pending`）原样搬进 `if (g_cfg.aof_fsync_sync) { }` 块。

- [ ] **Step 4: 重写 `persist_flush_pending` = handoff + 主线程提交 + 收割**

```c
static void handoff_current_slot(void) {
    if (!g_cur_slot || g_cur_slot->aof_len == 0) return;
    pthread_mutex_lock(&g_slot_lock);
    while (g_outstanding >= MAX_OUTSTANDING) {
        pthread_mutex_unlock(&g_slot_lock);
        drain_completed(); sched_yield();
        pthread_mutex_lock(&g_slot_lock);
    }
    slot_push_ready(g_cur_slot);
    g_cur_slot = NULL;
    pthread_mutex_unlock(&g_slot_lock);
}

static int submit_one_slot(persist_slot_t *s) {   /* 主线程；T2 移到 AOF 线程 */
    struct io_uring_sqe *sqe_w = io_uring_get_sqe(&g_persist_uring);
    if (!sqe_w) return -1;
    io_uring_prep_write(sqe_w, g_persist_aof_registered ? 0 : g_aof_fd,
                        s->aof_buf, s->aof_len, (off_t)s->base_offset);
    sqe_w->flags |= IOSQE_IO_LINK;
    if (g_persist_aof_registered) sqe_w->flags |= IOSQE_FIXED_FILE;
    io_uring_sqe_set_data(sqe_w, s);
    struct io_uring_sqe *sqe_f = io_uring_get_sqe(&g_persist_uring);
    if (!sqe_f) return -1;
    io_uring_prep_fsync(sqe_f, g_persist_aof_registered ? 0 : g_aof_fd, IORING_FSYNC_DATASYNC);
    if (g_persist_aof_registered) sqe_f->flags |= IOSQE_FIXED_FILE;
    io_uring_sqe_set_data(sqe_f, s);
    io_uring_submit(&g_persist_uring);
    return 0;
}

void persist_flush_pending(void) {
    if (g_cfg.aof_fsync_sync) { persist_sync_append_write(); return; }  /* 同步批量路径不动 */
    handoff_current_slot();
    for (;;) {   /* 提交全部 ready（主线程；T2 移线程） */
        pthread_mutex_lock(&g_slot_lock);
        persist_slot_t *s = slot_pop_ready();
        pthread_mutex_unlock(&g_slot_lock);
        if (!s) break;
        if (submit_one_slot(s) != 0) { g_persist_fatal_error = 1; break; }
    }
    persist_reap_completions();
}
```

- [ ] **Step 5: 重写 reap 路径（CQE 收割 + conn release + 回收槽）**

```c
void persist_reap_completions(void) {
    struct io_uring_cqe *cqe;
    while (io_uring_peek_cqe(&g_persist_uring, &cqe) == 0) {
        persist_slot_t *s = io_uring_cqe_get_data(cqe);
        if (s) {
            if (cqe->res > 0) { g_aof_write_offset += cqe->res; s->cqe_ok++; }
            else if (cqe->res == 0) s->cqe_ok++;
            else s->last_error = cqe->res;
            s->cqe_seen++;
        }
        io_uring_cqe_seen(&g_persist_uring, cqe);
        if (s && s->cqe_seen == 2) {
            if (s->cqe_ok != 2) {
                g_persist_fatal_error = 1;
                /* 兜底（对齐现 persist_inflight_release）：释放 release conns + 递减 persist_pending */
                for (persist_release_conn_t *r = s->release; r; r = r->next)
                    if (r->c && r->c->persist_pending > 0) r->c->persist_pending--;
                for (persist_release_conn_t *r = s->release; r; ) {
                    persist_release_conn_t *nx = r->next; kvs_free(r); r = nx;
                }
                s->release = NULL;
            } else {
                /* release conns（T3 删）：persist_pending--，归零 flush */
                for (persist_release_conn_t *r = s->release; r; r = r->next) {
                    if (r->c) {
                        if (r->c->persist_pending > 0) r->c->persist_pending--;
                        if (r->c->persist_pending == 0) flush_conn_output(r->c);
                    }
                }
                for (persist_release_conn_t *r = s->release; r; ) {
                    persist_release_conn_t *nx = r->next; kvs_free(r); r = nx;
                }
                s->release = NULL;
            }
            pthread_mutex_lock(&g_slot_lock);
            slot_push_completed(s);
            pthread_mutex_unlock(&g_slot_lock);
        }
    }
    drain_completed();
}

static void drain_completed(void) {
    pthread_mutex_lock(&g_slot_lock);
    persist_slot_t *h = slot_pop_completed_all();
    pthread_mutex_unlock(&g_slot_lock);
    for (persist_slot_t *s = h; s; ) {
        persist_slot_t *next = s->completed_next;
        s->completed_next = NULL;
        slot_push_free(s);
        s = next;
    }
}
```

`persist_inflight_reserve/release` 不再使用（其 slot 回收被 free 链表取代），删除或保留 unused（建议删除避免死代码）。`persist_submit_sqes` 保留（T2 drain 内部用）。

**`persist_drain_pending` 适配**：现实现（[kvs_persist.c:262-269](src/persistence/kvs_persist.c#L262)）的 `while (g_inflight_count > 0)` 条件在 `g_inflight_count` 不再维护后失效，改为 `while (g_outstanding > 0)`（g_outstanding 计数 ready+in-flight，双 CQE 完成递减）。

**`persist_purge_conn` 适配**：现实现只扫 `g_release_head` + `g_inflight[i].release`；release 改挂 `g_cur_slot->release` 后必须扩展——持 `g_slot_lock` 扫**所有** `g_inflight[]` 中 `in_use` 槽（含 g_cur_slot、ready、completed、in-flight）的 release 链表，移除该 conn + `c->persist_pending=0`（对齐现语义，防 conn 关闭 UAF）。

- [ ] **Step 6: 构建 + 验证**

```bash
make -j4 2>&1 | tail -5
```
Run: `make test_persist_aof_demo`（AOF 写入 + 恢复演示）。
Run: 起服务写 1w 条 → kill -9 → 重启恢复，确认 AOF 尾部 = 已回包命令（语义未变）。
Run: `redis-cli -p 5190 SAVE`、`BGREWRITEAOF` 各一次，无崩溃。
Expected: 编译通过、恢复完整、与改动前行为一致。

- [ ] **Step 7: Commit**

```bash
git add src/persistence/kvs_persist.c
git commit -m "refactor(persist): AOF write-in-place slot 化（无深拷贝，保持落盘才回包）"
```

---

### Task 2: AOF 线程接管 io_uring + 跨线程 fence（合并原 T2+T3）

**本任务必须整体完成才可提交**：`SINGLE_ISSUER` 使 uring 移线程后，主线程不能直接 submit_and_wait，SAVE/BGREWRITE/close 的 drain 必须是跨线程 fence——线程迁移与 fence 不可分。

**Files:**
- Modify: `src/persistence/kvs_persist.c`
- Modify: `src/core/reactor.c:264,321-327`

**Interfaces:**
- Consumes: Task 1 的槽辅助函数、`handoff_current_slot`、`submit_one_slot`、`drain_completed`。
- Produces: `persist_uring_fd()` 改返回 `g_aof_complete_efd`；`g_aof_wake_efd`（内部）；线程 `aof_thread_main`；fence 原语 `fence_wait/fence_signal/aof_work_request`；`g_aof_thread_created`。

- [ ] **Step 1: eventfd、线程生命周期、work flag**

```c
static int g_aof_wake_efd = -1;       /* 主线程 → AOF 线程 */
static int g_aof_complete_efd = -1;   /* AOF 线程 → 主线程 */
static pthread_t g_aof_thread;
static int g_aof_thread_created = 0;
static pthread_mutex_t g_work_mutex = PTHREAD_MUTEX_INITIALIZER;
static int g_work_flags = 0;
#define AOF_WORK_STOP 1
#define AOF_WORK_DRAIN 2
#define AOF_WORK_REREGISTER 4
```

`persist_uring_init_once()` 与 `persist_uring_close()` 移入线程内调用。`persist_uring_fd()` 返回 `g_aof_complete_efd`：
```c
int persist_uring_fd(void) { return g_aof_complete_efd; }
```

- [ ] **Step 2: AOF 线程主循环 + 收割/信号函数**

```c
/* CQE 收割（AOF 线程内）：双 CQE 完成 → 槽挂 completed + signal 主线程 */
static void reap_completions_inline(void) {
    struct io_uring_cqe *cqe;
    while (io_uring_peek_cqe(&g_persist_uring, &cqe) == 0) {
        persist_slot_t *s = io_uring_cqe_get_data(cqe);
        if (s) {
            if (cqe->res > 0) { g_aof_write_offset += cqe->res; s->cqe_ok++; }
            else if (cqe->res == 0) s->cqe_ok++;
            else s->last_error = cqe->res;
            s->cqe_seen++;
        }
        io_uring_cqe_seen(&g_persist_uring, cqe);
        if (s && s->cqe_seen == 2) {
            if (s->cqe_ok != 2) g_persist_fatal_error = 1;
            pthread_mutex_lock(&g_slot_lock);
            slot_push_completed(s);
            pthread_mutex_unlock(&g_slot_lock);
        }
    }
}
static void signal_complete_efd(void) {
    uint64_t one = 1; (void)!write(g_aof_complete_efd, &one, sizeof(one));
}

static void *aof_thread_main(void *arg) {
    (void)arg;
    if (persist_uring_init_once() != 0) { g_persist_fatal_error = 1; return NULL; }
    for (;;) {
        struct pollfd pfd[2];
        pfd[0].fd = g_aof_wake_efd;  pfd[0].events = POLLIN;
        pfd[1].fd = g_persist_eventfd; pfd[1].events = POLLIN;
        poll(pfd, 2, -1);
        if (pfd[0].revents & POLLIN) {
            uint64_t v; while (read(g_aof_wake_efd, &v, sizeof(v)) > 0) {}
            pthread_mutex_lock(&g_work_mutex);
            int flags = g_work_flags; g_work_flags = 0;
            pthread_mutex_unlock(&g_work_mutex);
            if (flags & AOF_WORK_REREGISTER) aof_thread_reregister_fd();
            if (flags & AOF_WORK_DRAIN)     aof_thread_drain_request();
            if (flags & AOF_WORK_STOP)      break;
            for (;;) {   /* 提交全部 ready */
                pthread_mutex_lock(&g_slot_lock);
                persist_slot_t *s = slot_pop_ready();
                pthread_mutex_unlock(&g_slot_lock);
                if (!s) break;
                if (submit_one_slot(s) != 0) { g_persist_fatal_error = 1; break; }
            }
        }
        if (pfd[1].revents & POLLIN) {
            uint64_t v; while (read(g_persist_eventfd, &v, sizeof(v)) > 0) {}
            reap_completions_inline();
            signal_complete_efd();
            pthread_mutex_lock(&g_work_mutex);
            int f = g_work_flags; g_work_flags = (f & AOF_WORK_REREGISTER) ? f : 0;
            pthread_mutex_unlock(&g_work_mutex);
            if (f & AOF_WORK_DRAIN) aof_thread_drain_request();
        }
    }
    persist_uring_close();
    return NULL;
}
```

> 说明：DRAIN/REREGISTER 请求可能与 CQE 事件同时到达。实现时保证：线程侧 drain/reregister 会先清空 ready 并等 `g_outstanding==0`，因此无论从 wake 还是 uring 分支进入都能正确处理。上面 CQE 分支的 flag 读取是示意，可按实际简化。

- [ ] **Step 3: 主线程 flush/reap 简化为 handoff + signal + drain**

```c
void persist_flush_pending(void) {
    if (g_cfg.aof_fsync_sync) { persist_sync_append_write(); return; }
    int had = (g_cur_slot && g_cur_slot->aof_len > 0);
    handoff_current_slot();
    if (had) { uint64_t one = 1; (void)!write(g_aof_wake_efd, &one, sizeof(one)); }
    persist_reap_completions();
}

void persist_reap_completions(void) { drain_completed(); }
```

conn release（T3 删）随主线程 `drain_completed` 处理——把 Task 1 Step 5 里 CQE 循环的 release 段移到 `drain_completed` 的槽回收前（AOF 线程不碰 conn）：

```c
static void drain_completed(void) {
    pthread_mutex_lock(&g_slot_lock);
    persist_slot_t *h = slot_pop_completed_all();
    pthread_mutex_unlock(&g_slot_lock);
    for (persist_slot_t *s = h; s; ) {
        persist_slot_t *next = s->completed_next;
        s->completed_next = NULL;
        for (persist_release_conn_t *r = s->release; r; r = r->next) {
            if (r->c) {
                if (r->c->persist_pending > 0) r->c->persist_pending--;
                if (r->c->persist_pending == 0) flush_conn_output(r->c);
            }
        }
        for (persist_release_conn_t *r = s->release; r; ) {
            persist_release_conn_t *nx = r->next; kvs_free(r); r = nx;
        }
        s->release = NULL;
        slot_push_free(s);
        s = next;
    }
}
```

`submit_one_slot`（Task 1 的 static）改为 AOF 线程使用；主线程不再调用。

- [ ] **Step 4: 线程创建/停止接入 persist_init/persist_close**

```c
static int aof_thread_start(void) {
    g_aof_wake_efd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    g_aof_complete_efd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (g_aof_wake_efd < 0 || g_aof_complete_efd < 0) return -1;
    if (pthread_create(&g_aof_thread, NULL, aof_thread_main, NULL) != 0) return -1;
    g_aof_thread_created = 1;
    return 0;
}
```

`persist_init()`：`aof_fsync==ALWAYS && !aof_fsync_sync` 时调用 `aof_thread_start()`（失败则 `g_persist_fatal_error=1`）。`persist_init` 不再调用 `persist_uring_init_once`（线程内做）。

- [ ] **Step 5: 跨线程 fence（drain/force_flush/close/reregister）**

fence 原语 + 双完成 flag：

```c
static pthread_mutex_t g_fence_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_fence_cond = PTHREAD_COND_INITIALIZER;
static int g_drain_done = 0;
static int g_reregister_done = 0;
static int g_reregister_fd = -1;

static void fence_wait(int *done_flag) {
    pthread_mutex_lock(&g_fence_mutex);
    while (!*done_flag) pthread_cond_wait(&g_fence_cond, &g_fence_mutex);
    pthread_mutex_unlock(&g_fence_mutex);
}
static void fence_signal(int *done_flag) {
    pthread_mutex_lock(&g_fence_mutex);
    *done_flag = 1;
    pthread_cond_broadcast(&g_fence_cond);
    pthread_mutex_unlock(&g_fence_mutex);
}
static void aof_work_request(int flag) {
    pthread_mutex_lock(&g_work_mutex);
    g_work_flags |= flag;
    pthread_mutex_unlock(&g_work_mutex);
    uint64_t one = 1; (void)!write(g_aof_wake_efd, &one, sizeof(one));
}
```

AOF 线程侧 drain 请求处理（含 ready 提交 + 等 `g_outstanding==0`；`g_outstanding` 计数 ready+in-flight，双 CQE 完成经 `slot_push_completed` 递减）：

```c
static void aof_thread_drain_request(void) {
    for (;;) {
        pthread_mutex_lock(&g_slot_lock);
        persist_slot_t *s = slot_pop_ready();
        pthread_mutex_unlock(&g_slot_lock);
        if (!s) break;
        if (submit_one_slot(s) != 0) { g_persist_fatal_error = 1; break; }
    }
    while (g_outstanding > 0) {
        io_uring_submit_and_wait(&g_persist_uring, 1);
        reap_completions_inline();
        signal_complete_efd();
    }
    fence_signal(&g_drain_done);
}
```

主线程 fence 函数：

```c
void persist_drain_pending(void) {
    if (g_cfg.aof_fsync_sync || !g_aof_thread_created) {
        /* 同步模式/无线程：保留原逻辑（persist_flush_pending + while inflight submit_and_wait + reap） */
        ...原代码不动...
    }
    if (g_cur_slot && g_cur_slot->aof_len > 0) handoff_current_slot();  /* handoff 内含背压，无 signal */
    g_drain_done = 0;
    aof_work_request(AOF_WORK_DRAIN);
    fence_wait(&g_drain_done);
}

int persist_force_aof_flush(void) {
    if (g_aof_fd < 0) return -1;
    persist_drain_pending();              /* 已含每批 fsync */
    if (persist_fsync_fd_best_effort(g_aof_fd) != 0) return -1;  /* 兜底 */
    return 0;
}

void persist_close(void) {
    if (g_aof_thread_created) {
        persist_drain_pending();
        aof_work_request(AOF_WORK_STOP);
        pthread_join(g_aof_thread, NULL);           /* 线程内 persist_uring_close() */
        if (g_aof_wake_efd >= 0) close(g_aof_wake_efd);
        if (g_aof_complete_efd >= 0) close(g_aof_complete_efd);
        g_aof_thread_created = 0;
    } else {
        /* 同步模式：原 persist_close 逻辑 */
        ...原代码不动...
    }
    if (g_aof_fd >= 0) close(g_aof_fd);
    g_aof_fd = -1;
    for (int i = 0; i < PERSIST_INFLIGHT_SIZE; i++)
        if (g_inflight[i].in_use) kvs_free(g_inflight[i].aof_buf);
}

void persist_reregister_aof_fd(void) {
    if (!g_aof_thread_created || g_aof_fd < 0) return;
    g_reregister_fd = g_aof_fd;           /* 新 fd 已由 finalize_rewrite_parent 打开 */
    g_reregister_done = 0;
    aof_work_request(AOF_WORK_REREGISTER);
    fence_wait(&g_reregister_done);
}

static void aof_thread_reregister_fd(void) {
    int fd = g_reregister_fd;
    aof_thread_drain_request();           /* 先清 ready + in-flight */
    if (g_persist_aof_registered) io_uring_unregister_files(&g_persist_uring);
    if (fd >= 0 && io_uring_register_files(&g_persist_uring, &fd, 1) == 0)
        g_persist_aof_registered = 1;
    else
        g_persist_aof_registered = 0;
    fence_signal(&g_reregister_done);
}
```

> 注意 `aof_thread_drain_request` 里 `fence_signal(&g_drain_done)` 与 reregister 里先 drain 再 signal `g_reregister_done` 的复用：reregister 内部调用 drain 时会多置一次 `g_drain_done`，无害（主线程只等各自 flag）。

- [ ] **Step 6: persist_purge_conn 适配（T3 删）**

[reactor.c:88](src/core/reactor.c#L88) 调用不变。函数改为：持 `g_slot_lock` 扫 `g_cur_slot`（主线程，无锁）+ ready + completed + in-flight 槽（持锁）的 release 链表移除该 conn；`c->persist_pending=0`。AOF 线程不碰 release，仅锁链表归属。

- [ ] **Step 7: reactor 事件处理器调整**

[reactor.c:321-327](src/core/reactor.c#L321) `persist_uring_fd()` 现返回 complete_efd（语义「批完成」），处理器只 drain：
```c
if (fd == persist_uring_fd()) {
    uint64_t val; ssize_t nread = read(fd, &val, sizeof(val)); (void)nread;
    persist_reap_completions();   /* 删掉原来的 persist_flush_pending() */
    continue;
}
```
[reactor.c:365-366](src/core/reactor.c#L365) 保留 `persist_flush_pending(); persist_reap_completions();`。[reactor.c:264](src/core/reactor.c#L264) 注册逻辑不变。

- [ ] **Step 8: 构建 + 完整验证**

```bash
make -j4 2>&1 | tail -5
```
Run 全套（**本任务是 M1 门禁**）：
1. `make test_persist_aof_demo`、重启恢复验证。
2. 起服务 + `redis-benchmark -P 20` 运行中执行 `redis-cli -p 5190 SAVE`，无卡死、无崩溃。
3. 运行中 `redis-cli -p 5190 BGREWRITEAOF`，完成后重启恢复，数据完整。
4. 运行中 kill -9 → 重启恢复，AOF 尾部 = 已回包（落盘才回包语义，无丢失）。
5. 客户端断连（benchmark 中途 Ctrl-C）无 UAF（persist_purge_conn 路径）。
6. 高 P 冒烟：`redis-benchmark -p 5190 -n 1000000 -c 50 -P 20 -d 64 -r 1000000 HSET key:__rand_int__ value`，相对基线（README P=20 660k）无崩溃、吞吐正常。
Expected: 全部通过、P=1 不回归（~42-45k）。

- [ ] **Step 9: Commit**

```bash
git add src/persistence/kvs_persist.c src/core/reactor.c
git commit -m "feat(persist): AOF 独立线程接管 io_uring + 跨线程 fence（语义不变）"
```

---

### Task 3: 先回复（移除落盘才回包 gating）

删除 `persist_pending`/release 链表机制，回包立即发。这是语义切换点，P=1 应显著提升。

**Files:**
- Modify: `src/persistence/kvs_persist.c`
- Modify: `include/kvstore/kvstore.h:294`（`persist_pending` 字段删除）
- Modify: `src/core/reactor.c`（`persist_pending` 判断与 `persist_purge_conn` 调用删除）

**Interfaces:**
- Consumes: Task 2 的槽/线程/fence。
- Produces: 无新接口；`persist_append_prepare` 不再挂 release、立即回包；`persist_purge_conn` 删除。

- [ ] **Step 1: 删 release gating**

`persist_append_prepare`：删除 `if (c) { ...release... }` 块（T1 Step 3 的 release 段）。删除 `persist_release_conn_t` 类型、release 链表操作。`drain_completed`（T2 版）里删 conn 处理段（只剩槽回收）。`persist_purge_conn` 整个删除（header 声明 + [reactor.c:88](src/core/reactor.c#L88) 调用）。`conn_t.persist_pending` 删除（[kvstore.h:294](include/kvstore/kvstore.h#L294) + reactor.c 所有 `persist_pending` 判断）。

- [ ] **Step 2: 确认先回复语义**

[reactor.c](src/core/reactor.c) 的 `flush_conn_output` 里 `persist_pending` 跳过逻辑、`on_write` 的 `!c->persist_pending` 判断全部删除。确认回包路径无残留 gating。

- [ ] **Step 3: 构建 + 验证**

```bash
make -j4 2>&1 | tail -5
```
Run: `make test_persist_aof_demo`、恢复验证、P=1 冒烟看 QPS 是否到 ~100k+。
Run: 满负荷 `-c 50 -P 1` 跑 30s → kill -9 → 重启，量 AOF 尾部与「最后回包命令」差距 ≤ 窗口上界（验证背压 `MAX_OUTSTANDING` 生效）。
Expected: 编译通过、P=1 QPS 显著提升（45k → ~100k+）、窗口有界。

- [ ] **Step 4: Commit**

```bash
git add src/persistence/kvs_persist.c include/kvstore/kvstore.h src/core/reactor.c
git commit -m "feat(persist): AOF 先回复（回包不等 fsync，有界窗口背压）"
```

---

### Task 4: 文档同步

**Files:**
- Modify: `README.md`（「真·落盘才回包」相关段落）、`docs/save-aof-always-mode-comparison.md`、`docs/optimization-history/aof-concurrent.md`、`docs/aof-fsync-modes-analysis.md`

- [ ] **Step 1: 更新语义表述**

把「kvstore AOF always 为真·落盘才回包（响应等 fsync 完成，对齐 redis 7.x #9678）」改为「**先回复 + 有界窗口**：回包不等 fsync，AOF 线程按磁盘最大 fsync 率定频刷盘，崩溃窗口稳态 ~1.3ms / 最坏 ~7ms」。同步更新各文档结论与模式对比表说明。

- [ ] **Step 2: 构建 + 验证**

```bash
grep -n "落盘才回包\|真·落盘" README.md docs/*.md docs/optimization-history/*.md
```
Expected: 无残留「落盘才回包」描述（除历史/归档文档）。

- [ ] **Step 3: Commit**

```bash
git add README.md docs/save-aof-always-mode-comparison.md docs/optimization-history/aof-concurrent.md docs/aof-fsync-modes-analysis.md
git commit -m "docs: AOF 语义更新为先回复+有界窗口"
```

---

### Task 5: 基准对比

**Files:**
- Create: `docs/superpowers/bench/2026-08-06-aof-thread-reply-first.md`

- [ ] **Step 1: 跑 P-sweep**

复用 `tools/bench/run_aof_bench.py` 的 `-c 50 -P N` 口径（N=1,10,20,40,80,160），每轮重启空库：
```bash
for P in 1 10 20 40 80 160; do
  pkill -x kvstore; rm -f kvstore.aof kvstore.dump
  taskset -c 2,3 ./kvstore --port 5190 --role master --mem libc --net reactor --appendfsync always &
  sleep 0.3
  redis-benchmark -p 5190 -n 1000000 -c 50 -P $P -d 64 -r 1000000 HSET key:__rand_int__ value
done
```

- [ ] **Step 2: 记录结果表**

| P | 新实现 | 基线(README 08-04) | 变化 |
|---|---|---|---|
| 1 | ? | 42,717 | 预期 ~110k（+150%） |
| 10 | ? | 373,692 | 持平/略升 |
| 20 | ? | 660,066 | 持平/略升 |
| 40 | ? | 891,266 | 持平/略升 |
| 80 | ? | 1,014,199 | 持平/略升 |
| 160 | ? | 1,024,590 | 持平/略升 |

- [ ] **Step 3: 顺带测异步逐条 + 复制冒烟**

`--aof-fsync-per-command` 语义（先回复）验证 + P=1 值。主从复制启动 + 基本同步确认（fence/先回复不破坏 `repl_durable_offset_ack`）。

- [ ] **Step 4: 回填 Task 4 文档 + 汇报**

如实报告：P=1 提升多少、高 P 持平/回退、窗口实测、复制验证结果。若有回退如实说明并给下一步。

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/bench/ benchmarks/data/
git commit -m "bench: AOF 先回复 P-sweep 对比"
```

---

## 自检记录

- **Spec 覆盖**：spec §5（主线程 append/handoff/drain）→ T1/T2/T3；§6（AOF 线程）→ T2；§7（fence）→ T2；§9（错误/窗口）→ T2/T3；§10（验证基准）→ T5 + 各任务验证步骤；§12（文档）→ T4。全部覆盖。
- **占位符**：无 TBD/TODO。`...原代码不动...` 明确指向现有代码片段（同步分支/旧 drain/旧 close），非新代码。
- **类型一致性**：`slot_push_*/pop_*`、`handoff_current_slot`、`submit_one_slot`、`reap_completions_inline`、`drain_completed`、`aof_thread_main`、`fence_wait/fence_signal/aof_work_request`、`g_aof_thread_created` 在各任务间名称一致。
- **修正记录（pre-flight）**：
  1. 同步模式保留 `g_aof_buf`（T1 不再删除），`persist_append_prepare` 同步分支保留原逻辑——修「删 g_aof_buf 破坏同步模式」冲突。
  2. T2 与 T3 合并：`SINGLE_ISSUER` 使 uring 移线程后主线程不能 submit_and_wait，drain/reregister/close 必须是 fence——修「T2 中间态 SAVE/BGREWRITE 断裂」冲突。
