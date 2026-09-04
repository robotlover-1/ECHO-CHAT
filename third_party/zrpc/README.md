# zrpc —— ECHO-CHAT 跨语言 RPC（C 实现 + cgo 接入 Go）

对原 zrpc（C 教学骨架，见 `reference/zrpc-original`）协议做加固后形成的 **zrpc v2**，
用于替换 ECHO-CHAT 中的三条 gRPC 链路。最终以静态库 `libzrpc.a` + cgo bridge
（`zrpc-go/`）接入 Go 业务。

> 本目录当前仍在实施早期（Task 0/1）。来源与许可证见 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)；
> 完整方案见 `docs/superpowers/plans/2026-09-04-echo-chat-c-zrpc-cross-language-migration-plan.md`。

## 目录布局

```text
third_party/zrpc/
├── LICENSE-NOTICE.md       # 来源、授权与许可证记录（Task 0）
├── reference/              # 原始 zrpc 快照，仅参考，不编译（Task 0）
│   └── zrpc-original/
├── include/                # 对外头文件
│   ├── zrpc.h              # 公共 ABI（status、buffer、client/server 句柄）
│   ├── zrpc_protocol.h     # 协议：帧头、消息类型、常量、帧编解码（v2）
│   ├── zrpc_client.h       # C client ABI（Task 2）
│   └── zrpc_server.h       # C server ABI（Task 2）
├── src/
│   ├── zrpc_io.c           # read_full/write_full（Task 1）
│   ├── zrpc_frame.c        # v2 帧编解码 + CRC32（Task 1）
│   ├── zrpc_error.c        # status → 字符串
│   ├── cJSON.c / cJSON.h   # MIT，vendored
│   └── ...（Task 2 后新增 zrpc_client.c/zrpc_server.c/zrpc_stream.c/zrpc_json.c）
├── tests/                  # C 单元测试
│   ├── test_frame.c
│   └── test_io.c
├── Makefile                # 产出 build/libzrpc.a；make test 跑单测
└── build/                  # 构建产物（gitignore）
```

## v2 帧头（20B，网络字节序）

```text
magic 2B | version 1B | type 1B | request_id 8B | length 4B | crc32(payload) 4B
```

常量：magic `0x5A52`（"ZR"）、version `2`、最大帧 4 MiB。
消息类型：request/response/stream_data/stream_end/error/cancel/ping/pong（1–8）。

## 构建与测试

```bash
make -C third_party/zrpc            # 产出 build/libzrpc.a
make -C third_party/zrpc test       # 运行 C 单测
CFLAGS="-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer" \
  make -C third_party/zrpc test     # sanitizer 版本（会重建 lib 与测试）
```
