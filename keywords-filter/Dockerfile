# keywords-filter —— zrpc v2 迁移后镜像（仓库根 build context）
# 构建：cd <repo 根> && docker build -f keywords-filter/Dockerfile -t echo/keywords-filter .
# 与旧版差异：CGO_ENABLED=1 + 静态链接 libzrpc.a（gRPC 观察期双栈：gRPC 50053 + zrpc 50063 + HTTP /readyz 18081）。

# ---- 构建阶段（glibc/gcc） ----
FROM registry.cn-hangzhou.aliyuncs.com/library/golang:1.20 AS build
RUN go env -w GOPROXY=https://goproxy.cn,direct
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libc6-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# 先复制 zrpc C 源码并编译 libzrpc.a（变化小，利于缓存）
COPY third_party/zrpc ./third_party/zrpc
RUN make -C third_party/zrpc
# 复制共享 go 模块与服务（monorepo replace ../zrpc-go 在镜像内可解析）
COPY zrpc-go ./zrpc-go
COPY keywords-filter ./keywords-filter
WORKDIR /src/keywords-filter
RUN CGO_ENABLED=1 GOOS=linux GOARCH=amd64 go build -o /out/keywords-filter ./filter-server

# ---- 运行阶段（glibc slim） ----
FROM registry.cn-hangzhou.aliyuncs.com/library/debian:bookworm-slim AS run
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgcc-s1 tzdata wget \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /out/keywords-filter /app/keywords-filter
COPY keywords-filter/dev.config.yaml /app/config.yaml
COPY keywords-filter/dict.txt /app/dict.txt
EXPOSE 50053 50063 18081
ENTRYPOINT ["./keywords-filter"]
CMD ["--config=config.yaml", "--dict=dict.txt"]
