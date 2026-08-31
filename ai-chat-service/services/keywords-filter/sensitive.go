package keywords_filter

import (
	"ai-chat-service/pkg/config"
	grpc_client "ai-chat-service/services/grpc-client"
	"sync"
)

var sensitivePool grpc_client.ClientPool
var sensitiveOnce sync.Once

type sensitiveClient struct {
	grpc_client.DefaultClient
}

func GetSensitiveClientPool() grpc_client.ClientPool {
	sensitiveOnce.Do(func() {
		cnf := config.GetConfig()
		c := &sensitiveClient{}
		sensitivePool = c.GetPool(cnf.DependOn.Sensitive.Address)
	})
	return sensitivePool
}
