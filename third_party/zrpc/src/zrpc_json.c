/*
 * zrpc_json.c - JSON envelope helpers (see zrpc_json.h).
 *
 * Business JSON is embedded/extracted verbatim via cJSON raw nodes so it is
 * never parsed and re-serialized (which would reorder fields or perturb number
 * formatting). Envelope metadata (method/auth/deadline) is parsed normally.
 */

#include <stdlib.h>
#include <string.h>

#include "zrpc_json.h"
#include "cJSON.h"

/* ---- small helpers ---- */

static char *dup_bytes(const void *bytes, size_t len)
{
    if (len == 0) return NULL;
    char *s = (char *)malloc(len + 1);
    if (!s) return NULL;
    memcpy(s, bytes, len);
    s[len] = '\0';
    return s;
}

/* Attach <business> bytes as a verbatim raw JSON member named key. */
static int add_raw_member(cJSON *root, const char *key,
                          const void *business, uint32_t business_len)
{
    if (business == NULL || business_len == 0) {
        return cJSON_AddRawToObject(root, key, "{}") != NULL ? 0 : -1;
    }
    char *raw = dup_bytes(business, business_len);
    if (!raw) return -1;
    cJSON *item = cJSON_CreateRaw(raw);
    free(raw);
    if (!item) return -1;
    cJSON_AddItemToObject(root, key, item);
    return 0;
}

static int store_buffer(zrpc_buffer_t *out, const char *text)
{
    out->data = NULL;
    out->len = 0;
    out->cap = 0;
    size_t n = text ? strlen(text) : 0;
    if (n == 0) return ZRPC_STATUS_OK;
    out->data = (uint8_t *)malloc(n);
    if (!out->data) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    memcpy(out->data, text, n);
    out->len = (uint32_t)n;
    out->cap = (uint32_t)n;
    return ZRPC_STATUS_OK;
}

/* ---- request envelope ---- */

int zrpc_json_build_request(const char *method, const char *auth,
                            int64_t deadline_unix_ms,
                            const void *payload, uint32_t payload_len,
                            zrpc_buffer_t *out)
{
    if (!out || !method) return ZRPC_STATUS_INVALID_ARGUMENT;
    out->data = NULL;
    out->len = 0;
    out->cap = 0;

    cJSON *root = cJSON_CreateObject();
    if (!root) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    cJSON_AddStringToObject(root, "method", method);
    if (auth && auth[0]) cJSON_AddStringToObject(root, "auth", auth);
    if (deadline_unix_ms > 0)
        cJSON_AddNumberToObject(root, "deadline_unix_ms", (double)deadline_unix_ms);
    if (add_raw_member(root, "payload", payload, payload_len) != 0) {
        cJSON_Delete(root);
        return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    }

    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!text) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    int st = store_buffer(out, text);
    free(text);
    return st;
}

int zrpc_json_parse_envelope(const void *bytes, uint32_t len,
                             zrpc_json_envelope_t *env)
{
    if (!env) return ZRPC_STATUS_INVALID_ARGUMENT;
    memset(env, 0, sizeof(*env));
    if (!bytes || len == 0) return ZRPC_STATUS_PROTOCOL_ERROR;

    char *text = dup_bytes(bytes, len);
    if (!text) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    cJSON *root = cJSON_Parse(text);
    free(text);
    if (!root) return ZRPC_STATUS_PROTOCOL_ERROR;

    cJSON *m = cJSON_GetObjectItemCaseSensitive(root, "method");
    if (!cJSON_IsString(m)) { cJSON_Delete(root); return ZRPC_STATUS_PROTOCOL_ERROR; }

    env->method = strdup(m->valuestring);
    cJSON *a = cJSON_GetObjectItemCaseSensitive(root, "auth");
    if (cJSON_IsString(a)) env->auth = strdup(a->valuestring);
    cJSON *d = cJSON_GetObjectItemCaseSensitive(root, "deadline_unix_ms");
    if (cJSON_IsNumber(d)) env->deadline_unix_ms = (int64_t)d->valuedouble;
    cJSON *p = cJSON_GetObjectItemCaseSensitive(root, "payload");
    if (p) {
        if (cJSON_IsRaw(p)) {
            env->payload = p->valuestring ? strdup(p->valuestring) : NULL;
        } else if (cJSON_IsObject(p) || cJSON_IsArray(p)) {
            env->payload = cJSON_PrintUnformatted(p);   /* e.g. non-raw path */
        } else if (cJSON_IsString(p)) {
            env->payload = strdup(p->valuestring);
        }
        env->payload_len = env->payload ? strlen(env->payload) : 0;
    }
    cJSON_Delete(root);
    if (!env->method) { zrpc_json_envelope_free(env); return ZRPC_STATUS_RESOURCE_EXHAUSTED; }
    return ZRPC_STATUS_OK;
}

void zrpc_json_envelope_free(zrpc_json_envelope_t *env)
{
    if (!env) return;
    free(env->method);
    free(env->auth);
    free(env->payload);
    memset(env, 0, sizeof(*env));
}

/* ---- response wrapping / unwrapping ---- */

int zrpc_json_wrap_payload(const void *business, uint32_t business_len,
                           zrpc_buffer_t *out)
{
    if (!out) return ZRPC_STATUS_INVALID_ARGUMENT;
    out->data = NULL;
    out->len = 0;
    out->cap = 0;

    cJSON *root = cJSON_CreateObject();
    if (!root) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    if (add_raw_member(root, "payload", business, business_len) != 0) {
        cJSON_Delete(root);
        return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    }
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!text) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    int st = store_buffer(out, text);
    free(text);
    return st;
}

int zrpc_json_unwrap_payload(const void *bytes, uint32_t len,
                             zrpc_buffer_t *out)
{
    if (!out) return ZRPC_STATUS_INVALID_ARGUMENT;
    out->data = NULL;
    out->len = 0;
    out->cap = 0;
    if (!bytes || len == 0) return ZRPC_STATUS_PROTOCOL_ERROR;

    char *text = dup_bytes(bytes, len);
    if (!text) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    cJSON *root = cJSON_Parse(text);
    free(text);
    if (!root) return ZRPC_STATUS_PROTOCOL_ERROR;

    cJSON *p = cJSON_GetObjectItemCaseSensitive(root, "payload");
    int st = ZRPC_STATUS_PROTOCOL_ERROR;
    if (p) {
        char *inner = NULL;
        if (cJSON_IsRaw(p)) inner = p->valuestring ? strdup(p->valuestring) : NULL;
        else if (cJSON_IsObject(p) || cJSON_IsArray(p)) inner = cJSON_PrintUnformatted(p);
        else if (cJSON_IsString(p)) inner = strdup(p->valuestring);
        else if (cJSON_IsNumber(p)) inner = cJSON_PrintUnformatted(p);
        if (inner) {
            st = store_buffer(out, inner);
            free(inner);
        } else {
            st = ZRPC_STATUS_RESOURCE_EXHAUSTED;
        }
    }
    cJSON_Delete(root);
    return st;
}

/* ---- error envelope ---- */

int zrpc_json_build_error(int code, const char *message, int retryable,
                          zrpc_buffer_t *out)
{
    if (!out) return ZRPC_STATUS_INVALID_ARGUMENT;
    out->data = NULL;
    out->len = 0;
    out->cap = 0;

    cJSON *root = cJSON_CreateObject();
    if (!root) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    cJSON_AddNumberToObject(root, "code", code);
    cJSON_AddStringToObject(root, "message", message ? message : "");
    cJSON_AddBoolToObject(root, "retryable", retryable ? 1 : 0);
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!text) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    int st = store_buffer(out, text);
    free(text);
    return st;
}

int zrpc_json_parse_error(const void *bytes, uint32_t len,
                          int *code, char **message, int *retryable)
{
    if (!bytes || len == 0) return ZRPC_STATUS_PROTOCOL_ERROR;

    char *text = dup_bytes(bytes, len);
    if (!text) return ZRPC_STATUS_RESOURCE_EXHAUSTED;
    cJSON *root = cJSON_Parse(text);
    free(text);
    if (!root) return ZRPC_STATUS_PROTOCOL_ERROR;

    int st = ZRPC_STATUS_OK;
    cJSON *c = cJSON_GetObjectItemCaseSensitive(root, "code");
    if (code) *code = cJSON_IsNumber(c) ? (int)c->valuedouble : ZRPC_STATUS_INTERNAL;
    cJSON *msg = cJSON_GetObjectItemCaseSensitive(root, "message");
    if (message) {
        *message = NULL;
        if (cJSON_IsString(msg)) *message = strdup(msg->valuestring);
    }
    cJSON *r = cJSON_GetObjectItemCaseSensitive(root, "retryable");
    if (retryable) *retryable = cJSON_IsBool(r) && cJSON_IsTrue(r);
    cJSON_Delete(root);
    return st;
}
