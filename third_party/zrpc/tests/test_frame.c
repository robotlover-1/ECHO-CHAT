/*
 * test_frame.c - unit tests for the zrpc v2 frame encode/decode layer.
 *
 * Covers: known-vector CRC, endian layout, round-trip, zero-length payload,
 * max / over-max length, bad magic / version / type, corrupt CRC and truncated
 * input. Each frame is validated before allocating payload-sized buffers.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "zrpc.h"

static int g_fail = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            g_fail++;                                                   \
        }                                                               \
    } while (0)

/* ---- big-endian byte writers (mirror the wire codec) ---- */
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

/* Build a raw frame image; returns bytes on the heap, caller frees. */
static uint8_t *craft_frame(int type, uint64_t rid, const void *payload,
                            uint32_t plen, int corrupt_crc)
{
    uint8_t *raw = (uint8_t *)malloc(ZRPC_HEADER_SIZE + plen);
    put_u16_be(raw, ZRPC_MAGIC);
    raw[2] = ZRPC_VERSION;
    raw[3] = (uint8_t)type;
    put_u64_be(raw + 4, rid);
    put_u32_be(raw + 12, plen);
    /* over-max header is rejected before CRC is ever checked, so skip the
     * computation when there is no real payload backing the declared size. */
    uint32_t crc = (payload && plen) ? zrpc_crc32(payload, plen) : 0u;
    if (corrupt_crc) crc ^= 0xDEADBEEFu;
    put_u32_be(raw + 16, crc);
    if (payload && plen) memcpy(raw + ZRPC_HEADER_SIZE, payload, plen);
    return raw;
}

static void test_crc32_known_vector(void)
{
    const char *msg = "123456789";
    CHECK(zrpc_crc32(msg, 9) == 0xCBF43926u);
}

static void test_roundtrip(void)
{
    /* binary-opaque payload spanning all byte values */
    uint8_t payload[300];
    for (int i = 0; i < (int)sizeof(payload); i++) payload[i] = (uint8_t)(i * 7 + 3);
    uint64_t rid = 0x1122334455667788ULL;

    zrpc_buffer_t buf;
    CHECK(zrpc_frame_encode(ZRPC_MSG_STREAM_DATA, rid, payload, sizeof(payload), &buf)
          == ZRPC_STATUS_OK);

    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(buf.data, buf.len, &f) == ZRPC_STATUS_OK);
    CHECK(f.type == ZRPC_MSG_STREAM_DATA);
    CHECK(f.request_id == rid);
    CHECK(f.length == sizeof(payload));
    CHECK(f.payload != NULL && memcmp(f.payload, payload, sizeof(payload)) == 0);
    zrpc_frame_free(&f);
    zrpc_buffer_free(&buf);
}

static void test_header_layout_big_endian(void)
{
    uint8_t payload[4] = { 1, 2, 3, 4 };
    zrpc_buffer_t buf;
    CHECK(zrpc_frame_encode(ZRPC_MSG_REQUEST, 0x0102030405060708ULL, payload, 4, &buf)
          == ZRPC_STATUS_OK);
    const uint8_t *h = buf.data;
    CHECK(h[0] == 0x5A && h[1] == 0x52);            /* magic 'ZR' */
    CHECK(h[2] == ZRPC_VERSION);
    CHECK(h[3] == ZRPC_MSG_REQUEST);
    CHECK(h[4] == 0x01 && h[5] == 0x02 && h[11] == 0x08); /* rid big-endian */
    CHECK(h[12] == 0x00 && h[15] == 0x04);          /* length big-endian */
    CHECK(zrpc_crc32(payload, 4) == ((uint32_t)h[16] << 24 | (uint32_t)h[17] << 16
                                     | (uint32_t)h[18] << 8 | h[19]));
    zrpc_buffer_free(&buf);
}

static void test_zero_length_payload(void)
{
    zrpc_buffer_t buf;
    CHECK(zrpc_frame_encode(ZRPC_MSG_PING, 7, NULL, 0, &buf) == ZRPC_STATUS_OK);
    CHECK(buf.len == ZRPC_HEADER_SIZE);

    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(buf.data, buf.len, &f) == ZRPC_STATUS_OK);
    CHECK(f.type == ZRPC_MSG_PING && f.request_id == 7);
    CHECK(f.length == 0 && f.payload == NULL);
    zrpc_frame_free(&f);
    zrpc_buffer_free(&buf);
}

static void test_max_length_payload_ok(void)
{
    uint32_t plen = ZRPC_MAX_FRAME_SIZE;
    uint8_t *payload = (uint8_t *)malloc(plen);
    memset(payload, 0xAB, plen);

    zrpc_buffer_t buf;
    CHECK(zrpc_frame_encode(ZRPC_MSG_STREAM_DATA, 1, payload, plen, &buf) == ZRPC_STATUS_OK);

    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(buf.data, buf.len, &f) == ZRPC_STATUS_OK);
    CHECK(f.length == plen && memcmp(f.payload, payload, plen) == 0);
    zrpc_frame_free(&f);
    zrpc_buffer_free(&buf);
    free(payload);
}

static void test_over_max_encode_rejected(void)
{
    uint8_t dummy = 0;
    zrpc_buffer_t buf;
    CHECK(zrpc_frame_encode(ZRPC_MSG_REQUEST, 1, &dummy, ZRPC_MAX_FRAME_SIZE + 1, &buf)
          == ZRPC_STATUS_FRAME_TOO_LARGE);
    CHECK(buf.data == NULL && buf.len == 0 && buf.cap == 0); /* no allocation */
}

static void test_over_max_decode_rejected(void)
{
    /* Declared length over the cap: must reject after header, before payload. */
    uint8_t *raw = craft_frame(ZRPC_MSG_REQUEST, 1, NULL, ZRPC_MAX_FRAME_SIZE + 1, 0);
    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(raw, ZRPC_HEADER_SIZE, &f) == ZRPC_STATUS_FRAME_TOO_LARGE);
    free(raw);
}

static void test_bad_magic(void)
{
    uint8_t raw[ZRPC_HEADER_SIZE] = { 0 };
    put_u16_be(raw, 0x1234);   /* wrong magic */
    raw[2] = ZRPC_VERSION;
    raw[3] = ZRPC_MSG_REQUEST;
    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(raw, sizeof(raw), &f) == ZRPC_STATUS_PROTOCOL_ERROR);
}

static void test_bad_version(void)
{
    uint8_t raw[ZRPC_HEADER_SIZE] = { 0 };
    put_u16_be(raw, ZRPC_MAGIC);
    raw[2] = 99;               /* wrong version */
    raw[3] = ZRPC_MSG_REQUEST;
    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(raw, sizeof(raw), &f) == ZRPC_STATUS_PROTOCOL_ERROR);
}

static void test_bad_type(void)
{
    for (int type = 0; type <= 9; type++) {
        if (type >= ZRPC_MSG_MIN && type <= ZRPC_MSG_MAX) continue;
        uint8_t *raw = craft_frame(type, 1, NULL, 0, 0);
        zrpc_frame_t f;
        CHECK(zrpc_frame_decode(raw, ZRPC_HEADER_SIZE, &f) == ZRPC_STATUS_PROTOCOL_ERROR);
        free(raw);
    }
}

static void test_corrupt_crc(void)
{
    const char *payload = "hello zrpc";
    uint8_t *raw = craft_frame(ZRPC_MSG_RESPONSE, 9, payload, (uint32_t)strlen(payload), 1);
    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(raw, ZRPC_HEADER_SIZE + strlen(payload), &f)
          == ZRPC_STATUS_PROTOCOL_ERROR);
    free(raw);
}

static void test_corrupt_payload_byte(void)
{
    const char *payload = "hello zrpc";
    uint8_t *raw = craft_frame(ZRPC_MSG_RESPONSE, 9, payload, (uint32_t)strlen(payload), 0);
    raw[ZRPC_HEADER_SIZE + 3] ^= 0xFF;   /* damage payload, header crc now stale */
    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(raw, ZRPC_HEADER_SIZE + strlen(payload), &f)
          == ZRPC_STATUS_PROTOCOL_ERROR);
    free(raw);
}

static void test_truncated_input(void)
{
    const char *payload = "payload-that-never-arrives-fully";
    uint32_t plen = (uint32_t)strlen(payload);
    uint8_t *raw = craft_frame(ZRPC_MSG_STREAM_DATA, 3, payload, plen, 0);

    zrpc_frame_t f;
    CHECK(zrpc_frame_decode(raw, 10, &f) == ZRPC_STATUS_PROTOCOL_ERROR);               /* < header */
    CHECK(zrpc_frame_decode(raw, ZRPC_HEADER_SIZE, &f) == ZRPC_STATUS_PROTOCOL_ERROR); /* header only */
    CHECK(zrpc_frame_decode(raw, ZRPC_HEADER_SIZE + plen - 1, &f)
          == ZRPC_STATUS_PROTOCOL_ERROR);                                              /* short body */
    CHECK(zrpc_frame_decode(NULL, 0, &f) == ZRPC_STATUS_PROTOCOL_ERROR);
    free(raw);
}

static void test_argument_checks(void)
{
    zrpc_buffer_t buf;
    const char one = 1;
    CHECK(zrpc_frame_encode((zrpc_msg_type_t)0, 1, &one, 1, &buf) == ZRPC_STATUS_INVALID_ARGUMENT);
    CHECK(zrpc_frame_encode(ZRPC_MSG_REQUEST, 1, NULL, 1, &buf) == ZRPC_STATUS_INVALID_ARGUMENT);
    CHECK(zrpc_frame_encode(ZRPC_MSG_REQUEST, 1, &one, 1, NULL) == ZRPC_STATUS_INVALID_ARGUMENT);
}

int main(void)
{
    test_crc32_known_vector();
    test_roundtrip();
    test_header_layout_big_endian();
    test_zero_length_payload();
    test_max_length_payload_ok();
    test_over_max_encode_rejected();
    test_over_max_decode_rejected();
    test_bad_magic();
    test_bad_version();
    test_bad_type();
    test_corrupt_crc();
    test_corrupt_payload_byte();
    test_truncated_input();
    test_argument_checks();

    if (g_fail == 0) {
        printf("test_frame: all tests passed\n");
        return 0;
    }
    fprintf(stderr, "test_frame: %d check(s) failed\n", g_fail);
    return 1;
}
