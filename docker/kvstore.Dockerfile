# kvstore(pocket-kv)镜像 —— 实验性。
# 构建上下文 = 仓库根；前提：kvstore 子模块已 init（含其 NtyCo 子模块，即 `git submodule update --init --recursive`）。
# 依赖 liburing/NtyCo 等；RDMA/eBPF 默认关（宿主若开启 RDMA，改 ARG 并按宿主 Makefile 调）。
FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates \
        liburing-dev liburing2 && \
    rm -rf /var/lib/apt/lists/*
ARG ENABLE_RDMA=0
ARG ENABLE_EBPF=0
ARG ENABLE_KPROBE_RDMA=0
COPY kvstore /kvstore
WORKDIR /kvstore/kvstore
RUN make ENABLE_RDMA=${ENABLE_RDMA} ENABLE_EBPF=${ENABLE_EBPF} ENABLE_KPROBE_RDMA=${ENABLE_KPROBE_RDMA} \
    || { echo "kvstore 镜像构建失败——依赖(NtyCo/liburing)或 Makefile 需按宿主调整；可用宿主 ./start.sh 跑 kvstore 再连（见 docker/README 备选）"; exit 1; }

FROM debian:bookworm-slim
WORKDIR /
RUN mkdir -p /runtime/logs /data /configs
COPY --from=build /kvstore/kvstore/kvstore /kvstore/kvstore/kvstore
EXPOSE 5160
# 配置由 compose 从仓库 configs/kvstore-ai.conf 挂载到 /configs/kvstore-ai.conf（相对 CWD=/ 路径语义）
CMD ["sh","-c","./kvstore/kvstore/kvstore configs/kvstore-ai.conf"]
