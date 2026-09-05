package controllers

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestReadyzAllOK(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	engine.GET("/api/readyz", ReadyzWith(map[string]func(context.Context) error{
		"mysql":         func(context.Context) error { return nil },
		"kvstore":       func(context.Context) error { return nil },
		"tokenizer":     func(context.Context) error { return nil },
		"ai-chat-service": func(context.Context) error { return nil },
	}))
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/readyz", nil)
	engine.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("code = %d, want 200; body=%s", w.Code, w.Body.String())
	}
}

func TestReadyzFailsWhenDepDown(t *testing.T) {
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	engine.GET("/api/readyz", ReadyzWith(map[string]func(context.Context) error{
		"mysql": func(context.Context) error { return nil },
		"kvstore": func(context.Context) error { return errors.New("down") },
	}))
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/readyz", nil)
	engine.ServeHTTP(w, req)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("code = %d, want 503; body=%s", w.Code, w.Body.String())
	}
	if got := w.Body.String(); !strings.Contains(got, "kvstore") {
		t.Fatalf("body should mention failing dep; got %s", got)
	}
	// 不得泄露内部地址/凭据
	for _, banned := range []string{"token", "dsn", "tcp(", "50055"} {
		if strings.Contains(w.Body.String(), banned) {
			t.Fatalf("body leaks %q: %s", banned, w.Body.String())
		}
	}
}
