/*
 * zrpc_server.c - zrpc v2 unary server (Task 2) running on NtyCo.
 *
 * Layout:
 *   - zrpc_server_serve() binds/listens on the caller thread, then starts a
 *     dedicated NtyCo scheduler thread.
 *   - An accept coroutine (server_main) accepts connections and spawns one
 *     conn_reader coroutine per connection.
 *   - conn_reader reads frames with coroutine-aware zrpc_io (recv/send yield on
 *     the scheduler), answers PING directly, authenticates, looks up the method
 *     and invokes the registered C callback for REQUEST frames.
 *   - zrpc_server_send_*() serialise writes per connection with a mutex and may
 *     be called from any thread (a cgo handler replies from a Go thread).
 *
 * Known limits (tracked for Task 5/8): no idle timeouts inside coroutines and
 * no clean cross-thread wake-up of the scheduler on shutdown yet.
 */

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include "nty_coroutine.h"
#include "zrpc_json.h"
#include "zrpc_server.h"

#define ZRPC_DEFAULT_MAX_CONN 1024

typedef struct zrpc_conn {
    struct zrpc_conn *next;
    struct zrpc_server *server;      /* back-pointer for dispatch */
    int      fd;
    pthread_mutex_t wlock;           /* serialise writers */
} zrpc_conn_t;

typedef struct zrpc_method {
    struct zrpc_method *next;
    char   *name;
    int     is_stream;
    uint64_t handler_handle;
    zrpc_request_callback_t cb;
} zrpc_method_t;

struct zrpc_server {
    zrpc_server_options_t opt;
    char    address[256];
    char   *access_token;

    int     listen_fd;
    int     max_connections;
    volatile int stopping;
    int     serve_rc;
    pthread_t sched_thread;
    int     sched_thread_started;

    pthread_mutex_t conn_lock;
    zrpc_conn_t *conns;

    pthread_mutex_t method_lock;
    zrpc_method_t *methods;

    char    err[256];
};

/* ---- helpers ---- */

static int ct_eq(const char *a, size_t alen, const char *b, size_t blen)
{
    size_t n = alen < blen ? alen : blen;
    volatile uint8_t acc = 0;
    for (size_t i = 0; i < n; i++) acc |= (uint8_t)(a[i] ^ b[i]);
    acc |= (uint8_t)(alen ^ blen);
    return acc == 0;
}

static int64_t unix_ms_now(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static void server_set_err(zrpc_server_t *s, const char *fmt, ...)
{
    if (!s) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s->err, sizeof(s->err), fmt, ap);
    va_end(ap);
}

/* ---- conn registry ---- */

static zrpc_conn_t *conn_find_locked(zrpc_server_t *s, int fd)
{
    for (zrpc_conn_t *c = s->conns; c; c = c->next)
        if (c->fd == fd) return c;
    return NULL;
}

/* Find conn under lock, take its write mutex, drop registry lock. */
static zrpc_conn_t *conn_lock_for_write(zrpc_server_t *s, int fd)
{
    pthread_mutex_lock(&s->conn_lock);
    zrpc_conn_t *c = conn_find_locked(s, fd);
    if (c) pthread_mutex_lock(&c->wlock);
    pthread_mutex_unlock(&s->conn_lock);
    return c;
}

static void conn_add(zrpc_server_t *s, zrpc_conn_t *c)
{
    pthread_mutex_lock(&s->conn_lock);
    c->next = s->conns;
    s->conns = c;
    pthread_mutex_unlock(&s->conn_lock);
}

static void conn_unlink_free(zrpc_server_t *s, int fd)
{
    pthread_mutex_lock(&s->conn_lock);
    zrpc_conn_t **pp = &s->conns;
    while (*pp) {
        if ((*pp)->fd == fd) {
            zrpc_conn_t *gone = *pp;
            *pp = gone->next;
            pthread_mutex_unlock(&s->conn_lock);
            pthread_mutex_destroy(&gone->wlock);
            free(gone);
            return;
        }
        pp = &(*pp)->next;
    }
    pthread_mutex_unlock(&s->conn_lock);
}

/* ---- public reply helpers (thread-safe) ---- */

static int send_frame(zrpc_server_t *s, int client_fd, zrpc_buffer_t *frame)
{
    zrpc_conn_t *c = conn_lock_for_write(s, client_fd);
    if (!c) return ZRPC_STATUS_UNAVAILABLE;
    int st = zrpc_write_full(c->fd, frame->data, frame->len, 0);
    pthread_mutex_unlock(&c->wlock);
    return st;
}

int zrpc_server_send_response(zrpc_server_t *s, int client_fd, uint64_t request_id,
                              const void *resp_json, uint32_t resp_len)
{
    if (!s) return ZRPC_STATUS_INVALID_ARGUMENT;
    zrpc_buffer_t wrapped = { NULL, 0, 0 };
    int st = zrpc_json_wrap_payload(resp_json, resp_len, &wrapped);
    if (st != ZRPC_STATUS_OK) return st;

    zrpc_buffer_t frame = { NULL, 0, 0 };
    st = zrpc_frame_encode(ZRPC_MSG_RESPONSE, request_id, wrapped.data, wrapped.len, &frame);
    zrpc_buffer_free(&wrapped);
    if (st != ZRPC_STATUS_OK) return st;

    st = send_frame(s, client_fd, &frame);
    zrpc_buffer_free(&frame);
    return st;
}

int zrpc_server_send_error(zrpc_server_t *s, int client_fd, uint64_t request_id,
                           int code, const char *message)
{
    if (!s) return ZRPC_STATUS_INVALID_ARGUMENT;
    zrpc_buffer_t errp = { NULL, 0, 0 };
    int st = zrpc_json_build_error(code, message ? message : zrpc_status_str(code), 0, &errp);
    if (st != ZRPC_STATUS_OK) return st;

    zrpc_buffer_t frame = { NULL, 0, 0 };
    st = zrpc_frame_encode(ZRPC_MSG_ERROR, request_id, errp.data, errp.len, &frame);
    zrpc_buffer_free(&errp);
    if (st != ZRPC_STATUS_OK) return st;

    st = send_frame(s, client_fd, &frame);
    zrpc_buffer_free(&frame);
    return st;
}

/* ---- auth + dispatch (runs inside the conn_reader coroutine) ---- */

static int authenticate(zrpc_server_t *s, const char *auth)
{
    if (!s->access_token) return 1;                 /* auth disabled */
    if (!auth || auth[0] == '\0') return 0;
    const char *prefix = "Bearer ";
    size_t plen = strlen(prefix);
    if (strncmp(auth, prefix, plen) != 0)
        return ct_eq(auth, strlen(auth), s->access_token, strlen(s->access_token));
    return ct_eq(auth + plen, strlen(auth) - plen,
                 s->access_token, strlen(s->access_token));
}

static void handle_request(zrpc_server_t *s, zrpc_conn_t *conn, zrpc_frame_t *f)
{
    zrpc_json_envelope_t env;
    int st = zrpc_json_parse_envelope(f->payload, f->length, &env);
    if (st != ZRPC_STATUS_OK) {
        zrpc_server_send_error(s, conn->fd, f->request_id,
                               ZRPC_STATUS_PROTOCOL_ERROR, "bad request envelope");
        return;
    }

    if (!authenticate(s, env.auth)) {
        zrpc_server_send_error(s, conn->fd, f->request_id, ZRPC_STATUS_UNAUTHENTICATED, NULL);
        zrpc_json_envelope_free(&env);
        return;
    }

    if (env.deadline_unix_ms > 0 && unix_ms_now() > env.deadline_unix_ms) {
        zrpc_server_send_error(s, conn->fd, f->request_id, ZRPC_STATUS_DEADLINE_EXCEEDED, NULL);
        zrpc_json_envelope_free(&env);
        return;
    }

    zrpc_request_callback_t cb = NULL;
    uint64_t handle = 0;
    pthread_mutex_lock(&s->method_lock);
    zrpc_method_t *m = s->methods;
    while (m && strcmp(m->name, env.method) != 0) m = m->next;
    if (m) { cb = m->cb; handle = m->handler_handle; }
    pthread_mutex_unlock(&s->method_lock);

    if (!cb) {
        zrpc_server_send_error(s, conn->fd, f->request_id, ZRPC_STATUS_NOT_FOUND, env.method);
        zrpc_json_envelope_free(&env);
        return;
    }

    int rc = cb(handle, f->request_id, conn->fd,
                env.payload, (uint32_t)env.payload_len, (uint64_t)env.deadline_unix_ms);
    if (rc != 0)
        zrpc_server_send_error(s, conn->fd, f->request_id, ZRPC_STATUS_INTERNAL, "handler failure");
    zrpc_json_envelope_free(&env);
}

/* ---- per-connection coroutine ---- */

static void conn_reader(void *arg)
{
    zrpc_conn_t *conn = (zrpc_conn_t *)arg;
    zrpc_server_t *s = conn->server;

    while (!s->stopping) {
        zrpc_frame_t f;
        int st = zrpc_frame_read(conn->fd, &f, 0);   /* coroutine-aware; no timeout */
        if (st != ZRPC_STATUS_OK)
            break;

        switch (f.type) {
        case ZRPC_MSG_PING:
            /* PING never enters a business handler. */
            {
                zrpc_buffer_t pong = { NULL, 0, 0 };
                if (zrpc_frame_encode(ZRPC_MSG_PONG, f.request_id, NULL, 0, &pong) == ZRPC_STATUS_OK) {
                    zrpc_conn_t *c = conn_lock_for_write(s, conn->fd);
                    if (c) {
                        zrpc_write_full(c->fd, pong.data, pong.len, 0);
                        pthread_mutex_unlock(&c->wlock);
                    }
                    zrpc_buffer_free(&pong);
                }
            }
            zrpc_frame_free(&f);
            continue;

        case ZRPC_MSG_REQUEST:
            handle_request(s, conn, &f);
            zrpc_frame_free(&f);
            continue;

        default:
            /* A unary client never sends RESPONSE, STREAM_*, ERROR or CANCEL
             * in v1; treat anything unexpected as a protocol error. */
            zrpc_server_send_error(s, conn->fd, f.request_id,
                                   ZRPC_STATUS_PROTOCOL_ERROR, "unexpected frame type");
            zrpc_frame_free(&f);
            break;
        }
        break;   /* protocol error: drop the connection */
    }

    /* tear down */
    close(conn->fd);
    conn_unlink_free(s, conn->fd);
}

/* ---- accept coroutine ---- */

static void server_main(void *arg)
{
    zrpc_server_t *s = (zrpc_server_t *)arg;

    while (!s->stopping) {
        struct sockaddr_in remote;
        socklen_t rlen = sizeof(remote);
        int cfd = accept(s->listen_fd, (struct sockaddr *)&remote, &rlen);
        if (cfd < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
            server_set_err(s, "accept: %s", strerror(errno));
            break;
        }
        int fl = fcntl(cfd, F_GETFL, 0);
        fcntl(cfd, F_SETFL, fl | O_NONBLOCK);

        zrpc_conn_t *conn = (zrpc_conn_t *)calloc(1, sizeof(*conn));
        if (!conn) { close(cfd); continue; }
        conn->server = s;
        conn->fd = cfd;
        pthread_mutex_init(&conn->wlock, NULL);
        conn_add(s, conn);

        nty_coroutine *co = NULL;
        nty_coroutine_create(&co, conn_reader, conn);
    }

    close(s->listen_fd);
    s->listen_fd = -1;
}

/* ---- scheduler thread ---- */

static void *scheduler_thread_main(void *arg)
{
    zrpc_server_t *s = (zrpc_server_t *)arg;
    nty_coroutine *co = NULL;
    nty_coroutine_create(&co, server_main, s);
    nty_schedule_run();                     /* never returns while coroutines live */
    return NULL;
}

/* ---- public lifecycle ---- */

zrpc_server_t *zrpc_server_new(const zrpc_server_options_t *opt)
{
    if (!opt || !opt->address || opt->address[0] == '\0') return NULL;
    zrpc_server_t *s = (zrpc_server_t *)calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->opt = *opt;
    snprintf(s->address, sizeof(s->address), "%s", opt->address);
    if (opt->access_token) s->access_token = strdup(opt->access_token);
    s->listen_fd = -1;
    s->max_connections = opt->max_connections > 0 ? opt->max_connections : ZRPC_DEFAULT_MAX_CONN;
    pthread_mutex_init(&s->conn_lock, NULL);
    pthread_mutex_init(&s->method_lock, NULL);
    return s;
}

int zrpc_server_register(zrpc_server_t *s, const char *method, int is_stream,
                         uint64_t handler_handle, zrpc_request_callback_t cb)
{
    if (!s || !method || !cb) return ZRPC_STATUS_INVALID_ARGUMENT;

    pthread_mutex_lock(&s->method_lock);
    for (zrpc_method_t *m = s->methods; m; m = m->next) {
        if (strcmp(m->name, method) == 0) {
            pthread_mutex_unlock(&s->method_lock);
            return ZRPC_STATUS_INVALID_ARGUMENT;   /* duplicate */
        }
    }
    zrpc_method_t *m = (zrpc_method_t *)calloc(1, sizeof(*m));
    if (!m) { pthread_mutex_unlock(&s->method_lock); return ZRPC_STATUS_RESOURCE_EXHAUSTED; }
    m->name = strdup(method);
    m->is_stream = is_stream;
    m->handler_handle = handler_handle;
    m->cb = cb;
    m->next = s->methods;
    s->methods = m;
    pthread_mutex_unlock(&s->method_lock);
    return ZRPC_STATUS_OK;
}

int zrpc_server_serve(zrpc_server_t *s)
{
    if (!s) return ZRPC_STATUS_INVALID_ARGUMENT;
    if (s->sched_thread_started) return ZRPC_STATUS_INTERNAL;

    /* parse host:port */
    char host[256];
    char portstr[16];
    const char *colon = strrchr(s->address, ':');
    if (!colon) return ZRPC_STATUS_INVALID_ARGUMENT;
    size_t hlen = (size_t)(colon - s->address);
    if (hlen == 0 || hlen >= sizeof(host)) return ZRPC_STATUS_INVALID_ARGUMENT;
    memcpy(host, s->address, hlen);
    host[hlen] = '\0';
    snprintf(portstr, sizeof(portstr), "%s", colon + 1);

    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;           /* v1: IPv4 listener */
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_PASSIVE;
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) {
        if (res) freeaddrinfo(res);
        return ZRPC_STATUS_INVALID_ARGUMENT;
    }

    int lfd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (lfd < 0) { freeaddrinfo(res); return ZRPC_STATUS_INTERNAL; }
    int yes = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    if (bind(lfd, res->ai_addr, res->ai_addrlen) != 0) {
        server_set_err(s, "bind %s: %s", s->address, strerror(errno));
        close(lfd);
        freeaddrinfo(res);
        return ZRPC_STATUS_UNAVAILABLE;
    }
    int backlog = s->opt.backlog > 0 ? s->opt.backlog : SOMAXCONN;
    if (listen(lfd, backlog) != 0) {
        server_set_err(s, "listen: %s", strerror(errno));
        close(lfd);
        freeaddrinfo(res);
        return ZRPC_STATUS_UNAVAILABLE;
    }
    freeaddrinfo(res);

    s->listen_fd = lfd;
    s->stopping = 0;

    if (pthread_create(&s->sched_thread, NULL, scheduler_thread_main, s) != 0) {
        close(lfd);
        s->listen_fd = -1;
        return ZRPC_STATUS_INTERNAL;
    }
    s->sched_thread_started = 1;
    return ZRPC_STATUS_OK;
}

int zrpc_server_shutdown(zrpc_server_t *s)
{
    if (!s) return ZRPC_STATUS_INVALID_ARGUMENT;
    s->stopping = 1;
    if (s->listen_fd >= 0) {
        close(s->listen_fd);           /* accept coroutine may not wake instantly */
        s->listen_fd = -1;
    }
    return ZRPC_STATUS_OK;
}

void zrpc_server_free(zrpc_server_t *s)
{
    if (!s) return;
    zrpc_server_shutdown(s);

    pthread_mutex_lock(&s->method_lock);
    zrpc_method_t *m = s->methods;
    while (m) {
        zrpc_method_t *nx = m->next;
        free(m->name);
        free(m);
        m = nx;
    }
    s->methods = NULL;
    pthread_mutex_unlock(&s->method_lock);

    /* conn structs owned by their coroutines; not force-freed here. */
    free(s->access_token);
    pthread_mutex_destroy(&s->conn_lock);
    pthread_mutex_destroy(&s->method_lock);
    free(s);
}
