package main

import (
	"ai-chat-service/chat-server/data"
	metrics_app "ai-chat-service/chat-server/metrics-app"
	metrics_bus "ai-chat-service/chat-server/metrics-bus"
	"ai-chat-service/chat-server/server"
	"ai-chat-service/interceptor"
	"ai-chat-service/pkg/config"
	"ai-chat-service/pkg/db/mysql"
	"ai-chat-service/pkg/db/redis"
	"ai-chat-service/pkg/log"
	"ai-chat-service/proto"
	zrpc "echo-zrpc-go"
	"flag"
	"fmt"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
	"net"
	"net/http"
	"sync/atomic"
)

var (
	configFile = flag.String("config", "dev.config.yaml", "")
	// ready 置真表示 zrpc/gRPC 监听均已就绪，供 /readyz 探活。
	ready atomic.Bool
)

func main() {
	flag.Parse()
	registry := prometheus.NewRegistry()
	registry.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	busMetrics := metrics_bus.NewBusMetrics(registry)

	http.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	// HTTP healthz/readyz（替代 grpc_health_probe；复用 :8080 metrics server）
	http.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	http.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if ready.Load() {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ready"))
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("not ready"))
	})
	go http.ListenAndServe(":8080", nil)

	//初始化配置文件
	config.InitConfig(*configFile)
	cnf := config.GetConfig()
	//初始化日志
	log.SetLevel(cnf.Log.Level)
	log.SetOutput(log.GetRotateWriter(cnf.Log.LogPath))
	log.SetPrintCaller(true)

	logger := log.NewLogger()
	logger.SetLevel(cnf.Log.Level)
	logger.SetOutput(log.GetRotateWriter(cnf.Log.LogPath))
	logger.SetPrintCaller(true)

	// 初始化Mysql
	mysql.InitMysql(cnf)
	// 初始化redis
	redis.InitRedisPool(cnf)

	recordsData := data.NewChatRecordsData(mysql.GetDB())

	// one shared service instance for both transports (same business state)
	service := server.NewChatService(recordsData, cnf, logger, busMetrics)

	// ---- zrpc v2 listener (double-stack; gRPC kept during observation) ----
	if cnf.Server.ZrpcPort > 0 {
		zsrv, err := zrpc.NewServer(zrpc.ServerOptions{
			Address:     fmt.Sprintf("%s:%d", cnf.Server.IP, cnf.Server.ZrpcPort),
			AccessToken: cnf.Server.AccessToken,
		})
		if err != nil {
			log.Fatal(err)
		}
		if err := server.RegisterChatZRPC(zsrv, service, true); err != nil {
			log.Fatal(err)
		}
		if err := zsrv.Serve(); err != nil {
			log.Fatal(err)
		}
		fmt.Printf("[zrpc] listening on %s:%d\n", cnf.Server.IP, cnf.Server.ZrpcPort)
	}

	lis, err := net.Listen("tcp", fmt.Sprintf("%s:%d", cnf.Server.IP, cnf.Server.Port))
	if err != nil {
		log.Fatal(err)
	}
	s := grpc.NewServer(grpc.UnaryInterceptor(interceptor.UnaryAuthInterceptor), grpc.StreamInterceptor(metrics_app.NewStreamMiddleware(registry).WrapHandler()))
	proto.RegisterChatServer(s, service)

	healthCheckSrv := health.NewServer()
	grpc_health_v1.RegisterHealthServer(s, healthCheckSrv)

	ready.Store(true) // zrpc 已 Serve、gRPC 监听已 bind → /readyz 放行
	if err = s.Serve(lis); err != nil {
		log.Fatal(err)
	}
}
