/*
 * ccli.c - minimal C client CLI used by the zrpc-go integration tests to prove
 * that a pure C client can drive a Go-registered handler through the C server.
 *
 * Usage: ccli <host> <port> <token> <method> <req-json>
 * On success prints the raw business response to stdout and exits 0.
 * On error prints the C-level message to stderr and exits with the status code
 * (0..10, matching zrpc_status_t).
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "zrpc.h"

int main(int argc, char **argv)
{
    if (argc < 6) {
        fprintf(stderr, "usage: %s <host> <port> <token> <method> <req-json>\n", argv[0]);
        return 2;
    }
    const char *host = argv[1];
    int port = atoi(argv[2]);
    const char *token = argv[3][0] ? argv[3] : NULL;
    const char *method = argv[4];
    const char *req = argv[5];

    zrpc_client_t *c = zrpc_client_new(host, (uint16_t)port, token, 1000, 3000);
    if (!c) {
        fprintf(stderr, "client_new failed\n");
        return 2;
    }

    zrpc_buffer_t resp = { NULL, 0, 0 };
    int st = zrpc_client_call_unary(c, method, req, (uint32_t)strlen(req), 0, &resp);
    if (st != ZRPC_STATUS_OK) {
        fprintf(stderr, "%s\n", zrpc_client_last_error(c));
        zrpc_client_free(c);
        return st == 0 ? 1 : st;   /* exit with the wire status code */
    }

    fwrite(resp.data, 1, resp.len, stdout);
    fflush(stdout);
    zrpc_buffer_free(&resp);
    zrpc_client_free(c);
    return 0;
}
