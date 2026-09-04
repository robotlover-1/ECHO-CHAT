package zrpc

/*
#cgo CFLAGS: -I${SRCDIR}/../third_party/zrpc/include
#cgo LDFLAGS: ${SRCDIR}/../third_party/zrpc/build/libzrpc.a -lpthread -ldl
#include <stdlib.h>
#include "zrpc.h"
*/
import "C"

import (
	"context"
	"encoding/json"
	"fmt"
	"runtime"
	"sync"
	"unsafe"
)

// ClientOptions configures a zrpc unary client. Each Client owns one reusable
// TCP connection; create one per worker when you need parallelism.
type ClientOptions struct {
	Host             string
	Port             int
	Token            string // optional; omitted when empty
	ConnectTimeoutMs int    // 0 => default
	IOTimeoutMs      int    // 0 => default 30s
}

// Client is a thin cgo wrapper over the C zrpc unary client.
type Client struct {
	mu               sync.Mutex
	ptr              *C.zrpc_client_t
	host             string
	port             int
	token            string
	connectTimeoutMs int
	ioTimeoutMs      int
}

// NewClient connects lazily on the first call (the C client dials on demand).
func NewClient(opts ClientOptions) (*Client, error) {
	if opts.Host == "" {
		return nil, fmt.Errorf("zrpc: empty host")
	}
	c := &Client{
		host:             opts.Host,
		port:             opts.Port,
		token:            opts.Token,
		connectTimeoutMs: opts.ConnectTimeoutMs,
		ioTimeoutMs:      opts.IOTimeoutMs,
	}
	ch := C.CString(opts.Host)
	var tok *C.char
	if opts.Token != "" {
		tok = C.CString(opts.Token)
	}
	c.ptr = C.zrpc_client_new(ch, C.uint16_t(opts.Port), tok,
		C.int(opts.ConnectTimeoutMs), C.int(opts.IOTimeoutMs))
	C.free(unsafe.Pointer(ch))
	if tok != nil {
		C.free(unsafe.Pointer(tok))
	}
	if c.ptr == nil {
		return nil, fmt.Errorf("zrpc: client_new failed")
	}
	return c, nil
}

func deadlineUnixMs(ctx context.Context) uint64 {
	if dl, ok := ctx.Deadline(); ok {
		if m := dl.UnixMilli(); m > 0 {
			return uint64(m)
		}
	}
	return 0
}

func marshalReq(req any) ([]byte, error) {
	if req == nil {
		return nil, nil
	}
	b, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	return b, nil
}

// Unary performs a blocking unary call: method over req, result unmarshalled
// into resp. The wire code is mapped to a *StatusError on failure.
func (c *Client) Unary(ctx context.Context, method string, req, resp any) error {
	body, err := marshalReq(req)
	if err != nil {
		return err
	}

	cm := C.CString(method)
	defer C.free(unsafe.Pointer(cm))

	var cReq unsafe.Pointer
	var cLen C.uint32_t
	if len(body) > 0 {
		cReq = unsafe.Pointer(&body[0])
		cLen = C.uint32_t(len(body))
	}

	var out C.zrpc_buffer_t
	st := C.zrpc_client_call_unary(c.ptr, cm, cReq, cLen,
		C.uint64_t(deadlineUnixMs(ctx)), &out)
	runtime.KeepAlive(body)

	if st != C.ZRPC_STATUS_OK {
		msg := C.GoString(C.zrpc_client_last_error(c.ptr))
		return &StatusError{Code: Code(int(st)), Message: msg}
	}

	data := C.GoBytes(unsafe.Pointer(out.data), C.int(out.len))
	C.zrpc_buffer_free(&out)
	if resp == nil {
		return nil
	}
	if err := json.Unmarshal(data, resp); err != nil {
		return fmt.Errorf("zrpc: unmarshal response: %w", err)
	}
	return nil
}

// Ping checks connectivity with a PING/PONG round trip.
func (c *Client) Ping(ctx context.Context) error {
	_ = ctx
	st := C.zrpc_client_ping(c.ptr, C.int(2000))
	if st != C.ZRPC_STATUS_OK {
		msg := C.GoString(C.zrpc_client_last_error(c.ptr))
		return &StatusError{Code: Code(int(st)), Message: msg}
	}
	return nil
}

// Close frees the C client and its connection.
func (c *Client) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.ptr != nil {
		C.zrpc_client_free(c.ptr)
		c.ptr = nil
	}
	return nil
}
