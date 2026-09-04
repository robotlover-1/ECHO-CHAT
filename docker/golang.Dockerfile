# 通用 Go 服务镜像。构建上下文 = 仓库根。
# 必传 ARG：
#   SRV   = 模块根相对路径（COPY 整目录进 /src 并以其为 module root）
#   ENTRY = 入口包（相对模块根，如 "./chat-server" / "."
# 用法见 docker/compose.yaml（ai-chat-service/openai-api-proxy/mock/keywords 共用）。
FROM golang:1.20-alpine AS build
ARG SRV ENTRY
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
COPY $SRV /src
RUN cd /src && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/app $ENTRY

FROM alpine:3.19
WORKDIR /app
RUN mkdir -p /app/runtime/logs
COPY --from=build /out/app /app/app
