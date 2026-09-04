/*
 * bridge.c - see bridge.h. Kept dependency-free except libzrpc.
 */

#include <stdlib.h>
#include <string.h>

#include "bridge.h"

/* Implemented by Go via //export; signatures must match the cgo-generated ones
 * (non-const void*). */
void goZRPCDispatchRequest(uint64_t handler_handle, uint64_t request_id,
                           int client_fd, void *data, uint32_t data_len,
                           uint64_t deadline_unix_ms);
void goZRPCOnStreamEvent(uint64_t callback_handle, uint64_t request_id,
                         int event_type, int status, void *data, uint32_t data_len);
void goZRPCOnConnClosed(int client_fd);

int zrpc_bridge_register(zrpc_server_t *server, const char *method, int is_stream,
                         uint64_t handler_handle)
{
    return zrpc_server_register(server, method, is_stream, handler_handle,
                                zrpc_bridge_server_cb);
}

static void zrpc_bridge_conn_close(uint64_t cb_handle, int client_fd)
{
    (void)cb_handle;
    goZRPCOnConnClosed(client_fd);
}

int zrpc_bridge_set_conn_close(zrpc_server_t *server)
{
    return zrpc_server_set_conn_close_cb(server, 0, zrpc_bridge_conn_close);
}

static void zrpc_bridge_stream_cb(uint64_t callback_handle, uint64_t request_id,
                                  int event_type, int status,
                                  const void *data, uint32_t data_len)
{
    goZRPCOnStreamEvent(callback_handle, request_id, event_type, status,
                        (void *)data, data_len);
}

int zrpc_bridge_call_stream(zrpc_client_t *client, const char *method,
                            const void *request, uint32_t request_len,
                            uint64_t deadline_unix_ms, uint64_t callback_handle)
{
    return zrpc_client_call_stream(client, method, request, request_len,
                                   deadline_unix_ms, callback_handle,
                                   zrpc_bridge_stream_cb);
}

void zrpc_bridge_cancel(zrpc_client_t *client)
{
    zrpc_client_cancel(client);
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
