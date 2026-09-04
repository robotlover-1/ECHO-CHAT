package keywords_filter

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"testing"

	zrpc "echo-zrpc-go"
	"echo-zrpc-go/contract"
)

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	p := l.Addr().(*net.TCPAddr).Port
	l.Close()
	return p
}

// Plumbing test: chat-service's zrpc filter client drives an in-process zrpc
// server with stub handlers (real-filter parity lives in keywords-filter).
func TestZRPCFilterClientPlumbing(t *testing.T) {
	port := freePort(t)
	zsrv, err := zrpc.NewServer(zrpc.ServerOptions{
		Address:     fmt.Sprintf("127.0.0.1:%d", port),
		AccessToken: "tok",
	})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	if err := zsrv.RegisterUnary(contract.MethodFilterValidate,
		func(_ context.Context, raw json.RawMessage) (any, error) {
			var req contract.FilterRequest
			_ = json.Unmarshal(raw, &req)
			return contract.ValidateResponse{OK: true, Keyword: "赌"}, nil
		}); err != nil {
		t.Fatalf("register validate: %v", err)
	}
	if err := zsrv.RegisterUnary(contract.MethodFilterFindAll,
		func(_ context.Context, raw json.RawMessage) (any, error) {
			var req contract.FilterRequest
			_ = json.Unmarshal(raw, &req)
			return contract.FindAllResponse{Keywords: []string{"赌"}}, nil
		}); err != nil {
		t.Fatalf("register findall: %v", err)
	}
	if err := zsrv.Serve(); err != nil {
		t.Fatalf("Serve: %v", err)
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)

	ok, word, err := ZRPCValidate(context.Background(), addr, "tok", "我们赌博")
	if err != nil || !ok || word != "赌" {
		t.Fatalf("ZRPCValidate = (%v,%q,%v), want (true,赌,nil)", ok, word, err)
	}
	words, err := ZRPCFindAll(context.Background(), addr, "tok", "赌博 毒品")
	if err != nil || fmt.Sprint(words) != "[赌]" {
		t.Fatalf("ZRPCFindAll = (%v,%v), want ([赌],nil)", words, err)
	}

	// wrong token must error (upstream treats it fail-closed / fail-open by call site)
	if _, _, err := ZRPCValidate(context.Background(), addr, "bad", "x"); err == nil {
		t.Fatalf("expected error for wrong token")
	}
}
