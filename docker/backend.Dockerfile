# ai-chat-backend + 前端 dist 合成镜像。构建上下文 = 仓库根。
FROM node:20-alpine AS fe
WORKDIR /fe
COPY ai-chat-web/package.json ai-chat-web/pnpm-lock.yaml* ./
RUN corepack enable && pnpm install --fetch-retries=15 || pnpm install
COPY ai-chat-web .
RUN pnpm build-only

FROM golang:1.20-alpine AS build
WORKDIR /src
COPY ai-chat-backend /src
RUN cd /src && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/app ./cmd/

FROM alpine:3.19
WORKDIR /app
RUN mkdir -p /app/runtime/logs
COPY --from=fe /fe/dist /app/www
COPY --from=build /out/app /app/app
