# 编译阶段
FROM quay.io/0voice/golang:1.20 as stage0
RUN go env -w GOPROXY=https://proxy.golang.com.cn,https://goproxy.cn,direct
ADD ./ /src/ai-chat-backend
WORKDIR /src/ai-chat-backend
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o ai-chat-backend cmd/*.go

FROM quay.io/0voice/alpine:3.18 as stage1
ADD ./curl-amd64 /usr/bin/curl
RUN chmod +x /usr/bin/curl
MAINTAINER nick
WORKDIR /app/
ADD ./dev.config.yaml /app/config.yaml
ADD ./www /app/www
COPY --from=stage0 /src/ai-chat-backend/ai-chat-backend ./
# 指定入口程序
ENTRYPOINT ["./ai-chat-backend"]
# 指定容器的启动命令或者入口程序的参数
CMD ["--config=config.yaml"]
