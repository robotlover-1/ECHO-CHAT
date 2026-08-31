package middlewares

import (
	"context"
	"errors"
	"net/http"

	"ai-chat-backend/pkg/config"
	kredis "ai-chat-backend/pkg/db/redis"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

func AuthMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		if !config.GetConfig().Auth.Enabled {
			c.Next()
			return
		}
		token := c.GetHeader("Authorization")
		deviceID, err := kredis.Get(context.Background(), "session:"+token)
		if err != nil {
			if errors.Is(err, redis.Nil) {
				c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"status": "Fail", "message": "未登录或登录已过期", "data": nil})
			} else {
				c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"status": "Fail", "message": "会话服务异常，请稍后重试", "data": nil})
			}
			return
		}
		c.Set("device_id", deviceID)
		c.Next()
	}
}
