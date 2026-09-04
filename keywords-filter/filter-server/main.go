package main

import (
	"flag"
	"fmt"
	"net"
	"net/http"
	"sync/atomic"

	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"

	zrpc "echo-zrpc-go"
	"keywords-filter/filter-server/interceptor"
	"keywords-filter/filter-server/server"
	"keywords-filter/pkg/config"
	"keywords-filter/pkg/filter"
	"keywords-filter/pkg/log"
	"keywords-filter/proto"
)

var (
	configFile = flag.String("config", "dev.config.yaml", "")
	dictFile   = flag.String("dict", "dict.txt", "")
	formatDict = flag.Bool("format", false, "")
)

// ready flips once both listeners are bound.
var ready atomic.Bool

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

	// ---- zrpc v2 listener (double-stack; gRPC kept during observation) ----
	if cnf.Server.ZrpcPort > 0 {
		zsrv, err := zrpc.NewServer(zrpc.ServerOptions{
			Address:     fmt.Sprintf("%s:%d", cnf.Server.IP, cnf.Server.ZrpcPort),
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
		log.InfoF("zrpc listening on %s:%d", cnf.Server.IP, cnf.Server.ZrpcPort)
	}

	// ---- gRPC listener (unchanged for the observation period) ----
	lis, err := net.Listen("tcp", fmt.Sprintf("%s:%d", cnf.Server.IP, cnf.Server.Port))
	if err != nil {
		log.Fatal(err)
	}
	s := grpc.NewServer(grpc.UnaryInterceptor(interceptor.UnaryAuthInterceptor))
	service := server.NewFilterService(filter.GetFilter())
	proto.RegisterFilterServer(s, service)

	healthCheckSrv := health.NewServer()
	grpc_health_v1.RegisterHealthServer(s, healthCheckSrv)
	healthCheckSrv.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)

	// ---- HTTP healthz/readyz (replaces grpc_health_probe for zrpc) ----
	if cnf.Server.HealthPort > 0 {
		go serveHealth(cnf.Server.HealthPort)
	}

	ready.Store(true)
	log.InfoF("gRPC listening on %s:%d", cnf.Server.IP, cnf.Server.Port)
	if err = s.Serve(lis); err != nil {
		log.Fatal(err)
	}
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
