package middlewares

import (
	"github.com/gin-gonic/gin"
)

// defaultTrustedProxies：公网部署后端只被同机 frpc(host 网络)访问，回环一跳可信；
// XFF 由 edge Nginx 覆盖式写入（见 deploy/edge/nginx/echo-chat.conf.envsubst）。
var defaultTrustedProxies = []string{"127.0.0.1", "::1"}

// NewEngine 构造 gin 引擎并显式设置可信代理。configured 为空 → 回退默认回环；
// 配置非法 → 返回 error（调用方 fatal，不静默信任全部）。
func NewEngine(configured []string) (*gin.Engine, error) {
	trusted := configured
	if len(trusted) == 0 {
		trusted = defaultTrustedProxies
	}
	engine := gin.New()
	engine.Use(gin.Logger(), gin.Recovery(), Cors())
	if err := engine.SetTrustedProxies(trusted); err != nil {
		return nil, err
	}
	return engine, nil
}
