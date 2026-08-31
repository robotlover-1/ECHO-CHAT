package metrics_app

import (
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc"
	"runtime"
	"time"
)

type StreamMiddleware interface {
	WrapHandler() grpc.StreamServerInterceptor
}
type streamMiddleware struct {
	registry        *prometheus.Registry
	handlerCounter  *prometheus.CounterVec
	handlerDuration *prometheus.SummaryVec
	handlerAtHour   *prometheus.HistogramVec
}

const (
	NAMESPACE = "ai_chat"
	SUBSYSTEM = "chat_service"
)

func NewStreamMiddleware(registry *prometheus.Registry) StreamMiddleware {
	counter := prometheus.NewCounterVec(prometheus.CounterOpts{
		Namespace:   NAMESPACE,
		Subsystem:   SUBSYSTEM,
		Name:        "requests_total",
		ConstLabels: map[string]string{"app": "ai_chat"},
		Help:        "用于累计请求次数",
	}, []string{"full_method"})
	gauge := prometheus.NewGaugeFunc(prometheus.GaugeOpts{
		Namespace:   NAMESPACE,
		Subsystem:   SUBSYSTEM,
		Name:        "curr_num_goroutine",
		ConstLabels: map[string]string{"app": "ai_chat"},
		Help:        "当前存在的goroutine数量",
	}, func() float64 {
		return float64(runtime.NumGoroutine())
	})
	histogram := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Namespace:   NAMESPACE,
		Subsystem:   SUBSYSTEM,
		Name:        "request_hour",
		ConstLabels: map[string]string{"app": "ai_chat"},
		Help:        "http请求发生在一天之中的哪个小时",
		Buckets:     []float64{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23},
	}, []string{"full_method"})
	summary := prometheus.NewSummaryVec(prometheus.SummaryOpts{
		Namespace:   NAMESPACE,
		Subsystem:   SUBSYSTEM,
		Name:        "request_duration_ms",
		ConstLabels: map[string]string{"app": "ai_chat"},
		Help:        "请求时长分布",
		Objectives:  map[float64]float64{0.1: 0.01, 0.5: 0.01, 0.9: 0.01, 0.99: 0.01},
	}, []string{"full_method"})
	registry.MustRegister(counter, gauge, histogram, summary)
	return &streamMiddleware{
		registry:        registry,
		handlerCounter:  counter,
		handlerDuration: summary,
		handlerAtHour:   histogram,
	}
}

func (s *streamMiddleware) WrapHandler() grpc.StreamServerInterceptor {
	return func(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		label := map[string]string{
			"full_method": info.FullMethod,
		}
		s.handlerCounter.With(label).Inc()
		hour := time.Now().Hour()
		s.handlerAtHour.With(label).Observe(float64(hour))
		start := time.Now()
		defer func() {
			s.handlerDuration.With(label).Observe(float64(time.Since(start).Milliseconds()))
		}()
		err := handler(srv, ss)
		return err
	}
}
