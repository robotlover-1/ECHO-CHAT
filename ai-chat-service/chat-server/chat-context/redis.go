package chat_context

import (
	predis "ai-chat-service/pkg/db/redis"
	"context"
	"encoding/json"
	"github.com/redis/go-redis/v9"
	"time"
)

type redisCache struct {
	redisClient *redis.Client
}

func NewRedisCache() ContextCache {
	pool := predis.GetPool()
	return &redisCache{
		redisClient: pool.Get(),
	}
}
func getRedisKey(key string) string {
	return predis.GetKey(key)
}

func (c *redisCache) Get(key string) (*ChatMessage, error) {
	key = getRedisKey(key)
	str, err := c.redisClient.Get(context.Background(), key).Result()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	value := &ChatMessage{}
	err = json.Unmarshal([]byte(str), value)
	return value, err
}
func (c *redisCache) Set(key string, value *ChatMessage, ttl int) error {
	key = getRedisKey(key)
	bytes, err := json.Marshal(value)
	if err != nil {
		return err
	}
	str := string(bytes)
	return c.redisClient.SetEx(context.Background(), key, str, time.Duration(ttl)*time.Second).Err()
}
func (c *redisCache) Close() {
	pool := predis.GetPool()
	pool.Put(c.redisClient)
}
