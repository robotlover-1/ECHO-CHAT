/*
 * zrpc_client.c - zrpc v2 unary client (Task 2).
 *
 * Plain blocking TCP client. Because NtyCo overrides recv/send/connect only on
 * threads that run a coroutine scheduler, calls made here (from normal threads)
 * transparently fall back to libc and keep the poll-based zrpc_io behaviour.
 *
 * A zrpc_client_t holds one reusable connection; callers wanting concurrency
 * create one client per worker (the Go bridge will build a pool on top).
 */

/* Diagnostic messages are bounded via snprintf truncation (intended). */
#pragma GCC diagnostic ignored "-Wformat-truncation"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "zrpc_client.h"
#include "zrpc_json.h"

struct zrpc_client {
    char    host[256];
    uint16_t port;
    char    token[256];
    int     connect_timeout_ms;
    int     io_timeout_ms;

    int     fd;             /* current connection, -1 when closed */
    uint64_t next_id;       /* request id source */

    char    err[256];
};

static void set_err(zrpc_client_t *c, const char *fmt, ...)
{
    if (!c) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(c->err, sizeof(c->err), fmt, ap);
    va_end(ap);
}

static void client_reset(zrpc_client_t *c)
{
    if (c->fd >= 0) {
        close(c->fd);
        c->fd = -1;
    }
}

/* ---- connect with timeout (non-blocking connect + poll) ---- */

static int open_tcp(const char *host, uint16_t port, int timeout_ms, char *err, size_t err_len)
{
    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    char portstr[16];
    snprintf(portstr, sizeof(portstr), "%u", (unsigned)port);

    int gai = getaddrinfo(host, portstr, &hints, &res);
    if (gai != 0) {
        if (err) snprintf(err, err_len, "getaddrinfo(%.200s): %.150s", host, gai_strerror(gai));
        return -1;
    }

    int fd = -1;
    for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) continue;

        int fl = fcntl(fd, F_GETFL, 0);
        fcntl(fd, F_SETFL, fl | O_NONBLOCK);

        int rc = connect(fd, ai->ai_addr, ai->ai_addrlen);
        if (rc == 0) {
            goto connected;
        }
        if (rc < 0 && errno != EINPROGRESS) {
            close(fd);
            fd = -1;
            continue;
        }

        int tmo = timeout_ms > 0 ? timeout_ms : 3000;
        struct pollfd pfd = { fd, POLLOUT, 0 };
        for (;;) {
            int pr = poll(&pfd, 1, tmo);
            if (pr < 0 && errno == EINTR) continue;
            if (pr <= 0) { close(fd); fd = -1; break; }
            if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
                int soerr = 0;
                socklen_t sl = sizeof(soerr);
                getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
                if (err) snprintf(err, err_len, "connect: %s",
                                  soerr ? strerror(soerr) : "connection failed");
                close(fd);
                fd = -1;
            } else if (pfd.revents & POLLOUT) {
                int soerr = 0;
                socklen_t sl = sizeof(soerr);
                getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
                if (soerr != 0) {
                    if (err) snprintf(err, err_len, "connect: %s", strerror(soerr));
                    close(fd);
                    fd = -1;
                }
            }
            break;
        }
        if (fd < 0) continue;
connected:
        fcntl(fd, F_SETFL, fl & ~O_NONBLOCK);   /* back to blocking for zrpc_io */
        break;
    }
    freeaddrinfo(res);
    if (fd < 0 && err && err[0] == '\0')
        snprintf(err, err_len, "connect to %.200s:%u failed", host, (unsigned)port);
    return fd;
}

/* ---- public API ---- */

zrpc_client_t *zrpc_client_new(const char *host, uint16_t port, const char *token,
                               int connect_timeout_ms, int io_timeout_ms)
{
    if (!host || host[0] == '\0') return NULL;
    zrpc_client_t *c = (zrpc_client_t *)calloc(1, sizeof(*c));
    if (!c) return NULL;
    snprintf(c->host, sizeof(c->host), "%s", host);
    c->port = port;
    if (token) snprintf(c->token, sizeof(c->token), "%s", token);
    c->connect_timeout_ms = connect_timeout_ms > 0 ? connect_timeout_ms : 3000;
    c->io_timeout_ms = io_timeout_ms > 0 ? io_timeout_ms : (int)ZRPC_DEFAULT_TIMEOUT_MS;
    c->fd = -1;
    c->next_id = 1;
    c->err[0] = '\0';
    return c;
}

void zrpc_client_close(zrpc_client_t *c)
{
    if (!c) return;
    client_reset(c);
}

void zrpc_client_free(zrpc_client_t *c)
{
    if (!c) return;
    client_reset(c);
    free(c);
}

const char *zrpc_client_last_error(const zrpc_client_t *c)
{
    return c ? c->err : "null client";
}

static int ensure_conn(zrpc_client_t *c)
{
    if (c->fd >= 0) return ZRPC_STATUS_OK;
    c->fd = open_tcp(c->host, c->port, c->connect_timeout_ms, c->err, sizeof(c->err));
    if (c->fd < 0) return ZRPC_STATUS_UNAVAILABLE;
    return ZRPC_STATUS_OK;
}

int zrpc_client_ping(zrpc_client_t *c, int timeout_ms)
{
    if (!c) return ZRPC_STATUS_INVALID_ARGUMENT;
    int st = ensure_conn(c);
    if (st != ZRPC_STATUS_OK) return st;

    uint64_t rid = c->next_id++;
    zrpc_buffer_t ping = { NULL, 0, 0 };
    st = zrpc_frame_encode(ZRPC_MSG_PING, rid, NULL, 0, &ping);
    if (st != ZRPC_STATUS_OK) return st;
    st = zrpc_write_full(c->fd, ping.data, ping.len, timeout_ms > 0 ? timeout_ms : c->io_timeout_ms);
    zrpc_buffer_free(&ping);
    if (st != ZRPC_STATUS_OK) { client_reset(c); return st; }

    int budget = timeout_ms > 0 ? timeout_ms : c->io_timeout_ms;
    zrpc_frame_t f;
    st = zrpc_frame_read(c->fd, &f, budget);
    if (st != ZRPC_STATUS_OK) { client_reset(c); return st; }
    int ok = (f.type == ZRPC_MSG_PONG && f.request_id == rid);
    zrpc_frame_free(&f);
    if (!ok) { client_reset(c); return ZRPC_STATUS_PROTOCOL_ERROR; }
    return ZRPC_STATUS_OK;
}

int zrpc_client_call_unary(zrpc_client_t *c, const char *method,
                           const void *req_json, uint32_t req_len,
                           uint64_t deadline_unix_ms, zrpc_buffer_t *response)
{
    if (!c || !method || (req_json == NULL && req_len > 0) || !response)
        return ZRPC_STATUS_INVALID_ARGUMENT;
    response->data = NULL;
    response->len = 0;
    response->cap = 0;

    int st = ensure_conn(c);
    if (st != ZRPC_STATUS_OK) return st;

    /* 1. build REQUEST frame: {"method","auth":"Bearer ..","deadline","payload":<verbatim>} */
    char auth[320];
    if (c->token[0]) {
        int n = snprintf(auth, sizeof(auth), "Bearer %s", c->token);
        if (n <= 0) return ZRPC_STATUS_INVALID_ARGUMENT;
    } else {
        auth[0] = '\0';
    }

    zrpc_buffer_t env = { NULL, 0, 0 };
    st = zrpc_json_build_request(method, auth, (int64_t)deadline_unix_ms,
                                 req_json, req_len, &env);
    if (st != ZRPC_STATUS_OK) return st;

    uint64_t rid = c->next_id++;
    zrpc_buffer_t frame = { NULL, 0, 0 };
    st = zrpc_frame_encode(ZRPC_MSG_REQUEST, rid, env.data, env.len, &frame);
    zrpc_buffer_free(&env);
    if (st != ZRPC_STATUS_OK) return st;

    /* 2. write + read reply on the reusable connection */
    st = zrpc_write_full(c->fd, frame.data, frame.len, c->io_timeout_ms);
    zrpc_buffer_free(&frame);
    if (st != ZRPC_STATUS_OK) { client_reset(c); set_err(c, "write: %s", zrpc_status_str(st)); return st; }

    zrpc_frame_t reply;
    st = zrpc_frame_read(c->fd, &reply, c->io_timeout_ms);
    if (st != ZRPC_STATUS_OK) { client_reset(c); set_err(c, "read: %s", zrpc_status_str(st)); return st; }

    int result = ZRPC_STATUS_INTERNAL;
    if (reply.type == ZRPC_MSG_RESPONSE) {
        if (reply.request_id != rid) {
            result = ZRPC_STATUS_PROTOCOL_ERROR;
            set_err(c, "response request_id mismatch");
        } else if (reply.payload) {
            result = zrpc_json_unwrap_payload(reply.payload, reply.length, response);
        } else {
            result = ZRPC_STATUS_PROTOCOL_ERROR;
        }
    } else if (reply.type == ZRPC_MSG_ERROR) {
        int code = ZRPC_STATUS_INTERNAL;
        int retryable = 0;
        char *msg = NULL;
        if (zrpc_json_parse_error(reply.payload, reply.length, &code, &msg, &retryable) == ZRPC_STATUS_OK) {
            result = code;
            if (msg) { set_err(c, "%s", msg); free(msg); }
        } else {
            result = ZRPC_STATUS_PROTOCOL_ERROR;
        }
    } else {
        result = ZRPC_STATUS_PROTOCOL_ERROR;
        set_err(c, "unexpected reply frame type %d", reply.type);
    }
    zrpc_frame_free(&reply);

    if (result == ZRPC_STATUS_UNAVAILABLE) client_reset(c);
    return result;
}
