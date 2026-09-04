/*
 * zrpc_json.h - JSON envelope helpers shared by the C client and server.
 *
 * Wire payload of a REQUEST frame is the object
 *     {"method":"...","auth":"Bearer <token>","deadline_unix_ms":N,"payload":<business>}
 * a RESPONSE/stream payload is  {"payload":<business>}
 * and an ERROR payload is        {"code":N,"message":"...","retryable":bool}
 *
 * <business> is injected / extracted verbatim (cJSON raw nodes) so business
 * JSON is never re-serialized and its field order / number formatting survive.
 */

#ifndef ZRPC_JSON_H
#define ZRPC_JSON_H

#include <stddef.h>
#include <stdint.h>
#include "zrpc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct zrpc_json_envelope {
    char    *method;            /* required; malloc'd */
    char    *auth;              /* optional; NULL when absent */
    int64_t  deadline_unix_ms;  /* 0 when absent */
    char    *payload;           /* business JSON text, verbatim; NULL if none */
    size_t   payload_len;
} zrpc_json_envelope_t;

/* Build a REQUEST envelope over a verbatim business payload. */
int zrpc_json_build_request(const char *method, const char *auth,
                            int64_t deadline_unix_ms,
                            const void *payload, uint32_t payload_len,
                            zrpc_buffer_t *out);

/* Parse a REQUEST envelope (bytes owned by caller). */
int zrpc_json_parse_envelope(const void *bytes, uint32_t len,
                             zrpc_json_envelope_t *env);
void zrpc_json_envelope_free(zrpc_json_envelope_t *env);

/* {"payload": <verbatim business>} */
int zrpc_json_wrap_payload(const void *business, uint32_t business_len,
                           zrpc_buffer_t *out);

/* Extract the verbatim <business> bytes out of a RESPONSE/stream payload. */
int zrpc_json_unwrap_payload(const void *bytes, uint32_t len,
                             zrpc_buffer_t *out);

/* {"code":N,"message":"...","retryable":b} */
int zrpc_json_build_error(int code, const char *message, int retryable,
                          zrpc_buffer_t *out);

/* Parse an ERROR payload; *message is malloc'd when non-NULL. */
int zrpc_json_parse_error(const void *bytes, uint32_t len,
                          int *code, char **message, int *retryable);

#ifdef __cplusplus
}
#endif

#endif /* ZRPC_JSON_H */
