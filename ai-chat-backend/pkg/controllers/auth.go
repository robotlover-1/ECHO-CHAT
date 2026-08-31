package controllers

import (
	"context"
	"errors"
	"time"

	"ai-chat-backend/pkg/config"
	kredis "ai-chat-backend/pkg/db/redis"
	"ai-chat-backend/pkg/users"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// Login: 免密自动登录。前端首访生成 device_id（UUID 存 localStorage），后端 upsert 用户并下发 session token。
func (chat *ChatService) Login(ctx *gin.Context) {
	var req struct {
		DeviceID string `json:"device_id"`
	}
	if err := ctx.BindJSON(&req); err != nil || req.DeviceID == "" {
		ctx.JSON(400, gin.H{"status": "Fail", "message": "device_id 不能为空", "data": nil})
		return
	}
	user, err := users.UpsertByDeviceID(req.DeviceID, config.GetConfig().Auth.InitQuota)
	if err != nil {
		chat.log.Error(err)
		ctx.JSON(500, gin.H{"status": "Fail", "message": "登录失败", "data": nil})
		return
	}
	token := uuid.New().String()
	if err := kredis.SetEx(context.Background(), "session:"+token, req.DeviceID, 7*24*time.Hour); err != nil {
		chat.log.Error(err)
		ctx.JSON(500, gin.H{"status": "Fail", "message": "登录失败", "data": nil})
		return
	}
	ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"quota": user.Quota}, "access_token": token})
}

func (chat *ChatService) Session(ctx *gin.Context) {
	cnf := config.GetConfig()
	if !cnf.Auth.Enabled {
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": false}})
		return
	}
	token := ctx.GetHeader("Authorization")
	deviceID, err := kredis.Get(context.Background(), "session:"+token)
	if err != nil {
		if !errors.Is(err, redis.Nil) {
			chat.log.Error(err)
			ctx.JSON(500, gin.H{"status": "Fail", "message": "会话服务异常", "data": nil})
			return
		}
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI"}})
		return
	}
	user, err := users.GetByDeviceID(deviceID)
	if err != nil || user == nil {
		ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI"}})
		return
	}
	ctx.JSON(200, gin.H{"status": "Success", "message": "", "data": gin.H{"auth": true, "model": "ChatGPTAPI", "phone": deviceID, "quota": user.Quota}})
}
