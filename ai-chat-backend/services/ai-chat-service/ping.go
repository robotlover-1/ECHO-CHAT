package ai_chat_service

import (
	"context"

	"ai-chat-backend/pkg/config"
	zrpc "echo-zrpc-go"
)

// Ping 探测 ai-chat-service(zrpc) 连通性，供 /api/readyz 使用。
// 复用 OpenChatStream 同款地址解析（config.DependOn.AiChatService.Address）。
func Ping(ctx context.Context) error {
	cnf := config.GetConfig()
	dep := cnf.DependOn.AiChatService
	cli, err := zrpc.NewClient(clientOptionsFromAddress(dep.Address, dep.AccessToken))
	if err != nil {
		return err
	}
	defer cli.Close()
	return cli.Ping(ctx)
}
