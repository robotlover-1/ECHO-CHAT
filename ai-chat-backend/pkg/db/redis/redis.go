package redis

import (
	"context"
	"fmt"
	"sync"
	"time"

	"ai-chat-backend/pkg/config"

	"github.com/redis/go-redis/v9"
)

var (
	pool *redis.Client
	once sync.Once
)

func GetPool() *redis.Client {
	once.Do(func() {
		cnf := config.GetConfig()
		pool = redis.NewClient(&redis.Options{
			Addr:     fmt.Sprintf("%s:%d", cnf.Redis.Host, cnf.Redis.Port),
			Password: cnf.Redis.Pwd,
		})
	})
	return pool
}

func SetEx(ctx context.Context, key, value string, ttl time.Duration) error {
	return GetPool().SetEx(ctx, key, value, ttl).Err()
}

func Get(ctx context.Context, key string) (string, error) {
	return GetPool().Get(ctx, key).Result()
}
