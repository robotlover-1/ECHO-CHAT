/*
 * zrpc_server.h - zrpc v2 C server ABI (unary; stream added in Task 5).
 *
 * Scheduling: the server accept loop and per-connection readers run as NtyCo
 * coroutines on a dedicated scheduler thread (zrpc_server_serve()). Handlers
 * may run either inside a coroutine (pure-C synchronous registration) or on a
 * different thread (cgo bridge dispatches to Go); response writes are serialized
 * per connection with a mutex, so zrpc_server_send_*() is safe from any thread.
 */

#ifndef ZRPC_SERVER_H
#define ZRPC_SERVER_H

#include <stdint.h>
#include "zrpc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct zrpc_server zrpc_server_t;

/*
 * Request callback invoked for a validated, authenticated REQUEST frame.
 * request/request_len is the *business* payload JSON (verbatim), owned by the
 * library and valid only for the duration of the call. The handler must reply
 * via zrpc_server_send_response()/zrpc_server_send_error() before returning
 * (synchronous) or copy the request and reply later (asynchronous). Return 0 on
 * success; a non-zero return is treated as an internal failure reply.
 *
 *   handler_handle: opaque uint64 passed through from register (the Go bridge
 *                   uses it as a registry key; the C server never dereferences).
 *   client_fd:      connection fd, used only as a key to the send_* functions.
 *   deadline_unix_ms: request deadline in unix millis, 0 if absent.
 */
typedef int (*zrpc_request_callback_t)(uint64_t handler_handle,
                                       uint64_t request_id,
                                       int client_fd,
                                       const void *request,
                                       uint32_t request_len,
                                       uint64_t deadline_unix_ms);

typedef struct zrpc_server_options {
    const char *address;        /* "0.0.0.0:50055" or host:port */
    const char *access_token;   /* NULL disables auth checking */
    int io_timeout_ms;          /* per-connection IO budget (0 = none) */
    int max_connections;        /* 0 => default (1024) */
    int backlog;                /* 0 => SOMAXCONN */
} zrpc_server_options_t;

zrpc_server_t *zrpc_server_new(const zrpc_server_options_t *options);

/*
 * Register a handler for method. is_stream is ignored until Task 5. A method
 * may be registered once; duplicates return ZRPC_STATUS_INVALID_ARGUMENT.
 */
int zrpc_server_register(zrpc_server_t *server,
                         const char *method,
                         int is_stream,
                         uint64_t handler_handle,
                         zrpc_request_callback_t callback);

/* Bind + listen + start the NtyCo scheduler thread. Returns immediately. */
int zrpc_server_serve(zrpc_server_t *server);

/* Stop accepting and signal the scheduler to wind down (best effort). */
int zrpc_server_shutdown(zrpc_server_t *server);

/* Free everything (after shutdown). */
void zrpc_server_free(zrpc_server_t *server);

/* ---- reply APIs (thread-safe; keyed by the client_fd from the callback) ---- */

/* Reply with a business response JSON object (wrapped in {"payload": ...}). */
int zrpc_server_send_response(zrpc_server_t *server,
                              int client_fd,
                              uint64_t request_id,
                              const void *resp_json,
                              uint32_t resp_len);

/* Reply with an ERROR frame carrying a stable zrpc_status_t code. */
int zrpc_server_send_error(zrpc_server_t *server,
                           int client_fd,
                           uint64_t request_id,
                           int code,
                           const char *message);

/* ---- streaming replies (Task 5): one terminal (STREAM_END or ERROR) ---- */

/* Push one STREAM_DATA frame (data is a JSON chunk, verbatim). */
int zrpc_server_send_stream_data(zrpc_server_t *server,
                                 int client_fd,
                                 uint64_t request_id,
                                 const void *data,
                                 uint32_t data_len);

/* Close the stream with STREAM_END (empty payload). */
int zrpc_server_send_stream_end(zrpc_server_t *server,
                                int client_fd,
                                uint64_t request_id);

/*
 * Connection-close notification (Task 5): when a connection is torn down the
 * optional callback fires with client_fd so the Go bridge can cancel any
 * in-flight stream handlers. cb_handle is passed through untouched.
 */
typedef void (*zrpc_conn_close_cb_t)(uint64_t cb_handle, int client_fd);

int zrpc_server_set_conn_close_cb(zrpc_server_t *server,
                                  uint64_t cb_handle,
                                  zrpc_conn_close_cb_t cb);

#ifdef __cplusplus
}
#endif

#endif /* ZRPC_SERVER_H */
