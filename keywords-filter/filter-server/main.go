package main

import (
	"flag"
	"fmt"
	"net/http"
	"sync/atomic"

	zrpc "echo-zrpc-go"
	"keywords-filter/filter-server/server"
	"keywords-filter/pkg/config"
	"keywords-filter/pkg/filter"
	"keywords-filter/pkg/log"
)

var (
	configFile = flag.String("config", "dev.config.yaml", "")
	dictFile   = flag.String("dict", "dict.txt", "")
	formatDict = flag.Bool("format", false, "")
	// ready 置真表示 zrpc 监听已就绪，供 /readyz 探活。
	ready atomic.Bool
)

func main() {
	flag.Parse()
	if *formatDict {
		filter.OverwriteDict(*dictFile)
		return
	}

	config.InitConfig(*configFile)
	cnf := config.GetConfig()
	log.SetLevel(cnf.Log.Level)
	log.SetOutput(log.GetRotateWriter(cnf.Log.LogPath))
	log.SetPrintCaller(true)
	filter.InitFilter(*dictFile)

	// 单 zrpc 传输（gRPC 已删）：业务端口即 cnf.Server.Port（50053/50054）
	zsrv, err := zrpc.NewServer(zrpc.ServerOptions{
		Address:     fmt.Sprintf("%s:%d", cnf.Server.IP, cnf.Server.Port),
		AccessToken: cnf.Server.AccessToken,
	})
	if err != nil {
		log.Fatal(err)
	}
	service := server.NewFilterService(filter.GetFilter())
	if err := service.RegisterZRPC(zsrv); err != nil {
		log.Fatal(err)
	}
	if err := zsrv.Serve(); err != nil {
		log.Fatal(err)
	}
	ready.Store(true)
	log.InfoF("zrpc listening on %s:%d", cnf.Server.IP, cnf.Server.Port)

	if cnf.Server.HealthPort > 0 {
		go serveHealth(cnf.Server.HealthPort)
	}
	select {} // zrpc server 协程在独立线程，主线程保持存活
}

func serveHealth(port int) {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if ready.Load() {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ready"))
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("not ready"))
	})
	addr := fmt.Sprintf(":%d", port)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Error(err)
	}
}
