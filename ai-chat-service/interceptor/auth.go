package interceptor

import (
	"ai-chat-service/pkg/config"
	"ai-chat-service/pkg/zerror"
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"strings"
)

func UnaryAuthInterceptor(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (resp any, err error) {
	if info.FullMethod != "/grpc.health.v1.Health/Check" {
		err = oauth2Valid(ctx)
		if err != nil {
			return
		}
	}
	return handler(ctx, req)
}
func oauth2Valid(ctx context.Context) error {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return zerror.NewByMsg("元数据获取失败")
	}
	authorization := md["authorization"]

	if len(authorization) < 1 {
		return zerror.NewByMsg("元数据获取失败")
	}

	token := strings.TrimPrefix(authorization[0], "Bearer ")
	cnf := config.GetConfig()
	if token != cnf.Server.AccessToken {
		return zerror.NewByMsg("鉴权失败")
	}
	return nil
}
