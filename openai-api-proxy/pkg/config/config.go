package config

import (
	"os"

	"github.com/spf13/viper"
	"log"
)

type Config struct {
	Http struct {
		Host        string
		Port        int
		AccessToken string `mapstructure:"access_token"`
		Mode        string
	}
	Chat struct {
		APIKeys []string `mapstructure:"api_keys"`
		BaseURL string   `mapstructure:"base_url"`
	}
	Log struct {
		Level   string
		LogPath string `mapstructure:"log_path"`
	}
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
	// DeepSeek key 从环境变量注入，覆盖配置文件（不把密钥写进 git）
	if k := os.Getenv("DEEPSEEK_API_KEY"); k != "" {
		conf.Chat.APIKeys = []string{k}
	}

}

func GetConfig() *Config {
	return conf
}
