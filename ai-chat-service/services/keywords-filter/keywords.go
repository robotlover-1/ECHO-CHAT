package keywords_filter

import (
	"ai-chat-service/pkg/config"
	grpc_client "ai-chat-service/services/grpc-client"
	"sync"
)

var keywordsPool grpc_client.ClientPool
var keywordsOnce sync.Once

type keywordsClient struct {
	grpc_client.DefaultClient
}

func GetKeywordsClientPool() grpc_client.ClientPool {
	keywordsOnce.Do(func() {
		cnf := config.GetConfig()
		c := &keywordsClient{}
		keywordsPool = c.GetPool(cnf.DependOn.Keywords.Address)
	})
	return keywordsPool
}
