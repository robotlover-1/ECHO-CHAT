package middlewares

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/gin-gonic/gin"
)

// 每注册一个 /ip{n} 路由（同一 engine 多次注册相同 path 会 panic）。
var ipRouteID struct {
	sync.Mutex
	n int
}

// clientIP 请求 "X-Forwarded-For" 场景下 gin.ClientIP 的解析结果。
func ipOf(t *testing.T, engine *gin.Engine, remoteAddr string, xff string) string {
	t.Helper()
	gin.SetMode(gin.TestMode)
	ipRouteID.Lock()
	ipRouteID.n++
	path := fmt.Sprintf("/ip%d", ipRouteID.n)
	ipRouteID.Unlock()
	engine.GET(path, func(c *gin.Context) {
		c.JSON(200, gin.H{"ip": c.ClientIP()})
	})
	req := httptest.NewRequest(http.MethodGet, path, nil)
	if remoteAddr != "" {
		req.RemoteAddr = remoteAddr
	}
	if xff != "" {
		req.Header.Set("X-Forwarded-For", xff)
	}
	w := httptest.NewRecorder()
	engine.ServeHTTP(w, req)
	var body struct {
		IP string `json:"ip"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal body %q: %v", w.Body.String(), err)
	}
	return body.IP
}

func TestNewEngineDefaultsToLoopbackTrust(t *testing.T) {
	engine, err := NewEngine(nil) // 默认信任 127.0.0.1/::1
	if err != nil {
		t.Fatalf("NewEngine(nil) err = %v", err)
	}
	// 可信回环一跳 + XFF → 采用 XFF（真实客户端，由 Nginx 覆盖写入）
	if got := ipOf(t, engine, "127.0.0.1:5555", "203.0.113.7"); got != "203.0.113.7" {
		t.Fatalf("loopback trusted: got %s, want 203.0.113.7", got)
	}
	// 非可信对端 + 伪造 XFF → 忽略 XFF，采用对端地址
	if got := ipOf(t, engine, "10.9.9.9:9999", "6.6.6.6"); got != "10.9.9.9" {
		t.Fatalf("untrusted peer spoof: got %s, want 10.9.9.9", got)
	}
	// 多级代理（右起第一个非可信地址为真实客户端）
	if got := ipOf(t, engine, "127.0.0.1:5555", "203.0.113.7, 10.1.1.1"); got != "10.1.1.1" {
		t.Fatalf("multi proxy: got %s, want 10.1.1.1", got)
	}
	// IPv6 回环可信
	if got := ipOf(t, engine, "[::1]:5555", "2001:db8::1"); got != "2001:db8::1" {
		t.Fatalf("ipv6 loopback: got %s, want 2001:db8::1", got)
	}
}

func TestNewEngineExplicitTrust(t *testing.T) {
	engine, err := NewEngine([]string{"10.0.0.0/8"})
	if err != nil {
		t.Fatalf("NewEngine err = %v", err)
	}
	// 10.x 对端可信 → 采用 XFF
	if got := ipOf(t, engine, "10.1.2.3:1234", "198.51.100.1"); got != "198.51.100.1" {
		t.Fatalf("explicit trust: got %s, want 198.51.100.1", got)
	}
}

func TestNewEngineRejectsInvalid(t *testing.T) {
	if _, err := NewEngine([]string{"not-an-ip"}); err == nil {
		t.Fatal("NewEngine invalid proxy should error")
	}
}
