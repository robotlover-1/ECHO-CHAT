/*
 * test_stream.c - end-to-end streaming tests for C client + NtyCo server.
 *
 * Covers: multi-chunk order/content, zero-chunk, data-then-error, a server that
 * (wrongly) sends a second STREAM_END (must not hang/crash the client), and a
 * client cancelling a slow server mid-stream.
 */

#define _GNU_SOURCE

#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "zrpc.h"

#define SRV_PORT  19094
#define SRV_TOKEN "stream-test-token"

static int g_fail = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            g_fail++;                                                   \
        }                                                               \
    } while (0)

static zrpc_server_t *g_srv;
static volatile int g_slow_sent = 0;   /* chunks the slow handler managed to send */

/* ---- collector used by the client stream callback ---- */
typedef struct collector {
    int     chunks;
    int     last_event;
    int     last_status;
    int     ended;
    char    log[65536];
    size_t  log_len;
} collector_t;

static void on_stream_event(uint64_t handle, uint64_t rid, int event, int status,
                            const void *data, uint32_t data_len)
{
    collector_t *col = (collector_t *)(uintptr_t)handle;
    (void)rid;
    col->last_event = event;
    col->last_status = status;
    switch (event) {
    case ZRPC_MSG_STREAM_DATA:
        col->chunks++;
        if (col->log_len + data_len + 1 < sizeof(col->log)) {
            memcpy(col->log + col->log_len, data, data_len);
            col->log_len += data_len;
            col->log[col->log_len] = '\n';
            col->log_len++;
        }
        break;
    case ZRPC_MSG_STREAM_END:
        col->ended = 1;
        break;
    default:
        break;   /* ERROR: last_status carries the code */
    }
}

/* ---- handlers (run synchronously inside the NtyCo coroutine) ---- */

static int handler_multi(uint64_t handle, uint64_t rid, int fd,
                         const void *req, uint32_t req_len, uint64_t deadline)
{
    zrpc_server_t *srv = (zrpc_server_t *)(uintptr_t)handle;
    (void)req; (void)req_len; (void)deadline;
    for (int i = 0; i < 20; i++) {
        char buf[32];
        int n = snprintf(buf, sizeof(buf), "{\"n\":%d}", i);
        if (zrpc_server_send_stream_data(srv, fd, rid, buf, (uint32_t)n) != ZRPC_STATUS_OK)
            return 1;
    }
    return zrpc_server_send_stream_end(srv, fd, rid) == ZRPC_STATUS_OK ? 0 : 1;
}

static int handler_zero(uint64_t handle, uint64_t rid, int fd,
                        const void *req, uint32_t req_len, uint64_t deadline)
{
    zrpc_server_t *srv = (zrpc_server_t *)(uintptr_t)handle;
    (void)req; (void)req_len; (void)deadline;
    return zrpc_server_send_stream_end(srv, fd, rid) == ZRPC_STATUS_OK ? 0 : 1;
}

static int handler_err_after_data(uint64_t handle, uint64_t rid, int fd,
                                  const void *req, uint32_t req_len, uint64_t deadline)
{
    zrpc_server_t *srv = (zrpc_server_t *)(uintptr_t)handle;
    (void)req; (void)req_len; (void)deadline;
    zrpc_server_send_stream_data(srv, fd, rid, "{\"n\":0}", 7);
    zrpc_server_send_stream_data(srv, fd, rid, "{\"n\":1}", 7);
    return zrpc_server_send_error(srv, fd, rid, ZRPC_STATUS_INVALID_ARGUMENT, "boom after data");
}

static int handler_double_end(uint64_t handle, uint64_t rid, int fd,
                              const void *req, uint32_t req_len, uint64_t deadline)
{
    zrpc_server_t *srv = (zrpc_server_t *)(uintptr_t)handle;
    (void)req; (void)req_len; (void)deadline;
    zrpc_server_send_stream_end(srv, fd, rid);   /* client stops at the first END */
    zrpc_server_send_stream_end(srv, fd, rid);   /* second must not crash anything */
    return 0;
}

static int handler_slow(uint64_t handle, uint64_t rid, int fd,
                        const void *req, uint32_t req_len, uint64_t deadline)
{
    zrpc_server_t *srv = (zrpc_server_t *)(uintptr_t)handle;
    (void)req; (void)req_len; (void)deadline;
    for (int i = 0; i < 100000; i++) {
        char buf[32];
        int n = snprintf(buf, sizeof(buf), "{\"i\":%d}", i);
        g_slow_sent = i;
        if (zrpc_server_send_stream_data(srv, fd, rid, buf, (uint32_t)n) != ZRPC_STATUS_OK)
            return 0;                       /* client went away: stop streaming */
        usleep(200);
    }
    zrpc_server_send_stream_end(srv, fd, rid);
    return 0;
}

/* ---- helpers ---- */

static zrpc_client_t *client(void)
{
    return zrpc_client_new("127.0.0.1", SRV_PORT, SRV_TOKEN, 1000, 3000);
}

static void wait_ready(void)
{
    for (int i = 0; i < 200; i++) {
        zrpc_client_t *c = client();
        if (c && zrpc_client_ping(c, 200) == ZRPC_STATUS_OK) {
            zrpc_client_free(c);
            return;
        }
        if (c) zrpc_client_free(c);
        usleep(10000);
    }
    fprintf(stderr, "server not ready\n");
    g_fail++;
}

/* ---- tests ---- */

static void test_multi_chunks(void)
{
    collector_t col;
    memset(&col, 0, sizeof(col));
    zrpc_client_t *c = client();
    CHECK(c != NULL);
    int st = zrpc_client_call_stream(c, "s.multi", "{\"ask\":1}", 9, 0,
                                     (uint64_t)(uintptr_t)&col, on_stream_event);
    CHECK(st == ZRPC_STATUS_OK);
    CHECK(col.ended == 1 && col.chunks == 20);
    CHECK(col.last_event == ZRPC_MSG_STREAM_END);

    /* reconstruct expected log */
    char expect[65536];
    size_t e = 0;
    for (int i = 0; i < 20; i++)
        e += (size_t)snprintf(expect + e, sizeof(expect) - e, "{\"n\":%d}\n", i);
    CHECK(col.log_len == e && memcmp(col.log, expect, e) == 0);
    zrpc_client_free(c);
}

static void test_zero_chunks(void)
{
    collector_t col;
    memset(&col, 0, sizeof(col));
    zrpc_client_t *c = client();
    int st = zrpc_client_call_stream(c, "s.zero", "{}", 2, 0,
                                     (uint64_t)(uintptr_t)&col, on_stream_event);
    CHECK(st == ZRPC_STATUS_OK);
    CHECK(col.ended == 1 && col.chunks == 0);
    zrpc_client_free(c);
}

static void test_error_after_data(void)
{
    collector_t col;
    memset(&col, 0, sizeof(col));
    zrpc_client_t *c = client();
    int st = zrpc_client_call_stream(c, "s.err", "{}", 2, 0,
                                     (uint64_t)(uintptr_t)&col, on_stream_event);
    CHECK(st == ZRPC_STATUS_INVALID_ARGUMENT);
    CHECK(col.chunks == 2);
    CHECK(col.last_event == ZRPC_MSG_ERROR && col.last_status == ZRPC_STATUS_INVALID_ARGUMENT);
    zrpc_client_free(c);
}

static void test_double_end_is_safe(void)
{
    collector_t col;
    memset(&col, 0, sizeof(col));
    zrpc_client_t *c = client();
    int st = zrpc_client_call_stream(c, "s.double", "{}", 2, 0,
                                     (uint64_t)(uintptr_t)&col, on_stream_event);
    CHECK(st == ZRPC_STATUS_OK);
    CHECK(col.ended == 1 && col.chunks == 0);
    zrpc_client_free(c);
}

struct cancel_arg { zrpc_client_t *c; collector_t col; int st; };
static void *slow_consumer(void *p)
{
    struct cancel_arg *a = (struct cancel_arg *)p;
    a->st = zrpc_client_call_stream(a->c, "s.slow", "{}", 2, 0,
                                    (uint64_t)(uintptr_t)&a->col, on_stream_event);
    return NULL;
}

static void test_client_cancel_mid_stream(void)
{
    struct cancel_arg a;
    memset(&a, 0, sizeof(a));
    a.c = client();
    CHECK(a.c != NULL);

    g_slow_sent = 0;
    pthread_t th;
    pthread_create(&th, NULL, slow_consumer, &a);
    usleep(80000);                          /* let a couple of chunks flow */
    zrpc_client_cancel(a.c);                /* wake the blocked reader */
    pthread_join(th, NULL);

    CHECK(a.st == ZRPC_STATUS_CANCELLED);
    CHECK(a.col.chunks > 0);                /* saw data before cancelling */
    CHECK(g_slow_sent < 100000);            /* server stopped early */
    zrpc_client_free(a.c);
}

int main(void)
{
    zrpc_server_options_t opt;
    memset(&opt, 0, sizeof(opt));
    opt.address = "127.0.0.1:19094";
    opt.access_token = SRV_TOKEN;
    g_srv = zrpc_server_new(&opt);
    CHECK(g_srv != NULL);

    uintptr_t h = (uintptr_t)g_srv;
    CHECK(zrpc_server_register(g_srv, "s.multi",  1, h, handler_multi) == ZRPC_STATUS_OK);
    CHECK(zrpc_server_register(g_srv, "s.zero",   1, h, handler_zero) == ZRPC_STATUS_OK);
    CHECK(zrpc_server_register(g_srv, "s.err",    1, h, handler_err_after_data) == ZRPC_STATUS_OK);
    CHECK(zrpc_server_register(g_srv, "s.double", 1, h, handler_double_end) == ZRPC_STATUS_OK);
    CHECK(zrpc_server_register(g_srv, "s.slow",   1, h, handler_slow) == ZRPC_STATUS_OK);
    CHECK(zrpc_server_serve(g_srv) == ZRPC_STATUS_OK);

    wait_ready();
    if (g_fail) return 1;

    test_multi_chunks();
    test_zero_chunks();
    test_error_after_data();
    test_double_end_is_safe();
    test_client_cancel_mid_stream();

    if (g_fail == 0) {
        printf("test_stream: all tests passed\n");
        return 0;
    }
    fprintf(stderr, "test_stream: %d check(s) failed\n", g_fail);
    return 1;
}
