/*
 * test_io.c - network-level tests for the zrpc v2 safe-IO + frame-read path.
 *
 * Drives real AF_UNIX stream sockets (socketpair) to exercise the plan's fault
 * cases: 1-byte splits, sticky/coalesced frames, header/body splits, timeouts,
 * zero-length payloads, over-max length, corrupt CRC, bad magic, partial-input
 * peer close and write-to-closed-peer. Slow-producer threads are used where a
 * deterministic interleaving needs a second actor.
 */

#define _GNU_SOURCE

#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#include "zrpc.h"

static int g_fail = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            g_fail++;                                                   \
        }                                                               \
    } while (0)

/* ---- helpers ---- */

static void put_u16_be(uint8_t *b, uint16_t v) { b[0] = (uint8_t)(v >> 8); b[1] = (uint8_t)v; }
static void put_u32_be(uint8_t *b, uint32_t v)
{
    b[0] = (uint8_t)(v >> 24); b[1] = (uint8_t)(v >> 16);
    b[2] = (uint8_t)(v >> 8);  b[3] = (uint8_t)v;
}
static void put_u64_be(uint8_t *b, uint64_t v)
{
    for (int i = 0; i < 8; i++) b[i] = (uint8_t)(v >> (56 - 8 * i));
}

static void make_pair(int fds[2])
{
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) {
        perror("socketpair");
        exit(2);
    }
}

/* drain bytes from fd until len bytes read; returns count read. */
static size_t drain(int fd, uint8_t *buf, size_t len)
{
    size_t off = 0;
    while (off < len) {
        ssize_t n = recv(fd, buf + off, len - off, 0);
        if (n <= 0) break;
        off += (size_t)n;
    }
    return off;
}

/* ---- read_full ---- */

struct slow_writer_arg { int fd; const uint8_t *data; size_t len; };
static void *slow_byte_writer(void *p)
{
    struct slow_writer_arg *a = (struct slow_writer_arg *)p;
    for (size_t i = 0; i < a->len; i++) {
        if (write(a->fd, &a->data[i], 1) != 1) break;   /* byte-by-byte */
        usleep(1000);
    }
    shutdown(a->fd, SHUT_WR);
    return NULL;
}

static void test_read_full_byte_at_a_time(void)
{
    int fds[2];
    make_pair(fds);
    uint8_t data[8] = { 0x10, 0x32, 0x54, 0x76, 0x98, 0xBA, 0xDC, 0xFE };

    struct slow_writer_arg arg = { fds[1], data, sizeof(data) };
    pthread_t th;
    pthread_create(&th, NULL, slow_byte_writer, &arg);

    uint8_t out[sizeof(data)];
    CHECK(zrpc_read_full(fds[0], out, sizeof(out), 5000) == ZRPC_STATUS_OK);
    CHECK(memcmp(out, data, sizeof(data)) == 0);

    pthread_join(th, NULL);
    close(fds[0]);
    close(fds[1]);
}

static void test_read_full_timeout(void)
{
    int fds[2];
    make_pair(fds);
    uint8_t b;
    CHECK(zrpc_read_full(fds[0], &b, 1, 60) == ZRPC_STATUS_DEADLINE_EXCEEDED);
    close(fds[0]);
    close(fds[1]);
}

static void test_read_full_eof_mid(void)
{
    int fds[2];
    make_pair(fds);
    uint8_t data[3] = { 1, 2, 3 };
    CHECK(write(fds[1], data, 3) == 3);
    shutdown(fds[1], SHUT_WR);

    uint8_t out[8];
    CHECK(zrpc_read_full(fds[0], out, sizeof(out), 1000) == ZRPC_STATUS_UNAVAILABLE);
    CHECK(out[0] == 1 && out[2] == 3);   /* partial data preserved before EOF */
    close(fds[0]);
    close(fds[1]);
}

static void test_read_full_invalid(void)
{
    CHECK(zrpc_read_full(-1, NULL, 5, 0) == ZRPC_STATUS_INVALID_ARGUMENT);
}

/* ---- write_full ---- */

static void test_write_full_echo(void)
{
    int fds[2];
    make_pair(fds);
    uint8_t data[1024];
    for (size_t i = 0; i < sizeof(data); i++) data[i] = (uint8_t)(i * 31);

    CHECK(zrpc_write_full(fds[0], data, sizeof(data), 5000) == ZRPC_STATUS_OK);

    uint8_t out[sizeof(data)];
    CHECK(drain(fds[1], out, sizeof(out)) == sizeof(data));
    CHECK(memcmp(out, data, sizeof(data)) == 0);
    close(fds[0]);
    close(fds[1]);
}

static void test_write_full_to_closed_peer(void)
{
    int fds[2];
    make_pair(fds);
    uint8_t data[64] = { 0 };
    close(fds[1]);                      /* peer gone */
    CHECK(zrpc_write_full(fds[0], data, sizeof(data), 1000) == ZRPC_STATUS_UNAVAILABLE);
    close(fds[0]);
}

struct slow_drain_arg { int fd; uint8_t *buf; size_t len; };
static void *slow_drainer(void *p)
{
    struct slow_drain_arg *a = (struct slow_drain_arg *)p;
    size_t off = 0;
    while (off < a->len) {
        ssize_t n = recv(a->fd, a->buf + off, a->len - off, 0);
        if (n <= 0) break;
        off += (size_t)n;
        usleep(100);                    /* keep sender's buffer filling up */
    }
    return NULL;
}

/* Non-blocking sender + slow draining reader forces EAGAIN / poll(POLLOUT). */
static void test_write_full_eagain_path(void)
{
    int fds[2];
    make_pair(fds);

    size_t total = 512 * 1024;
    uint8_t *data = (uint8_t *)malloc(total);
    for (size_t i = 0; i < total; i++) data[i] = (uint8_t)(i % 251);
    int flags = fcntl(fds[0], F_GETFL, 0);
    fcntl(fds[0], F_SETFL, flags | O_NONBLOCK);

    uint8_t *got = (uint8_t *)malloc(total);
    struct slow_drain_arg arg = { fds[1], got, total };
    pthread_t th;
    pthread_create(&th, NULL, slow_drainer, &arg);

    CHECK(zrpc_write_full(fds[0], data, total, 8000) == ZRPC_STATUS_OK);
    pthread_join(th, NULL);
    CHECK(memcmp(got, data, total) == 0);

    free(got);
    free(data);
    close(fds[0]);
    close(fds[1]);
}

/* ---- frame over a real socket ---- */

static void test_frame_roundtrip_over_socket(void)
{
    int fds[2];
    make_pair(fds);
    const char *payload = "{\"method\":\"chat.completion\"}";

    zrpc_buffer_t buf;
    CHECK(zrpc_frame_encode(ZRPC_MSG_REQUEST, 42, payload, (uint32_t)strlen(payload), &buf)
          == ZRPC_STATUS_OK);
    CHECK(zrpc_write_full(fds[0], buf.data, buf.len, 3000) == ZRPC_STATUS_OK);

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[1], &f, 3000) == ZRPC_STATUS_OK);
    CHECK(f.type == ZRPC_MSG_REQUEST && f.request_id == 42);
    CHECK(f.length == strlen(payload) && memcmp(f.payload, payload, strlen(payload)) == 0);
    zrpc_frame_free(&f);
    zrpc_buffer_free(&buf);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_sticky_two_frames(void)
{
    int fds[2];
    make_pair(fds);
    const char *a = "first-frame-payload";
    const char *b = "second-frame-payload";
    zrpc_buffer_t ba, bb;
    zrpc_frame_encode(ZRPC_MSG_STREAM_DATA, 1, a, (uint32_t)strlen(a), &ba);
    zrpc_frame_encode(ZRPC_MSG_STREAM_DATA, 2, b, (uint32_t)strlen(b), &bb);

    /* two frames coalesced into one write */
    uint8_t *joined = (uint8_t *)malloc(ba.len + bb.len);
    memcpy(joined, ba.data, ba.len);
    memcpy(joined + ba.len, bb.data, bb.len);
    CHECK(zrpc_write_full(fds[0], joined, ba.len + bb.len, 3000) == ZRPC_STATUS_OK);

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[1], &f, 3000) == ZRPC_STATUS_OK);
    CHECK(f.request_id == 1 && f.length == strlen(a)
          && memcmp(f.payload, a, strlen(a)) == 0);
    zrpc_frame_free(&f);
    CHECK(zrpc_frame_read(fds[1], &f, 3000) == ZRPC_STATUS_OK);
    CHECK(f.request_id == 2 && f.length == strlen(b)
          && memcmp(f.payload, b, strlen(b)) == 0);
    zrpc_frame_free(&f);

    free(joined);
    zrpc_buffer_free(&ba);
    zrpc_buffer_free(&bb);
    close(fds[0]);
    close(fds[1]);
}

struct split_writer_arg { int fd; const uint8_t *data; size_t len; };
static void *split_frame_writer(void *p)
{
    struct split_writer_arg *a = (struct split_writer_arg *)p;
    if (write(a->fd, a->data, ZRPC_HEADER_SIZE) != (ssize_t)ZRPC_HEADER_SIZE) {
        /* ignored */;
    }
    usleep(20000);                      /* header arrives well before body */
    if (a->len > ZRPC_HEADER_SIZE) {
        const uint8_t *body = a->data + ZRPC_HEADER_SIZE;
        (void)!write(a->fd, body, a->len - ZRPC_HEADER_SIZE);
    }
    shutdown(a->fd, SHUT_WR);
    return NULL;
}

static void test_frame_split_header_then_body(void)
{
    int fds[2];
    make_pair(fds);
    const char *payload = "header and body split in time";
    zrpc_buffer_t buf;
    zrpc_frame_encode(ZRPC_MSG_STREAM_DATA, 7, payload, (uint32_t)strlen(payload), &buf);

    struct split_writer_arg arg = { fds[1], buf.data, buf.len };
    pthread_t th;
    pthread_create(&th, NULL, split_frame_writer, &arg);

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[0], &f, 3000) == ZRPC_STATUS_OK);
    CHECK(f.request_id == 7 && f.length == strlen(payload)
          && memcmp(f.payload, payload, strlen(payload)) == 0);
    zrpc_frame_free(&f);

    pthread_join(th, NULL);
    zrpc_buffer_free(&buf);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_zero_length_ping(void)
{
    int fds[2];
    make_pair(fds);
    zrpc_buffer_t buf;
    zrpc_frame_encode(ZRPC_MSG_PING, 5, NULL, 0, &buf);
    CHECK(zrpc_write_full(fds[0], buf.data, buf.len, 3000) == ZRPC_STATUS_OK);

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[1], &f, 3000) == ZRPC_STATUS_OK);
    CHECK(f.type == ZRPC_MSG_PING && f.request_id == 5 && f.length == 0 && f.payload == NULL);
    zrpc_frame_free(&f);
    zrpc_buffer_free(&buf);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_timeout(void)
{
    int fds[2];
    make_pair(fds);
    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[0], &f, 60) == ZRPC_STATUS_DEADLINE_EXCEEDED);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_partial_header_then_close(void)
{
    int fds[2];
    make_pair(fds);
    uint8_t part[10] = { 0 };
    CHECK(write(fds[1], part, sizeof(part)) == (ssize_t)sizeof(part));
    shutdown(fds[1], SHUT_WR);

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[0], &f, 1000) == ZRPC_STATUS_UNAVAILABLE);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_peer_closed_empty(void)
{
    int fds[2];
    make_pair(fds);
    shutdown(fds[1], SHUT_WR);          /* clean close, no bytes */

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[0], &f, 1000) == ZRPC_STATUS_UNAVAILABLE);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_over_max_rejected_before_body(void)
{
    int fds[2];
    make_pair(fds);

    uint8_t hdr[ZRPC_HEADER_SIZE];
    memset(hdr, 0, sizeof(hdr));
    put_u16_be(hdr, ZRPC_MAGIC);
    hdr[2] = ZRPC_VERSION;
    hdr[3] = ZRPC_MSG_REQUEST;
    put_u64_be(hdr + 4, 1);
    put_u32_be(hdr + 12, ZRPC_MAX_FRAME_SIZE + 1);   /* lie about the size */
    put_u32_be(hdr + 16, 0);                          /* crc never checked */

    CHECK(write(fds[1], hdr, sizeof(hdr)) == (ssize_t)sizeof(hdr));

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[0], &f, 1000) == ZRPC_STATUS_FRAME_TOO_LARGE);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_bad_magic_over_socket(void)
{
    int fds[2];
    make_pair(fds);
    uint8_t hdr[ZRPC_HEADER_SIZE];
    memset(hdr, 0, sizeof(hdr));
    put_u16_be(hdr, 0x4242);            /* bad magic */
    hdr[2] = ZRPC_VERSION;
    hdr[3] = ZRPC_MSG_REQUEST;
    CHECK(write(fds[1], hdr, sizeof(hdr)) == (ssize_t)sizeof(hdr));

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[0], &f, 1000) == ZRPC_STATUS_PROTOCOL_ERROR);
    close(fds[0]);
    close(fds[1]);
}

static void test_frame_bad_crc_over_socket(void)
{
    int fds[2];
    make_pair(fds);
    const char *payload = "tampered payload";
    zrpc_buffer_t buf;
    zrpc_frame_encode(ZRPC_MSG_RESPONSE, 3, payload, (uint32_t)strlen(payload), &buf);
    buf.data[ZRPC_HEADER_SIZE + 2] ^= 0xAA;   /* corrupt body, header unchanged */
    CHECK(zrpc_write_full(fds[0], buf.data, buf.len, 3000) == ZRPC_STATUS_OK);

    zrpc_frame_t f;
    CHECK(zrpc_frame_read(fds[1], &f, 3000) == ZRPC_STATUS_PROTOCOL_ERROR);
    zrpc_buffer_free(&buf);
    close(fds[0]);
    close(fds[1]);
}

int main(void)
{
    /* read_full */
    test_read_full_byte_at_a_time();
    test_read_full_timeout();
    test_read_full_eof_mid();
    test_read_full_invalid();
    /* write_full */
    test_write_full_echo();
    test_write_full_to_closed_peer();
    test_write_full_eagain_path();
    /* frame over a real socket */
    test_frame_roundtrip_over_socket();
    test_frame_sticky_two_frames();
    test_frame_split_header_then_body();
    test_frame_zero_length_ping();
    test_frame_timeout();
    test_frame_partial_header_then_close();
    test_frame_peer_closed_empty();
    test_frame_over_max_rejected_before_body();
    test_frame_bad_magic_over_socket();
    test_frame_bad_crc_over_socket();

    if (g_fail == 0) {
        printf("test_io: all tests passed\n");
        return 0;
    }
    fprintf(stderr, "test_io: %d check(s) failed\n", g_fail);
    return 1;
}
