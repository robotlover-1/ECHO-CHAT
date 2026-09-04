/*
 * zrpc_protocol.h - zrpc v2 wire protocol definitions.
 *
 * Frame layout (20-byte header, all integers big-endian on the wire):
 *
 *   +----------+---------+---------+----------------+----------------+----------+
 *   | magic 2B | ver 1B  | type 1B |  request_id 8B |   length 4B    | crc32 4B |
 *   +----------+---------+---------+----------------+----------------+----------+
 *   |                    payload: length bytes (UTF-8 JSON)                    |
 *   +----------------------------------------------------------------------------+
 *
 * magic = 0x5A52 ("ZR"), version = 2. crc32 is the IEEE CRC-32 of the payload
 * only; it is a corruption check, not an authenticity or security mechanism.
 *
 * This is zrpc v2, a fresh implementation that shares only the high-level idea
 * of the original teaching skeleton (reference/zrpc-original): a registered
 * method table + CRC + length-framed JSON. See LICENSE-NOTICE.md.
 */

#ifndef ZRPC_PROTOCOL_H
#define ZRPC_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- version & limits ---- */
#define ZRPC_MAGIC              0x5A52u    /* 'Z' 'R' */
#define ZRPC_VERSION            2
#define ZRPC_HEADER_SIZE        20u
#define ZRPC_MAX_FRAME_SIZE     (4u * 1024u * 1024u)  /* 4 MiB */
#define ZRPC_DEFAULT_TIMEOUT_MS 30000u
#define ZRPC_MAX_INFLIGHT_PER_CONN 64u
#define ZRPC_DEADLINE_NONE      INT64_MAX   /* internal: no timeout sentinel */

/* ---- message types ---- */
typedef enum zrpc_msg_type {
    ZRPC_MSG_REQUEST   = 1,
    ZRPC_MSG_RESPONSE  = 2,
    ZRPC_MSG_STREAM_DATA = 3,
    ZRPC_MSG_STREAM_END  = 4,
    ZRPC_MSG_ERROR       = 5,
    ZRPC_MSG_CANCEL      = 6,
    ZRPC_MSG_PING        = 7,
    ZRPC_MSG_PONG        = 8,
    ZRPC_MSG_MIN         = ZRPC_MSG_REQUEST,
    ZRPC_MSG_MAX         = ZRPC_MSG_PONG
} zrpc_msg_type_t;

/* ---- status / error codes (stable, public contract) ---- */
typedef enum zrpc_status {
    ZRPC_STATUS_OK                = 0,
    ZRPC_STATUS_CANCELLED         = 1,
    ZRPC_STATUS_INVALID_ARGUMENT  = 2,
    ZRPC_STATUS_UNAUTHENTICATED   = 3,
    ZRPC_STATUS_NOT_FOUND         = 4,
    ZRPC_STATUS_DEADLINE_EXCEEDED = 5,
    ZRPC_STATUS_RESOURCE_EXHAUSTED= 6,
    ZRPC_STATUS_UNAVAILABLE       = 7,
    ZRPC_STATUS_INTERNAL          = 8,
    ZRPC_STATUS_PROTOCOL_ERROR    = 9,
    ZRPC_STATUS_FRAME_TOO_LARGE   = 10
} zrpc_status_t;

const char *zrpc_status_str(int status);

/* ---- buffers ---- */
typedef struct zrpc_buffer {
    uint8_t *data;
    uint32_t len;
    uint32_t cap;
} zrpc_buffer_t;

void zrpc_buffer_free(zrpc_buffer_t *buffer);

/* ---- frames ---- */
typedef struct zrpc_frame {
    int        type;        /* zrpc_msg_type_t */
    uint64_t   request_id;
    uint32_t   length;      /* payload length in bytes */
    void      *payload;     /* owned; NULL when length == 0 */
} zrpc_frame_t;

void zrpc_frame_free(zrpc_frame_t *frame);

/*
 * Encode one frame into a freshly allocated contiguous buffer (header followed
 * by payload). Caller releases with zrpc_buffer_free(). Returns zrpc_status_t.
 * payload_len > ZRPC_MAX_FRAME_SIZE -> ZRPC_STATUS_FRAME_TOO_LARGE (before any
 * allocation). type outside [ZRPC_MSG_MIN,ZRPC_MSG_MAX] -> INVALID_ARGUMENT.
 */
int zrpc_frame_encode(zrpc_msg_type_t type,
                      uint64_t request_id,
                      const void *payload,
                      uint32_t payload_len,
                      zrpc_buffer_t *out);

/*
 * Decode one frame from a contiguous in-memory buffer of raw_len bytes (header
 * + payload). The payload is copied into a freshly allocated buffer owned by
 * *out. Returns zrpc_status_t; ZRPC_STATUS_PROTOCOL_ERROR for a bad magic /
 * version / type / CRC, ZRPC_STATUS_FRAME_TOO_LARGE when the declared length
 * exceeds the limit, PROTOCOL_ERROR when raw_len is too short for the payload.
 */
int zrpc_frame_decode(const void *raw, size_t raw_len, zrpc_frame_t *out);

/*
 * Read exactly one frame from fd within timeout_ms (a single shared deadline
 * covers both header and payload). A frame larger than ZRPC_MAX_FRAME_SIZE is
 * rejected right after the header, before allocating. See zrpc_read_full for
 * status semantics on EOF / timeout / errors. Caller releases *out via
 * zrpc_frame_free().
 */
int zrpc_frame_read(int fd, zrpc_frame_t *out, int timeout_ms);

/* IEEE CRC-32 (poly 0xEDB88320), init 0xFFFFFFFF, final xor 0xFFFFFFFF. */
uint32_t zrpc_crc32(const void *data, size_t len);

/* ---- safe IO (zrpc_io.c) ----
 *
 * Read/write exactly len bytes on a (possibly non-blocking) socket, handling
 * EINTR, EAGAIN/EWOULDBLOCK via poll, short transfers, orderly close and
 * timeouts. Returns zrpc_status_t:
 *   - OK                 : all len bytes transferred
 *   - UNAVAILABLE        : peer closed/reset before len bytes (recv 0 / ECONNRESET / EPIPE), or poll HUP/ERR
 *   - DEADLINE_EXCEEDED  : timeout_ms elapsed before completion
 *   - INTERNAL           : unexpected syscall error
 *   - INVALID_ARGUMENT   : NULL buf while len > 0
 *
 * timeout_ms > 0 is a wall-clock budget; timeout_ms == 0 blocks indefinitely.
 * write_full never triggers SIGPIPE (uses MSG_NOSIGNAL).
 */
int zrpc_read_full(int fd, void *buf, size_t len, int timeout_ms);
int zrpc_write_full(int fd, const void *buf, size_t len, int timeout_ms);

/* Variants that share one monotonic deadline across several calls. */
int64_t zrpc_now_mono_ms(void);
int zrpc_read_full_until(int fd, void *buf, size_t len, int64_t deadline_ms);
int zrpc_write_full_until(int fd, const void *buf, size_t len, int64_t deadline_ms);

#ifdef __cplusplus
}
#endif

#endif /* ZRPC_PROTOCOL_H */
