# 编译阶段
FROM quay.io/0voice/golang:1.20 as stage0
RUN go env -w GOPROXY=https://proxy.golang.com.cn,https://goproxy.cn,direct
ADD ./ /src/keywords-filter
WORKDIR /src/keywords-filter
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o keywords-filter ./filter-server

FROM quay.io/0voice/alpine:3.18 as stage1
ADD ./grpc_health_probe-linux-amd64 /usr/bin/grpc_health_probe
RUN chmod +x /usr/bin/grpc_health_probe
MAINTAINER nick
WORKDIR /app/
ADD ./dev.config.yaml /app/config.yaml
ADD ./dict.txt /app/dict.txt
COPY --from=stage0 /src/keywords-filter/keywords-filter ./
# 指定入口程序
ENTRYPOINT ["./keywords-filter"]
# 指定容器的启动命令或者入口程序的参数
CMD ["--config=config.yaml","--dict=dict.txt"]
