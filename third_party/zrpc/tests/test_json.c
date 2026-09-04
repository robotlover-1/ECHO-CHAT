/*
 * test_json.c - unit tests for the JSON envelope helpers (zrpc_json.c).
 * Pure C, no coroutines: safe to run under ASan/UBSan with leak checks on.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "zrpc.h"
#include "zrpc_json.h"

static int g_fail = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            g_fail++;                                                   \
        }                                                               \
    } while (0)

static void test_request_roundtrip_verbatim(void)
{
    const char *payload = "{\"b\":40,\"a\":2,\"nested\":{\"x\":[1,2.5]}}";
    zrpc_buffer_t env = { NULL, 0, 0 };
    int st = zrpc_json_build_request("chat.completion", "Bearer sekret",
                                     1788547200000LL, payload, (uint32_t)strlen(payload), &env);
    CHECK(st == ZRPC_STATUS_OK);

    zrpc_json_envelope_t e;
    st = zrpc_json_parse_envelope(env.data, env.len, &e);
    CHECK(st == ZRPC_STATUS_OK);
    CHECK(e.method && strcmp(e.method, "chat.completion") == 0);
    CHECK(e.auth && strcmp(e.auth, "Bearer sekret") == 0);
    CHECK(e.deadline_unix_ms == 1788547200000LL);
    CHECK(e.payload && strcmp(e.payload, payload) == 0);   /* verbatim, order kept */
    zrpc_json_envelope_free(&e);
    zrpc_buffer_free(&env);
}

static void test_request_without_auth_deadline(void)
{
    const char *payload = "{\"ok\":true}";
    zrpc_buffer_t env = { NULL, 0, 0 };
    CHECK(zrpc_json_build_request("filter.validate", NULL, 0, payload,
                                  (uint32_t)strlen(payload), &env) == ZRPC_STATUS_OK);
    zrpc_json_envelope_t e;
    CHECK(zrpc_json_parse_envelope(env.data, env.len, &e) == ZRPC_STATUS_OK);
    CHECK(e.auth == NULL && e.deadline_unix_ms == 0);
    CHECK(e.payload && strcmp(e.payload, payload) == 0);
    zrpc_json_envelope_free(&e);
    zrpc_buffer_free(&env);
}

static void test_response_wrap_unwrap(void)
{
    const char *business = "{\"choices\":[{\"text\":\"hi\"}],\"source\":\"llm\"}";
    zrpc_buffer_t wrapped = { NULL, 0, 0 };
    CHECK(zrpc_json_wrap_payload(business, (uint32_t)strlen(business), &wrapped) == ZRPC_STATUS_OK);

    zrpc_buffer_t out = { NULL, 0, 0 };
    CHECK(zrpc_json_unwrap_payload(wrapped.data, wrapped.len, &out) == ZRPC_STATUS_OK);
    CHECK(out.len == strlen(business) && memcmp(out.data, business, out.len) == 0);
    zrpc_buffer_free(&out);
    zrpc_buffer_free(&wrapped);
}

static void test_error_roundtrip(void)
{
    zrpc_buffer_t errp = { NULL, 0, 0 };
    CHECK(zrpc_json_build_error(9, "protocol error", 0, &errp) == ZRPC_STATUS_OK);
    int code = -1, retryable = 1;
    char *msg = NULL;
    CHECK(zrpc_json_parse_error(errp.data, errp.len, &code, &msg, &retryable) == ZRPC_STATUS_OK);
    CHECK(code == 9);
    CHECK(msg && strcmp(msg, "protocol error") == 0);
    CHECK(retryable == 0);
    free(msg);
    zrpc_buffer_free(&errp);
}

static void test_malformed(void)
{
    const char *bad = "not-json{";
    zrpc_buffer_t out = { NULL, 0, 0 };
    CHECK(zrpc_json_unwrap_payload(bad, (uint32_t)strlen(bad), &out) == ZRPC_STATUS_PROTOCOL_ERROR);
    zrpc_json_envelope_t e;
    CHECK(zrpc_json_parse_envelope(bad, (uint32_t)strlen(bad), &e) == ZRPC_STATUS_PROTOCOL_ERROR);

    /* missing method => protocol error */
    const char *noid = "{\"auth\":\"x\",\"payload\":{}}";
    CHECK(zrpc_json_parse_envelope(noid, (uint32_t)strlen(noid), &e) == ZRPC_STATUS_PROTOCOL_ERROR);
}

int main(void)
{
    test_request_roundtrip_verbatim();
    test_request_without_auth_deadline();
    test_response_wrap_unwrap();
    test_error_roundtrip();
    test_malformed();

    if (g_fail == 0) {
        printf("test_json: all tests passed\n");
        return 0;
    }
    fprintf(stderr, "test_json: %d check(s) failed\n", g_fail);
    return 1;
}
