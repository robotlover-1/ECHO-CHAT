/*
 * zrpc_io.c - safe full read/write helpers for stream sockets.
 *
 * Guarantees (see zrpc_protocol.h):
 *   - EINTR is retried, EAGAIN/EWOULDBLOCK wait on poll.
 *   - An orderly close (recv -> 0) or reset mid-transfer maps to UNAVAILABLE.
 *   - Timeout maps to DEADLINE_EXCEEDED; timeout_ms == 0 blocks indefinitely.
 *   - write_full uses MSG_NOSIGNAL so a reset peer never kills the process.
 *   - No socket input path may assert.
 *
 * The "_until" variants share one CLOCK_MONOTONIC deadline across the header
 * and payload phases of a frame, so the whole frame obeys a single budget.
 */

#define _GNU_SOURCE   /* MSG_NOSIGNAL */

#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <sys/socket.h>
#include <time.h>

#include "zrpc_protocol.h"

static int64_t now_mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

int64_t zrpc_now_mono_ms(void)
{
    return now_mono_ns() / 1000000LL;
}

/*
 * Wait for the given poll events on fd until the deadline.
 * Returns 0 on readiness, DEADLINE_EXCEEDED on timeout, UNAVAILABLE when the
 * peer is gone or the fd is bad, INTERNAL on other poll() failures.
 */
static int wait_ready(int fd, short events, int64_t deadline_ms)
{
    for (;;) {
        int timeout_ms = -1;  /* infinite */
        if (deadline_ms != ZRPC_DEADLINE_NONE) {
            int64_t remain = deadline_ms - zrpc_now_mono_ms();
            if (remain <= 0)
                return ZRPC_STATUS_DEADLINE_EXCEEDED;
            timeout_ms = (remain > INT32_MAX) ? INT32_MAX : (int)remain;
        }

        struct pollfd pfd;
        pfd.fd = fd;
        pfd.events = (short)events;
        pfd.revents = 0;

        int rc = poll(&pfd, 1, timeout_ms);
        if (rc < 0) {
            if (errno == EINTR)
                continue;
            if (errno == EINVAL)
                return ZRPC_STATUS_INVALID_ARGUMENT;
            return ZRPC_STATUS_INTERNAL;
        }
        if (rc == 0)
            return ZRPC_STATUS_DEADLINE_EXCEEDED;

        short rev = pfd.revents;
        if (rev & (POLLERR | POLLNVAL))
            return ZRPC_STATUS_UNAVAILABLE;
        if (rev & events)
            return ZRPC_STATUS_OK;       /* readiness first: POLLHUP may pair with POLLIN (buffered data then EOF) */
        if (rev & POLLHUP)
            return ZRPC_STATUS_UNAVAILABLE;  /* no reader side / gone, nothing more to transfer */
    }
}

static int map_recv_error(int err)
{
    switch (err) {
    case EINTR:
        return ZRPC_STATUS_OK;      /* caller decides; handled by caller retry */
    case EAGAIN:                    /* EWOULDBLOCK == EAGAIN on Linux */
        return ZRPC_STATUS_OK;      /* retry after polling */
    case ECONNRESET:
    case ENOTCONN:
    case EPIPE:
        return ZRPC_STATUS_UNAVAILABLE;
    default:
        return ZRPC_STATUS_INTERNAL;
    }
}

int zrpc_read_full_until(int fd, void *buf, size_t len, int64_t deadline_ms)
{
    if (len == 0)
        return ZRPC_STATUS_OK;
    if (buf == NULL)
        return ZRPC_STATUS_INVALID_ARGUMENT;

    uint8_t *p = (uint8_t *)buf;
    size_t off = 0;

    while (off < len) {
        int st = wait_ready(fd, POLLIN, deadline_ms);
        if (st != ZRPC_STATUS_OK)
            return st;

        ssize_t n = recv(fd, p + off, len - off, 0);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;   /* repoll with the remaining budget */
            return map_recv_error(errno);
        }
        if (n == 0)
            return ZRPC_STATUS_UNAVAILABLE;  /* orderly close mid-read */
        off += (size_t)n;
    }
    return ZRPC_STATUS_OK;
}

int zrpc_read_full(int fd, void *buf, size_t len, int timeout_ms)
{
    int64_t deadline = ZRPC_DEADLINE_NONE;
    if (timeout_ms > 0)
        deadline = zrpc_now_mono_ms() + timeout_ms;
    return zrpc_read_full_until(fd, buf, len, deadline);
}

static int map_send_error(int err)
{
    switch (err) {
    case EINTR:
    case EAGAIN:                    /* EWOULDBLOCK == EAGAIN on Linux */
        return ZRPC_STATUS_OK;      /* retry after polling */
    case EPIPE:
    case ECONNRESET:
    case ENOTCONN:
        return ZRPC_STATUS_UNAVAILABLE;
    default:
        return ZRPC_STATUS_INTERNAL;
    }
}

int zrpc_write_full_until(int fd, const void *buf, size_t len, int64_t deadline_ms)
{
    if (len == 0)
        return ZRPC_STATUS_OK;
    if (buf == NULL)
        return ZRPC_STATUS_INVALID_ARGUMENT;

    const uint8_t *p = (const uint8_t *)buf;
    size_t off = 0;

    while (off < len) {
        int st = wait_ready(fd, POLLOUT, deadline_ms);
        if (st != ZRPC_STATUS_OK)
            return st;

        ssize_t n = send(fd, p + off, len - off, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;   /* repoll with the remaining budget */
            return map_send_error(errno);
        }
        if (n == 0)
            return ZRPC_STATUS_UNAVAILABLE;  /* zero-byte write == connection bad */
        off += (size_t)n;
    }
    return ZRPC_STATUS_OK;
}

int zrpc_write_full(int fd, const void *buf, size_t len, int timeout_ms)
{
    int64_t deadline = ZRPC_DEADLINE_NONE;
    if (timeout_ms > 0)
        deadline = zrpc_now_mono_ms() + timeout_ms;
    return zrpc_write_full_until(fd, buf, len, deadline);
}
