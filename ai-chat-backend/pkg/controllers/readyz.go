package controllers

import (
	"context"
	"net/http"
	"sort"
	"sync"
	"time"

	"ai-chat-backend/pkg/log"
	"ai-chat-backend/pkg/config"
	mysqlpkg "ai-chat-backend/pkg/db/mysql"
	redisclient "ai-chat-backend/pkg/db/redis"
	ai_chat_service "ai-chat-backend/services/ai-chat-service"

	"github.com/gin-gonic/gin"
)

// depCheck 每次只输出 ok/degraded/fail 三态，细粒度错误只写日志，不暴露地址/凭据。
type depCheck struct {
	status string
}

func depProbe(name string, fn func(context.Context) error, logger log.ILogger) string {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := fn(ctx); err != nil {
		logger.ErrorF("readyz %s: %v", name, err)
		return "fail"
	}
	return "ok"
}

func defaultReadyzCheckers() map[string]func(context.Context) error {
	return map[string]func(context.Context) error{
		"mysql": func(ctx context.Context) error {
			return mysqlpkg.GetDB().PingContext(ctx)
		},
		"kvstore": func(ctx context.Context) error {
			return redisclient.GetPool().Ping(ctx).Err()
		},
		"tokenizer": func(ctx context.Context) error {
			return tokenizerReachable(ctx)
		},
		"ai-chat-service": ai_chat_service.Ping,
	}
}

// ReadyzWith 可注入 checker 的通用 handler（便于单测）。
func ReadyzWith(checkers map[string]func(context.Context) error) gin.HandlerFunc {
	logger := log.NewLogger()
	return func(c *gin.Context) {
		results := make(map[string]string, len(checkers))
		var mu sync.Mutex
		var wg sync.WaitGroup
		for name, fn := range checkers {
			wg.Add(1)
			go func(name string, fn func(context.Context) error) {
				defer wg.Done()
				st := depProbe(name, fn, logger)
				mu.Lock()
				results[name] = st
				mu.Unlock()
			}(name, fn)
		}
		wg.Wait()

		names := make([]string, 0, len(results))
		allOK := true
		for n, st := range results {
			names = append(names, n)
			if st != "ok" {
				allOK = false
			}
		}
		sort.Strings(names)

		data := make([]gin.H, 0, len(names))
		for _, n := range names {
			data = append(data, gin.H{"name": n, "status": results[n]})
		}
		if !allOK {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "Fail", "data": data})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "Success", "data": data})
	}
}

// ReadyzHandler 生产实现：全依赖并行探测。
func ReadyzHandler() gin.HandlerFunc {
	return ReadyzWith(defaultReadyzCheckers())
}

// tokenizerReachable：tokenizer 无 /health 路由，做 HTTP 可达性探测即可。
func tokenizerReachable(ctx context.Context) error {
	addr := config.GetConfig().Tokenizer.Address
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, addr, nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}
