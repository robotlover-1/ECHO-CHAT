package keywords_filter

import (
	"context"
	"fmt"
	"net"
	"sync"
	"time"

	"ai-chat-service/pkg/log"
	zrpc "echo-zrpc-go"
	"echo-zrpc-go/contract"
)

/*
 * zrpc v2 filter client used by chat-server when dependOn.<svc>.transport is
 * "zrpc". One C-backed connection is kept per (address|token); calls on a
 * connection are serialised because a zrpc.Client owns a single stream. The
 * unary connection pool from the plan is a later-stage refinement.
 *
 * Error/timeout policy mirrors the gRPC path:
 *   - sensitive (Validate) failure is returned to the caller (fail-closed);
 *   - keywords (FindAll) failure is turned into an empty list by the caller.
 */

type zrpcConn struct {
	cli *zrpc.Client
	mu  sync.Mutex
}

var (
	connsMu sync.Mutex
	conns   = map[string]*zrpcConn{}
)

func getConn(addr, token string) (*zrpcConn, error) {
	key := addr + "|" + token
	connsMu.Lock()
	defer connsMu.Unlock()
	if c := conns[key]; c != nil {
		return c, nil
	}
	host, portStr, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("keywords-filter zrpc: bad address %q: %w", addr, err)
	}
	var port int
	if _, err := fmt.Sscanf(portStr, "%d", &port); err != nil {
		return nil, fmt.Errorf("keywords-filter zrpc: bad port %q: %w", portStr, err)
	}
	cli, err := zrpc.NewClient(zrpc.ClientOptions{Host: host, Port: port, Token: token})
	if err != nil {
		return nil, err
	}
	c := &zrpcConn{cli: cli}
	conns[key] = c
	return c, nil
}

func (c *zrpcConn) unary(ctx context.Context, method string, req, resp any) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, 800*time.Millisecond)
	defer cancel()
	return c.cli.Unary(ctx, method, req, resp)
}

// ZRPCValidate calls the sensitive filter over zrpc v2.
func ZRPCValidate(ctx context.Context, addr, token, text string) (bool, string, error) {
	c, err := getConn(addr, token)
	if err != nil {
		log.Error(err)
		return false, "", err
	}
	var resp contract.ValidateResponse
	if err := c.unary(ctx, contract.MethodFilterValidate, &contract.FilterRequest{Text: text}, &resp); err != nil {
		return false, "", err
	}
	return resp.OK, resp.Keyword, nil
}

// ZRPCFindAll calls the keywords filter over zrpc v2.
func ZRPCFindAll(ctx context.Context, addr, token, text string) ([]string, error) {
	c, err := getConn(addr, token)
	if err != nil {
		log.Error(err)
		return nil, err
	}
	var resp contract.FindAllResponse
	if err := c.unary(ctx, contract.MethodFilterFindAll, &contract.FilterRequest{Text: text}, &resp); err != nil {
		return nil, err
	}
	if resp.Keywords == nil {
		return []string{}, nil
	}
	return resp.Keywords, nil
}
