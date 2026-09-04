/*
 * zrpc_error.c - stable status-code to short message mapping.
 *
 * Only the numeric code is part of the wire/ABI contract; strings are for logs
 * and user-facing text, never parsed on the wire.
 */

#include "zrpc_protocol.h"

const char *zrpc_status_str(int status)
{
    switch (status) {
    case ZRPC_STATUS_OK:                 return "ok";
    case ZRPC_STATUS_CANCELLED:          return "cancelled";
    case ZRPC_STATUS_INVALID_ARGUMENT:   return "invalid argument";
    case ZRPC_STATUS_UNAUTHENTICATED:    return "unauthenticated";
    case ZRPC_STATUS_NOT_FOUND:          return "not found";
    case ZRPC_STATUS_DEADLINE_EXCEEDED:  return "deadline exceeded";
    case ZRPC_STATUS_RESOURCE_EXHAUSTED: return "resource exhausted";
    case ZRPC_STATUS_UNAVAILABLE:        return "unavailable";
    case ZRPC_STATUS_INTERNAL:           return "internal";
    case ZRPC_STATUS_PROTOCOL_ERROR:     return "protocol error";
    case ZRPC_STATUS_FRAME_TOO_LARGE:    return "frame too large";
    default:                             return "unknown";
    }
}
