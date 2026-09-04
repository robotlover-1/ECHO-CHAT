package server

import (
	"context"
	"fmt"
	"net"
	"os"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	zrpc "echo-zrpc-go"
	"echo-zrpc-go/contract"
	"keywords-filter/pkg/filter"
	"keywords-filter/proto"
)

func findDict() string {
	for _, p := range []string{"dict.txt", "../dict.txt", "../../dict.txt", "../../../dict.txt"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return "dict.txt"
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("probe listen: %v", err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	return port
}

func retryCall(t *testing.T, what string, fn func() error) {
	t.Helper()
	var last error
	for i := 0; i < 200; i++ {
		if last = fn(); last == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("%s never succeeded: %v", what, last)
}

// Golden parity: the SAME real filter instance served over gRPC and zrpc must
// agree with each other and with a direct call, for a battery of inputs.
func TestTransportParityGolden(t *testing.T) {
	filter.InitFilter(findDict())
	f := filter.GetFilter()

	// ---- gRPC server (ephemeral port, no interceptor needed) ----
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("grpc listen: %v", err)
	}
	grpcAddr := lis.Addr().String()
	gs := grpc.NewServer()
	grpcSvc := NewFilterService(f)
	proto.RegisterFilterServer(gs, grpcSvc)
	go func() { _ = gs.Serve(lis) }()
	defer gs.Stop()

	conn, err := grpc.Dial(grpcAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("grpc dial: %v", err)
	}
	defer conn.Close()
	gcli := proto.NewFilterClient(conn)

	// ---- zrpc server (Bearer token "ptok") ----
	zport := freePort(t)
	zsrv, err := zrpc.NewServer(zrpc.ServerOptions{
		Address:     fmt.Sprintf("127.0.0.1:%d", zport),
		AccessToken: "ptok",
	})
	if err != nil {
		t.Fatalf("zrpc NewServer: %v", err)
	}
	if err := grpcSvc.RegisterZRPC(zsrv); err != nil {
		t.Fatalf("RegisterZRPC: %v", err)
	}
	if err := zsrv.Serve(); err != nil {
		t.Fatalf("zrpc Serve: %v", err)
	}

	zcli, err := zrpc.NewClient(zrpc.ClientOptions{Host: "127.0.0.1", Port: zport, Token: "ptok"})
	if err != nil {
		t.Fatalf("zrpc NewClient: %v", err)
	}
	defer zcli.Close()

	battery := []string{
		"你好",
		"我们赌博不好",
		" 赌 ",
		"毒品危害健康 赌博伤身",
		"完全正常的句子，讨论天气",
		"中国 你好 谢谢",
		"",
		"the quick brown fox",
	}

	for _, in := range battery {
		// direct reference
		dok, dword := f.Validate(in)
		dwords := f.FindAll(in)

		// gRPC
		var gok bool
		var gword string
		var gwords []string
		retryCall(t, "grpc-validate", func() error {
			res, err := gcli.Validate(context.Background(), &proto.FilterReq{Text: in})
			if err != nil {
				return err
			}
			gok, gword = res.Ok, res.Keyword
			return nil
		})
		retryCall(t, "grpc-findall", func() error {
			res, err := gcli.FindAll(context.Background(), &proto.FilterReq{Text: in})
			if err != nil {
				return err
			}
			gwords = res.Keywords
			return nil
		})
		if gok != dok || gword != dword {
			t.Errorf("gRPC Validate(%q)=(%v,%q) direct=(%v,%q)", in, gok, gword, dok, dword)
		}
		if fmt.Sprint(gwords) != fmt.Sprint(dwords) {
			t.Errorf("gRPC FindAll(%q)=%v direct=%v", in, gwords, dwords)
		}

		// zrpc
		var vres contract.ValidateResponse
		err = zcli.Unary(context.Background(), contract.MethodFilterValidate,
			&contract.FilterRequest{Text: in}, &vres)
		if err != nil {
			t.Fatalf("zrpc Validate(%q): %v", in, err)
		}
		if vres.OK != dok || vres.Keyword != dword {
			t.Errorf("zrpc Validate(%q)=(%v,%q) direct=(%v,%q)", in, vres.OK, vres.Keyword, dok, dword)
		}
		var fres contract.FindAllResponse
		if err := zcli.Unary(context.Background(), contract.MethodFilterFindAll,
			&contract.FilterRequest{Text: in}, &fres); err != nil {
			t.Fatalf("zrpc FindAll(%q): %v", in, err)
		}
		if fmt.Sprint(fres.Keywords) != fmt.Sprint(dwords) {
			t.Errorf("zrpc FindAll(%q)=%v direct=%v", in, fres.Keywords, dwords)
		}
	}
	t.Logf("parity ok: gRPC == zrpc == direct for %d golden inputs", len(battery))
}
