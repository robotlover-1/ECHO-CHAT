/*
 * zrpc.h - public umbrella header for the zrpc v2 C library.
 *
 * cgo consumers and C clients/servers include this single header. Today it
 * exposes the wire protocol (zrpc_protocol.h); zrpc_client.h / zrpc_server.h
 * ABI headers are added as their C implementations land (Task 2+).
 */

#ifndef ZRPC_H
#define ZRPC_H

#include "zrpc_protocol.h"
#include "zrpc_client.h"
#include "zrpc_server.h"

#endif /* ZRPC_H */
