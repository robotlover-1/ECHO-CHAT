/*
 * bridge.h - C shim between libzrpc.a and the Go bridge (echo-zrpc-go).
 *
 * The C server invokes a registered zrpc_request_callback_t inside its NtyCo
 * coroutine. zrpc_bridge_server_cb is that callback for every method: it copies
 * the request bytes and calls back into Go (goZRPCDispatchRequest, defined in
 * server.go via //export) which hands the job to a Go worker. The callback never
 * blocks the coroutine and never holds a C buffer after the call.
 */

#ifndef ZRPC_BRIDGE_H
#define ZRPC_BRIDGE_H

#include <stdint.h>
#include "zrpc.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Register method with the C server using zrpc_bridge_server_cb. */
int zrpc_bridge_register(zrpc_server_t *server, const char *method, int is_stream,
                         uint64_t handler_handle);

/*
 * Called by libzrpc on a validated REQUEST frame. Copies the payload and calls
 * the Go export goZRPCDispatchRequest; the Go side must copy data during the
 * call (it does) and the copy is freed here before returning.
 */
int zrpc_bridge_server_cb(uint64_t handler_handle, uint64_t request_id,
                          int client_fd, const void *request, uint32_t request_len,
                          uint64_t deadline_unix_ms);

/* goZRPCDispatchRequest is implemented by Go (server.go //export); cgo emits the
 * prototype into its generated export header. bridge.c keeps a local matching
 * declaration. */

#ifdef __cplusplus
}
#endif

#endif /* ZRPC_BRIDGE_H */
