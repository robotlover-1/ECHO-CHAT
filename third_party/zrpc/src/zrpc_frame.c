/*
 * zrpc_frame.c - zrpc v2 frame encoding / decoding and CRC-32.
 *
 * Integers are stored big-endian on the wire by explicit byte placement
 * (never through unaligned pointer casts). Every length is validated against
 * ZRPC_MAX_FRAME_SIZE before any allocation sized by a remote value.
 */

#include <stdlib.h>
#include <string.h>

#include "zrpc_protocol.h"

/* ---- endian helpers (explicit, alignment-safe, endian-independent) ---- */

static void put_u16_be(uint8_t *b, uint16_t v) { b[0] = (uint8_t)(v >> 8);  b[1] = (uint8_t)v; }
static void put_u32_be(uint8_t *b, uint32_t v) { b[0]=(uint8_t)(v>>24); b[1]=(uint8_t)(v>>16); b[2]=(uint8_t)(v>>8); b[3]=(uint8_t)v; }
static void put_u64_be(uint8_t *b, uint64_t v) { for (int i=0;i<8;i++) b[i]=(uint8_t)(v>>(56-8*i)); }

static uint16_t get_u16_be(const uint8_t *b) { return (uint16_t)((b[0]<<8)|b[1]); }
static uint32_t get_u32_be(const uint8_t *b) { return ((uint32_t)b[0]<<24)|((uint32_t)b[1]<<16)|((uint32_t)b[2]<<8)|(uint32_t)b[3]; }
static uint64_t get_u64_be(const uint8_t *b) { uint64_t v=0; for (int i=0;i<8;i++) v=(v<<8)|b[i]; return v; }

/* ---- IEEE CRC-32 ---- */

uint32_t zrpc_crc32(const void *data, size_t len)
{
    const uint8_t *p = (const uint8_t *)data;
    uint32_t crc = 0xFFFFFFFFu;
    while (len--) {
        crc ^= *p++;
        for (int i = 0; i < 8; i++)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return crc ^ 0xFFFFFFFFu;
}
/* PERF: bitwise CRC is simple and thread-safe; if Task 2+ benchmarks show it
 * matters, replace with a slice-by-8 table built under pthread_once. */

/* ---- buffer ---- */

void zrpc_buffer_free(zrpc_buffer_t *buffer)
{
    if (buffer == NULL)
        return;
    free(buffer->data);
    buffer->data = NULL;
    buffer->len = 0;
    buffer->cap = 0;
}

void zrpc_frame_free(zrpc_frame_t *frame)
{
    if (frame == NULL)
        return;
    free(frame->payload);
    frame->payload = NULL;
    frame->length = 0;
    frame->type = 0;
    frame->request_id = 0;
}

/*
 * Validate the 20-byte header. On success fills type, request_id and length
 * and returns OK (or FRAME_TOO_LARGE / PROTOCOL_ERROR without touching them).
 */
static int parse_header(const uint8_t h[ZRPC_HEADER_SIZE],
                        int *type, uint64_t *request_id, uint32_t *length)
{
    if (get_u16_be(h) != ZRPC_MAGIC)
        return ZRPC_STATUS_PROTOCOL_ERROR;    /* wrong port / lost sync */
    if (h[2] != ZRPC_VERSION)
        return ZRPC_STATUS_PROTOCOL_ERROR;
    if (h[3] < ZRPC_MSG_MIN || h[3] > ZRPC_MSG_MAX)
        return ZRPC_STATUS_PROTOCOL_ERROR;

    uint32_t len = get_u32_be(h + 12);
    if (len > ZRPC_MAX_FRAME_SIZE)
        return ZRPC_STATUS_FRAME_TOO_LARGE;   /* checked before any allocation */

    if (type)       *type = h[3];
    if (request_id) *request_id = get_u64_be(h + 4);
    if (length)     *length = len;
    return ZRPC_STATUS_OK;
}

static void build_header(uint8_t h[ZRPC_HEADER_SIZE], int type,
                         uint64_t request_id, uint32_t length,
                         const void *payload)
{
    memset(h, 0, ZRPC_HEADER_SIZE);
    put_u16_be(h, ZRPC_MAGIC);
    h[2] = ZRPC_VERSION;
    h[3] = (uint8_t)type;
    put_u64_be(h + 4, request_id);
    put_u32_be(h + 12, length);
    put_u32_be(h + 16, zrpc_crc32(payload, length));
}

int zrpc_frame_encode(zrpc_msg_type_t type, uint64_t request_id,
                      const void *payload, uint32_t payload_len,
                      zrpc_buffer_t *out)
{
    if (out == NULL)
        return ZRPC_STATUS_INVALID_ARGUMENT;

    /* Leave *out fully zeroed on every path (success or any failure). */
    out->data = NULL;
    out->len = 0;
    out->cap = 0;

    if (type < ZRPC_MSG_MIN || type > ZRPC_MSG_MAX)
        return ZRPC_STATUS_INVALID_ARGUMENT;
    if (payload_len > ZRPC_MAX_FRAME_SIZE)
        return ZRPC_STATUS_FRAME_TOO_LARGE;
    if (payload == NULL && payload_len > 0)
        return ZRPC_STATUS_INVALID_ARGUMENT;

    uint8_t *raw = (uint8_t *)malloc(ZRPC_HEADER_SIZE + payload_len);
    if (raw == NULL)
        return ZRPC_STATUS_RESOURCE_EXHAUSTED;

    build_header(raw, (int)type, request_id, payload_len, payload);
    if (payload_len > 0)
        memcpy(raw + ZRPC_HEADER_SIZE, payload, payload_len);

    out->data = raw;
    out->len = ZRPC_HEADER_SIZE + payload_len;
    out->cap = out->len;
    return ZRPC_STATUS_OK;
}

int zrpc_frame_decode(const void *raw, size_t raw_len, zrpc_frame_t *out)
{
    if (out == NULL)
        return ZRPC_STATUS_INVALID_ARGUMENT;
    memset(out, 0, sizeof(*out));

    if (raw == NULL || raw_len < ZRPC_HEADER_SIZE)
        return ZRPC_STATUS_PROTOCOL_ERROR;

    const uint8_t *h = (const uint8_t *)raw;
    int type;
    uint64_t request_id;
    uint32_t length;
    int st = parse_header(h, &type, &request_id, &length);
    if (st != ZRPC_STATUS_OK)
        return st;

    if (raw_len < (size_t)ZRPC_HEADER_SIZE + length)
        return ZRPC_STATUS_PROTOCOL_ERROR;   /* truncated payload */

    const uint8_t *body = (const uint8_t *)raw + ZRPC_HEADER_SIZE;
    if (get_u32_be(h + 16) != zrpc_crc32(body, length))
        return ZRPC_STATUS_PROTOCOL_ERROR;   /* corrupt frame */

    void *copy = NULL;
    if (length > 0) {
        copy = malloc(length);
        if (copy == NULL)
            return ZRPC_STATUS_RESOURCE_EXHAUSTED;
        memcpy(copy, body, length);
    }

    out->type = type;
    out->request_id = request_id;
    out->length = length;
    out->payload = copy;
    return ZRPC_STATUS_OK;
}

int zrpc_frame_read(int fd, zrpc_frame_t *out, int timeout_ms)
{
    if (out == NULL)
        return ZRPC_STATUS_INVALID_ARGUMENT;
    memset(out, 0, sizeof(*out));

    int64_t deadline = ZRPC_DEADLINE_NONE;   /* shared budget: header + payload */
    if (timeout_ms > 0)
        deadline = zrpc_now_mono_ms() + timeout_ms;

    uint8_t hdr[ZRPC_HEADER_SIZE];
    int st = zrpc_read_full_until(fd, hdr, sizeof(hdr), deadline);
    if (st != ZRPC_STATUS_OK)
        return st;

    int type;
    uint64_t request_id;
    uint32_t length;
    st = parse_header(hdr, &type, &request_id, &length);
    if (st != ZRPC_STATUS_OK)
        return st;    /* includes FRAME_TOO_LARGE: rejected before allocating */

    void *payload = NULL;
    if (length > 0) {
        payload = malloc(length);
        if (payload == NULL)
            return ZRPC_STATUS_RESOURCE_EXHAUSTED;
        st = zrpc_read_full_until(fd, payload, length, deadline);
        if (st != ZRPC_STATUS_OK) {
            free(payload);
            return st;
        }
        if (get_u32_be(hdr + 16) != zrpc_crc32(payload, length)) {
            free(payload);
            return ZRPC_STATUS_PROTOCOL_ERROR;
        }
    }

    out->type = type;
    out->request_id = request_id;
    out->length = length;
    out->payload = payload;
    return ZRPC_STATUS_OK;
}
