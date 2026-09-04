package zrpc_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	zrpc "echo-zrpc-go"
)

const testAddr = "127.0.0.1:19093"
const testToken = "task3-test-token"

type addReq struct {
	A int64 `json:"a"`
	B int64 `json:"b"`
}
type addResp struct {
	Sum int64 `json:"sum"`
}

func startServer(t *testing.T) *zrpc.Server {
	t.Helper()
	srv, err := zrpc.NewServer(zrpc.ServerOptions{Address: testAddr, AccessToken: testToken})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	if err := srv.RegisterUnary("echo.add", func(ctx context.Context, raw json.RawMessage) (any, error) {
		var req addReq
		if err := json.Unmarshal(raw, &req); err != nil {
			return nil, zrpc.InvalidArgument(err)
		}
		return addResp{Sum: req.A + req.B}, nil
	}); err != nil {
		t.Fatalf("RegisterUnary: %v", err)
	}
	if err := srv.RegisterUnary("echo.sleepy", func(ctx context.Context, raw json.RawMessage) (any, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}); err != nil {
		t.Fatalf("RegisterUnary sleepy: %v", err)
	}
	if err := srv.RegisterUnary("echo.panic", func(ctx context.Context, raw json.RawMessage) (any, error) {
		panic("boom")
	}); err != nil {
		t.Fatalf("RegisterUnary panic: %v", err)
	}
	if err := srv.Serve(); err != nil {
		t.Fatalf("Serve: %v", err)
	}
	return srv
}

func waitReady(t *testing.T) *zrpc.Client {
	t.Helper()
	var cli *zrpc.Client
	var err error
	for i := 0; i < 200; i++ {
		cli, err = zrpc.NewClient(zrpc.ClientOptions{Host: "127.0.0.1", Port: 19093, Token: testToken})
		if err != nil {
			t.Fatalf("NewClient: %v", err)
		}
		if cli.Ping(context.Background()) == nil {
			return cli
		}
		cli.Close()
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("server not ready")
	return nil
}

// Direction A: Go (cgo) client -> C server core -> cgo -> Go handler.
func TestUnarySuccess(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	cli := waitReady(t)
	defer cli.Close()

	var resp addResp
	err := cli.Unary(context.Background(), "echo.add", &addReq{A: 40, B: 2}, &resp)
	if err != nil {
		t.Fatalf("Unary: %v", err)
	}
	if resp.Sum != 42 {
		t.Fatalf("sum = %d, want 42", resp.Sum)
	}
}

func TestPing(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	cli := waitReady(t)
	defer cli.Close()
	if err := cli.Ping(context.Background()); err != nil {
		t.Fatalf("Ping: %v", err)
	}
}

func TestNotFound(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	cli := waitReady(t)
	defer cli.Close()

	var resp addResp
	err := cli.Unary(context.Background(), "echo.missing", &addReq{A: 1, B: 1}, &resp)
	var se *zrpc.StatusError
	if !errors.As(err, &se) || se.Code != zrpc.CodeNotFound {
		t.Fatalf("err = %v, want CodeNotFound", err)
	}
}

func TestUnauthenticated(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()

	bad, err := zrpc.NewClient(zrpc.ClientOptions{Host: "127.0.0.1", Port: 19093, Token: "wrong"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	defer bad.Close()
	// wait for server readiness first via a valid client
	ok := waitReady(t)
	ok.Close()

	var resp addResp
	err = bad.Unary(context.Background(), "echo.add", &addReq{A: 1, B: 1}, &resp)
	var se *zrpc.StatusError
	if !errors.As(err, &se) || se.Code != zrpc.CodeUnauthenticated {
		t.Fatalf("err = %v, want CodeUnauthenticated", err)
	}
}

func TestDeadlineExceeded(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	cli := waitReady(t)
	defer cli.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	var resp addResp
	err := cli.Unary(ctx, "echo.sleepy", &addReq{}, &resp)
	var se *zrpc.StatusError
	if !errors.As(err, &se) || se.Code != zrpc.CodeDeadlineExceeded {
		t.Fatalf("err = %v, want CodeDeadlineExceeded", err)
	}
}

func TestHandlerPanicIsInternal(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	cli := waitReady(t)
	defer cli.Close()

	var resp addResp
	err := cli.Unary(context.Background(), "echo.panic", &addReq{}, &resp)
	var se *zrpc.StatusError
	if !errors.As(err, &se) || se.Code != zrpc.CodeInternal {
		t.Fatalf("err = %v, want CodeInternal", err)
	}
}

func TestRegisterDuplicate(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	err := srv.RegisterUnary("echo.add", func(ctx context.Context, raw json.RawMessage) (any, error) {
		return nil, nil
	})
	if err == nil {
		t.Fatalf("duplicate register should fail")
	}
}

func TestConcurrentUnary(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()

	const workers = 16
	const per = 100
	var wg sync.WaitGroup
	errCh := make(chan error, workers)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			cli, err := zrpc.NewClient(zrpc.ClientOptions{Host: "127.0.0.1", Port: 19093, Token: testToken})
			if err != nil {
				errCh <- err
				return
			}
			defer cli.Close()
			for i := 0; i < per; i++ {
				var resp addResp
				if err := cli.Unary(context.Background(), "echo.add", &addReq{A: 1, B: int64(i)}, &resp); err != nil {
					errCh <- fmt.Errorf("i=%d: %w", i, err)
					return
				}
				if resp.Sum != 1+int64(i) {
					errCh <- fmt.Errorf("sum=%d", resp.Sum)
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatalf("concurrent unary: %v", err)
	}
}

// Handle cleanup: after Close the Go-side registry must be empty for this server.
func TestHandleCleanupOnClose(t *testing.T) {
	srv := startServer(t)
	cli := waitReady(t)
	var resp addResp
	if err := cli.Unary(context.Background(), "echo.add", &addReq{A: 5, B: 5}, &resp); err != nil {
		t.Fatalf("Unary: %v", err)
	}
	if resp.Sum != 10 {
		t.Fatalf("sum=%d", resp.Sum)
	}
	if n := srv.RegisteredCount(); n == 0 {
		t.Fatalf("expected registered handles before Close")
	}
	cli.Close()
	srv.Close()
	if n := srv.RegisteredCount(); n != 0 {
		t.Fatalf("registry not empty after Close: %d", n)
	}
}
