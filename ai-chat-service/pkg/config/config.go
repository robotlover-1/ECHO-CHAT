package config

import (
	"github.com/spf13/viper"
	"log"
)

type Config struct {
	Server struct {
		IP          string
		Port        int
		AccessToken string
	}
	Log struct {
		Level   string
		LogPath string `mapstructure:"logPath"`
	} `mapstructure:"log"`
	Chat struct {
		ApiKey            string  `mapstructure:"api_key"`
		BaseUrl           string  `mapstructure:"base_url"`
		Model             string  `mapstructure:"model"`
		MaxTokens         int     `mapstructure:"max_tokens"`
		Temperature       float32 `mapstructure:"temperature"`
		TopP              float32 `mapstructure:"top_p"`
		PresencePenalty   float32 `mapstructure:"presence_penalty"`
		FrequencyPenalty  float32 `mapstructure:"frequency_penalty"`
		BotDesc           string  `mapstructure:"bot_desc"`
		MinResponseTokens int     `mapstructure:"min_response_tokens"`
		ContextTTL        int     `mapstructure:"context_ttl"`
		ContextLen        int     `mapstructure:"context_len"`
	}
	Mysql struct {
		DSN         string
		MaxLifeTime int
		MaxOpenConn int
		MaxIdleConn int
	}
	Redis struct {
		Host string
		Port int
		Pwd  string `mapstructure:"pwd"`
	}
	DependOn struct {
		Sensitive struct {
			Address     string
			AccessToken string
		}
		Keywords struct {
			Address     string
			AccessToken string
		}
		Tokenizer struct {
			Address string
		}
		Semantic struct {
			Address string
		}
	}
	VectorDB struct {
		Url                string
		Username           string
		Pwd                string
		Database           string
		Timeout            int
		MaxIdleConnPerHost int
		ReadConsistency    string
		IdleConnTimeout    int
	}
	SemanticCache struct {
		Enabled                 bool    `mapstructure:"enabled"`
		// Deprecated: Phase-1 阈值,仅兼容读取,不再参与新(e5/批决策)链路。
		Threshold               float32 `mapstructure:"threshold"`
		// Deprecated: Phase-1 rerank 阈值,仅兼容读取,不再参与新链路。/rerank 已废弃且 score 恒 0。
		RerankThreshold         float32 `mapstructure:"rerank_threshold"`
		ExactFingerprintEnabled bool    `mapstructure:"exact_fingerprint_enabled"`
		TopK                    int     `mapstructure:"top_k"`

		// e5 向量命名空间与一致性(e5s:v1)。
		Dimension         int    `mapstructure:"dimension"`          // 384
		VectorNamespace   string `mapstructure:"vector_namespace"`   // "semd:e5s:v1:"
		EmbeddingModel    string `mapstructure:"embedding_model"`    // "intfloat/multilingual-e5-small"
		EmbeddingRevision string `mapstructure:"embedding_revision"` // 固定 commit SHA
		ExportVersion     string `mapstructure:"export_version"`     // "onnx-int8-v1"

		// 检索阈值:acceptance_threshold + min_margin(TrimFlow 靠 VSEARCH Top-K)。
		AcceptanceThreshold  float32 `mapstructure:"acceptance_threshold"`
		MinMargin            float32 `mapstructure:"min_margin"`
		SoftSemanticFallback bool    `mapstructure:"soft_semantic_fallback"` // soft 通道兜底,默认 false

		// 灰度读写模式与异步回填。
		VectorReadMode  string `mapstructure:"vector_read_mode"`  // "new_only" | "dual_read"
		VectorWriteMode string `mapstructure:"vector_write_mode"` // "new_only" | "dual_write"
		AsyncBackfill   bool   `mapstructure:"async_backfill"`    // 默认 true
	} `mapstructure:"semantic_cache"`
}

var conf *Config

func InitConfig(filePath string, typ ...string) {
	v := viper.New()
	v.SetConfigFile(filePath)
	if len(typ) > 0 {
		v.SetConfigType(typ[0])
	}
	err := v.ReadInConfig()
	if err != nil {
		log.Fatal(err)
	}
	conf = &Config{}
	err = v.Unmarshal(conf)
	if err != nil {
		log.Fatal(err)
	}

}

func GetConfig() *Config {
	return conf
}
