/*
 * bridge.c - see bridge.h. Kept dependency-free except libzrpc.
 */

#include <stdlib.h>
#include <string.h>

#include "bridge.h"

/* Implemented by Go via //export; signature must match the cgo-generated one
 * (non-const void*). */
void goZRPCDispatchRequest(uint64_t handler_handle, uint64_t request_id,
                           int client_fd, void *data, uint32_t data_len,
                           uint64_t deadline_unix_ms);

int zrpc_bridge_register(zrpc_server_t *server, const char *method, int is_stream,
                         uint64_t handler_handle)
{
    return zrpc_server_register(server, method, is_stream, handler_handle,
                                zrpc_bridge_server_cb);
}

int zrpc_bridge_server_cb(uint64_t handler_handle, uint64_t request_id,
                          int client_fd, const void *request, uint32_t request_len,
                          uint64_t deadline_unix_ms)
{
    void *copy = NULL;
    if (request_len > 0) {
        copy = malloc(request_len);
        if (copy == NULL)
            return ZRPC_STATUS_RESOURCE_EXHAUSTED;
        memcpy(copy, request, request_len);
    }

    /* Go must copy data during this synchronous call (it does via C.GoBytes). */
    goZRPCDispatchRequest(handler_handle, request_id, client_fd,
                          copy, request_len, deadline_unix_ms);

    free(copy);
    return 0;
}
