package zrpc_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"testing"
	"time"

	zrpc "echo-zrpc-go"
)

func freePortGR(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	p := l.Addr().(*net.TCPAddr).Port
	l.Close()
	return p
}

func threadCount() int {
	es, _ := os.ReadDir("/proc/self/task")
	return len(es)
}

// Close must join the NtyCo scheduler thread: thread count returns to baseline,
// the registry empties, and a fresh server can start right after.
func TestServerGracefulCloseNoThreadLeak(t *testing.T) {
	base := threadCount()

	port := freePortGR(t)
	srv, err := zrpc.NewServer(zrpc.ServerOptions{Address: fmt.Sprintf("127.0.0.1:%d", port), AccessToken: "tok"})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	if err := srv.RegisterUnary("t.echo", func(ctx context.Context, raw json.RawMessage) (any, error) {
		return map[string]string{"pong": "ok"}, nil
	}); err != nil {
		t.Fatalf("RegisterUnary: %v", err)
	}
	if err := srv.Serve(); err != nil {
		t.Fatalf("Serve: %v", err)
	}
	// scheduler thread is up
	for i := 0; i < 100 && threadCount() <= base; i++ {
		time.Sleep(10 * time.Millisecond)
	}
	if threadCount() <= base {
		t.Fatalf("scheduler thread did not appear (base=%d now=%d)", base, threadCount())
	}

	cli, err := zrpc.NewClient(zrpc.ClientOptions{Host: "127.0.0.1", Port: port, Token: "tok"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	var resp map[string]string
	if err := cli.Unary(context.Background(), "t.echo", map[string]string{"x": "1"}, &resp); err != nil {
		t.Fatalf("Unary: %v", err)
	}
	cli.Close()

	if n := srv.RegisteredCount(); n == 0 {
		t.Fatalf("expected registered handle before Close")
	}

	done := make(chan error, 1)
	go func() { done <- srv.Close() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Close: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatalf("Close hung (scheduler thread did not exit)")
	}

	if n := srv.RegisteredCount(); n != 0 {
		t.Fatalf("registry not empty after Close: %d", n)
	}
	// allow the scheduler thread a moment to fully wind down
	deadline := time.Now().Add(5 * time.Second)
	for threadCount() > base && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if now := threadCount(); now > base {
		t.Fatalf("thread leaked: base=%d now=%d", base, now)
	}

	// a fresh server must start cleanly afterwards
	srv2, err := zrpc.NewServer(zrpc.ServerOptions{Address: fmt.Sprintf("127.0.0.1:%d", freePortGR(t)), AccessToken: "tok"})
	if err != nil {
		t.Fatalf("NewServer#2: %v", err)
	}
	if err := srv2.RegisterUnary("t.echo", func(ctx context.Context, raw json.RawMessage) (any, error) {
		return map[string]string{"pong": "ok"}, nil
	}); err != nil {
		t.Fatalf("RegisterUnary#2: %v", err)
	}
	if err := srv2.Serve(); err != nil {
		t.Fatalf("Serve#2: %v", err)
	}
	if err := srv2.Close(); err != nil {
		t.Fatalf("Close#2: %v", err)
	}
	if now := threadCount(); now > base {
		t.Fatalf("thread leaked after second close: base=%d now=%d", base, now)
	}
}
