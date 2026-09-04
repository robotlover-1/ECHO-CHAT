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
	"net/http"

	"net"
)

var (
	configFile = flag.String("config", "dev.config.yaml", "")
)

func main() {
	flag.Parse()
	registry := prometheus.NewRegistry()
	registry.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	busMetrics := metrics_bus.NewBusMetrics(registry)

	http.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
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

	// ---- zrpc v2 listener (double-stack; gRPC kept during observation) ----
	if cnf.Server.ZrpcPort > 0 {
		zsrv, err := zrpc.NewServer(zrpc.ServerOptions{
			Address:     fmt.Sprintf("%s:%d", cnf.Server.IP, cnf.Server.ZrpcPort),
			AccessToken: cnf.Server.AccessToken,
		})
		if err != nil {
			log.Fatal(err)
		}
		chatSvc := server.NewChatService(recordsData, cnf, logger, busMetrics)
		// streamOK=false until Task 7 wires chat.completion_stream.
		if err := server.RegisterChatZRPC(zsrv, chatSvc, false); err != nil {
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
	service := server.NewChatService(recordsData, cnf, logger, busMetrics)
	proto.RegisterChatServer(s, service)

	healthCheckSrv := health.NewServer()
	grpc_health_v1.RegisterHealthServer(s, healthCheckSrv)

	if err = s.Serve(lis); err != nil {
		log.Fatal(err)
	}
}
