package grpc_client

import (
	"ai-chat-backend/pkg/log"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type ServiceClient interface {
	GetPool(addr string) ClientPool
}
type DefaultClient struct {
}

func (c *DefaultClient) GetPool(addr string) ClientPool {
	pool, err := NewPool(addr, c.getOptions()...)
	if err != nil {
		log.Error(err)
		return nil
	}
	return pool
}

func (c *DefaultClient) getOptions() []grpc.DialOption {
	opts := make([]grpc.DialOption, 0)
	opts = append(opts, grpc.WithTransportCredentials(insecure.NewCredentials()))
	return opts
}
