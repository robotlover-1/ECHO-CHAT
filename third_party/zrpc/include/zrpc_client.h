/*
 * zrpc_client.h - zrpc v2 C client ABI (unary + ping; stream in Task 5).
 *
 * The client is thread-safe in the sense that distinct zrpc_client_t instances
 * may be used from distinct threads concurrently; a single instance must be
 * confined to one caller at a time (the Go bridge layers a pool on top).
 */

#ifndef ZRPC_CLIENT_H
#define ZRPC_CLIENT_H

#include <stdint.h>
#include "zrpc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct zrpc_client zrpc_client_t;

/*
 * Stream callback shape (declared now to freeze the ABI; wired up in Task 5):
 *   event_type: ZRPC_MSG_STREAM_DATA / ZRPC_MSG_STREAM_END / ZRPC_MSG_ERROR
 *   status:     zrpc_status_t (meaningful for ZRPC_MSG_ERROR)
 *   data/data_len: for STREAM_DATA, the raw JSON bytes (owned by the library,
 *                  valid only for the duration of the call).
 */
typedef void (*zrpc_stream_callback_t)(uint64_t callback_handle,
                                       uint64_t request_id,
                                       int event_type,
                                       int status,
                                       const void *data,
                                       uint32_t data_len);

/*
 * Connect to host:port. token is copied; may be NULL for servers with auth
 * disabled. connect_timeout_ms / io_timeout_ms are IO budgets; 0 blocks
 * indefinitely. Returns NULL on failure (use zrpc_client_last_error()).
 */
zrpc_client_t *zrpc_client_new(const char *host, uint16_t port, const char *token,
                               int connect_timeout_ms, int io_timeout_ms);

/*
 * Perform a unary call. req_json is the business request JSON (object). On
 * success ZRPC_STATUS_OK is returned and *response holds the business response
 * JSON (object), caller frees with zrpc_buffer_free(). Deadline is an absolute
 * unix-millis deadline; 0 means none.
 */
int zrpc_client_call_unary(zrpc_client_t *client,
                           const char *method,
                           const void *req_json,
                           uint32_t req_len,
                           uint64_t deadline_unix_ms,
                           zrpc_buffer_t *response);

/* Round-trip PING/PONG on a fresh short-lived connection. */
int zrpc_client_ping(zrpc_client_t *client, int timeout_ms);

/*
 * Blocking server-streaming call (Task 5). Each call uses a DEDICATED TCP
 * connection, so a slow stream never head-of-line-blocks another request. The
 * C callback is invoked for every event and must copy data before returning.
 * Returns OK after STREAM_END, the zrpc error code after an ERROR frame,
 * CANCELLED if zrpc_client_cancel() is called from another thread, or the IO
 * status when the connection dies.
 */
int zrpc_client_call_stream(zrpc_client_t *client,
                            const char *method,
                            const void *req_json,
                            uint32_t req_len,
                            uint64_t deadline_unix_ms,
                            uint64_t callback_handle,
                            zrpc_stream_callback_t callback);

/* Ask an in-flight zrpc_client_call_stream to stop (wakes its blocked read). */
void zrpc_client_cancel(zrpc_client_t *client);

/* Close the underlying connection(s); safe to call multiple times. */
void zrpc_client_close(zrpc_client_t *client);
void zrpc_client_free(zrpc_client_t *client);

/* Last error description (thread-local-ish best effort, for diagnostics). */
const char *zrpc_client_last_error(const zrpc_client_t *client);

#ifdef __cplusplus
}
#endif

#endif /* ZRPC_CLIENT_H */
