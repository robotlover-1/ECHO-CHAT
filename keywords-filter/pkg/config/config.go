package config

import (
	"github.com/spf13/viper"
	"log"
)

type Config struct {
	Server struct {
		IP          string
		Port        int // gRPC business port (kept during the observation period)
		ZrpcPort    int // zrpc v2 business port (double-stack)
		AccessToken string
		HealthPort  int // 0 disables the HTTP healthz/readyz listener
	}
	Log struct {
		Level   string
		LogPath string `mapstructure:"logPath"`
	} `mapstructure:"log"`
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
