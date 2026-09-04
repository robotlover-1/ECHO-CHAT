package zrpc_test

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"testing"

	zrpc "echo-zrpc-go"
)

/*
 * Direction-B evidence: a standalone C client process (tests/bin/ccli, built by
 * `make -C third_party/zrpc ccli`) calls a method registered from Go on the
 * C/NtyCo server. Chain: C client -> C server coroutine -> bridge -> Go handler
 * -> C send_response -> C client. cgo cannot live inside _test.go, so the C
 * client runs as a child process.
 */

func ccliPath(t *testing.T) string {
	t.Helper()
	p := "../third_party/zrpc/tests/bin/ccli"
	if _, err := os.Stat(p); err != nil {
		t.Fatalf("ccli binary missing (run make -C third_party/zrpc ccli): %v", err)
	}
	return p
}

func runCCli(t *testing.T, token, method, req string) (code int, out string) {
	t.Helper()
	cmd := exec.Command(ccliPath(t), "127.0.0.1", "19093", token, method, req)
	o, err := cmd.CombinedOutput()
	code = 0
	if cmd.ProcessState != nil {
		code = cmd.ProcessState.ExitCode()
	}
	if err != nil && code == 0 {
		t.Fatalf("ccli exec: %v out=%s", err, o)
	}
	return code, string(o)
}

func TestCCliDrivesGoHandler(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()

	// make sure the server is accepting before the child connects
	ok, err := zrpc.NewClient(zrpc.ClientOptions{Host: "127.0.0.1", Port: 19093, Token: testToken})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	_ = ok.Ping(context.Background())
	ok.Close()

	code, body := runCCli(t, testToken, "echo.add", `{"a":20,"b":22}`)
	if code != 0 {
		t.Fatalf("ccli exit=%d stderr=%s", code, body)
	}
	var s struct {
		Sum int64 `json:"sum"`
	}
	if err := json.Unmarshal([]byte(body), &s); err != nil {
		t.Fatalf("unmarshal %q: %v", body, err)
	}
	if s.Sum != 42 {
		t.Fatalf("sum=%d want 42", s.Sum)
	}
}

func TestCCliWrongToken(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	code, _ := runCCli(t, "bad-token", "echo.add", `{"a":1,"b":1}`)
	if code != int(zrpc.CodeUnauthenticated) {
		t.Fatalf("exit=%d want CodeUnauthenticated(%d)", code, int(zrpc.CodeUnauthenticated))
	}
}

func TestCCliNotFound(t *testing.T) {
	srv := startServer(t)
	defer srv.Close()
	code, _ := runCCli(t, testToken, "echo.does_not_exist", `{"a":1,"b":1}`)
	if code != int(zrpc.CodeNotFound) {
		t.Fatalf("exit=%d want CodeNotFound(%d)", code, int(zrpc.CodeNotFound))
	}
}
