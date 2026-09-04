/*
 * test_unary.c - end-to-end unary tests for the C client + NtyCo server.
 *
 * Usage:
 *   ./test_unary              functional tests (auth, ping, echo, conn reuse)
 *   ./test_unary load N       sequential load test of N unary calls on one conn
 *   ./test_unary load N T     N calls split across T worker threads
 *
 * The load mode measures fd count / RSS across the run as a leak canary.
 */

#define _GNU_SOURCE

#include <dirent.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#include "zrpc.h"
#include "cJSON.h"

#define SRV_PORT  19091
#define SRV_TOKEN "sekret-test-token"

static int g_fail = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            g_fail++;                                                   \
        }                                                               \
    } while (0)

static zrpc_server_t *g_srv;

/* Handler checks it received the exact request bytes, then replies a+b. */
static const char *g_expect_req = NULL;
static int g_expect_len = 0;

static int add_handler(uint64_t handle, uint64_t rid, int fd,
                       const void *req, uint32_t req_len, uint64_t deadline)
{
    zrpc_server_t *srv = (zrpc_server_t *)(uintptr_t)handle;
    (void)deadline;

    if (g_expect_req && req_len == (uint32_t)g_expect_len
        && memcmp(req, g_expect_req, req_len) == 0) {
        /* payload verbatim match */
    } else if (g_expect_req) {
        fprintf(stderr, "payload mismatch: got '%.*s'\n", (int)req_len, (char *)req);
    }

    char *buf = (char *)malloc(req_len + 1);
    memcpy(buf, req, req_len);
    buf[req_len] = '\0';
    cJSON *root = cJSON_Parse(buf);
    free(buf);
    long a = 0, b = 0;
    if (root) {
        cJSON *ja = cJSON_GetObjectItemCaseSensitive(root, "a");
        cJSON *jb = cJSON_GetObjectItemCaseSensitive(root, "b");
        if (cJSON_IsNumber(ja)) a = (long)ja->valuedouble;
        if (cJSON_IsNumber(jb)) b = (long)jb->valuedouble;
    }
    cJSON_Delete(root);

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddNumberToObject(resp, "sum", (double)(a + b));
    char *body = cJSON_PrintUnformatted(resp);
    cJSON_Delete(resp);

    int st = zrpc_server_send_response(srv, fd, rid, body, (uint32_t)strlen(body));
    free(body);
    return st == ZRPC_STATUS_OK ? 0 : 1;
}

static long get_sum(const char *resp_json)
{
    cJSON *root = cJSON_Parse(resp_json);
    long s = -999;
    if (root) {
        cJSON *js = cJSON_GetObjectItemCaseSensitive(root, "sum");
        if (cJSON_IsNumber(js)) s = (long)js->valuedouble;
    }
    cJSON_Delete(root);
    return s;
}

static int fd_count(void)
{
    DIR *d = opendir("/proc/self/fd");
    if (!d) return -1;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] != '.') n++;
    }
    closedir(d);
    return n;
}

static long rss_kb(void)
{
    FILE *f = fopen("/proc/self/status", "r");
    char line[256];
    long kb = -1;
    while (f && fgets(line, sizeof(line), f)) {
        if (strncmp(line, "VmRSS:", 6) == 0) { kb = atol(line + 6); break; }
    }
    if (f) fclose(f);
    return kb;
}

static zrpc_client_t *make_client(const char *token)
{
    return zrpc_client_new("127.0.0.1", SRV_PORT, token, 1000, 3000);
}

static void wait_server_ready(void)
{
    for (int i = 0; i < 200; i++) {
        zrpc_client_t *c = make_client(NULL);
        if (c) {
            if (zrpc_client_ping(c, 200) == ZRPC_STATUS_OK) {
                zrpc_client_free(c);
                return;
            }
            zrpc_client_free(c);
        }
        usleep(10000);
    }
    fprintf(stderr, "server did not become ready\n");
    g_fail++;
}

/* ---- functional ---- */

static void test_ping(void)
{
    zrpc_client_t *c = make_client(NULL);
    CHECK(c != NULL);
    CHECK(zrpc_client_ping(c, 500) == ZRPC_STATUS_OK);
    zrpc_client_close(c);
    zrpc_client_free(c);
}

static void test_unary_ok(void)
{
    const char *req = "{\"a\":2,\"b\":40}";
    g_expect_req = req;
    g_expect_len = (int)strlen(req);

    zrpc_client_t *c = make_client(SRV_TOKEN);
    CHECK(c != NULL);
    zrpc_buffer_t resp = { NULL, 0, 0 };
    int st = zrpc_client_call_unary(c, "test.add", req, (uint32_t)strlen(req), 0, &resp);
    CHECK(st == ZRPC_STATUS_OK);
    if (st == ZRPC_STATUS_OK) {
        CHECK(get_sum((char *)resp.data) == 42);
        zrpc_buffer_free(&resp);
    }
    zrpc_client_free(c);
    g_expect_req = NULL;
}

static void test_unknown_method(void)
{
    zrpc_client_t *c = make_client(SRV_TOKEN);
    zrpc_buffer_t resp = { NULL, 0, 0 };
    int st = zrpc_client_call_unary(c, "test.no_such_method", "{\"a\":1}", 8, 0, &resp);
    CHECK(st == ZRPC_STATUS_NOT_FOUND);
    zrpc_client_free(c);
}

static void test_bad_token(void)
{
    zrpc_client_t *c = make_client("wrong-token");
    zrpc_buffer_t resp = { NULL, 0, 0 };
    int st = zrpc_client_call_unary(c, "test.add", "{\"a\":1,\"b\":1}", 14, 0, &resp);
    CHECK(st == ZRPC_STATUS_UNAUTHENTICATED);
    zrpc_client_free(c);
}

static void test_no_token(void)
{
    zrpc_client_t *c = make_client(NULL);
    zrpc_buffer_t resp = { NULL, 0, 0 };
    int st = zrpc_client_call_unary(c, "test.add", "{\"a\":1,\"b\":1}", 14, 0, &resp);
    CHECK(st == ZRPC_STATUS_UNAUTHENTICATED);
    zrpc_client_free(c);
}

static void test_conn_reuse(void)
{
    zrpc_client_t *c = make_client(SRV_TOKEN);
    int ok = 1;
    for (int i = 0; i < 50 && ok; i++) {
        char req[64];
        int n = snprintf(req, sizeof(req), "{\"a\":%d,\"b\":1}", i);
        zrpc_buffer_t resp = { NULL, 0, 0 };
        int st = zrpc_client_call_unary(c, "test.add", req, (uint32_t)n, 0, &resp);
        if (st != ZRPC_STATUS_OK || get_sum((char *)resp.data) != i + 1) ok = 0;
        zrpc_buffer_free(&resp);
    }
    CHECK(ok);
    zrpc_client_free(c);
}

/* ---- concurrency ---- */

struct worker_arg { int n; int *okp; };
static void *worker_main(void *p)
{
    struct worker_arg *a = (struct worker_arg *)p;
    zrpc_client_t *c = make_client(SRV_TOKEN);
    int ok = 1;
    for (int i = 0; i < a->n && ok; i++) {
        zrpc_buffer_t resp = { NULL, 0, 0 };
        int st = zrpc_client_call_unary(c, "test.add", "{\"a\":1,\"b\":1}", 14, 0, &resp);
        if (st != ZRPC_STATUS_OK || get_sum((char *)resp.data) != 2) ok = 0;
        zrpc_buffer_free(&resp);
    }
    zrpc_client_free(c);
    *a->okp = ok;
    return NULL;
}

static void test_concurrent(void)
{
    enum { T = 8, PER = 200 };
    pthread_t th[T];
    struct worker_arg args[T];
    int ok[T];
    for (int i = 0; i < T; i++) {
        args[i].n = PER;
        args[i].okp = &ok[i];
        pthread_create(&th[i], NULL, worker_main, &args[i]);
    }
    int all = 1;
    for (int i = 0; i < T; i++) {
        pthread_join(th[i], NULL);
        all = all && ok[i];
    }
    CHECK(all);
}

/* ---- load ---- */

static int run_load(long total, int threads)
{
    if (threads == 1) {
        zrpc_client_t *c = make_client(SRV_TOKEN);
        if (!c) return -1;
        /* warm-up */
        for (int i = 0; i < 5000; i++) {
            zrpc_buffer_t resp = { NULL, 0, 0 };
            if (zrpc_client_call_unary(c, "test.add", "{\"a\":1,\"b\":1}", 14, 0, &resp) != ZRPC_STATUS_OK) {
                zrpc_client_free(c);
                return -1;
            }
            zrpc_buffer_free(&resp);
        }
        int fd0 = fd_count();
        long rss0 = rss_kb();
        long t0 = (long)time(NULL);

        for (long i = 0; i < total - 5000; i++) {
            zrpc_buffer_t resp = { NULL, 0, 0 };
            int st = zrpc_client_call_unary(c, "test.add", "{\"a\":1,\"b\":1}", 14, 0, &resp);
            if (st != ZRPC_STATUS_OK) {
                fprintf(stderr, "load failed at %ld: %s\n", i, zrpc_client_last_error(c));
                zrpc_client_free(c);
                return -1;
            }
            zrpc_buffer_free(&resp);
        }
        long t1 = (long)time(NULL);
        int fd1 = fd_count();
        long rss1 = rss_kb();
        zrpc_client_free(c);
        printf("load: %ld calls in %lds  fds %d->%d  rss %ld->%ld KB\n",
               total, t1 - t0, fd0, fd1, rss0, rss1);
        return (fd1 <= fd0 + 4) ? 0 : -1;
    }

    /* multi-thread: reuse code above for worker accounting */
    pthread_t *th = calloc((size_t)threads, sizeof(pthread_t));
    struct worker_arg *args = calloc((size_t)threads, sizeof(struct worker_arg));
    int *ok = calloc((size_t)threads, sizeof(int));
    long per = total / threads;
    for (int i = 0; i < threads; i++) {
        args[i].n = (int)per;
        args[i].okp = &ok[i];
        pthread_create(&th[i], NULL, worker_main, &args[i]);
    }
    for (int i = 0; i < threads; i++) pthread_join(th[i], NULL);
    int all = 1;
    for (int i = 0; i < threads; i++) all = all && ok[i];
    printf("load: %ld calls over %d threads => %s\n", per * threads, threads, all ? "ok" : "FAIL");
    free(th); free(args); free(ok);
    return all ? 0 : -1;
}

int main(int argc, char **argv)
{
    zrpc_server_options_t opt;
    memset(&opt, 0, sizeof(opt));
    opt.address = "127.0.0.1:19091";
    opt.access_token = SRV_TOKEN;
    opt.io_timeout_ms = 0;
    g_srv = zrpc_server_new(&opt);
    CHECK(g_srv != NULL);

    uintptr_t handle = (uintptr_t)g_srv;
    CHECK(zrpc_server_register(g_srv, "test.add", 0, handle, add_handler) == ZRPC_STATUS_OK);
    CHECK(zrpc_server_register(g_srv, "test.add", 0, handle, add_handler) == ZRPC_STATUS_INVALID_ARGUMENT);
    CHECK(zrpc_server_serve(g_srv) == ZRPC_STATUS_OK);

    wait_server_ready();
    if (g_fail) return 1;

    if (argc >= 2 && strcmp(argv[1], "load") == 0) {
        long n = (argc >= 3) ? atol(argv[2]) : 100000;
        int threads = (argc >= 4) ? atoi(argv[3]) : 1;
        int rc = run_load(n, threads);
        zrpc_server_free(g_srv);
        return rc == 0 ? 0 : 1;
    }

    test_ping();
    test_unary_ok();
    test_unknown_method();
    test_bad_token();
    test_no_token();
    test_conn_reuse();
    test_concurrent();

    /* client closes all its connections; server conn readers exit on EOF. */
    usleep(200000);

    if (g_fail == 0) {
        printf("test_unary: all tests passed\n");
        zrpc_server_free(g_srv);
        return 0;
    }
    fprintf(stderr, "test_unary: %d check(s) failed\n", g_fail);
    zrpc_server_free(g_srv);
    return 1;
}
