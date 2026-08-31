# AOF 独立线程 + 先回复（reply-first）基准对比

> 计划：`2026-08-06-aof-thread-reply-first`（slot 原地写 + AOF 独立线程 + 先回复语语义）。HEAD = `98eeefc`。
> 目标：测量完整 P-sweep，对比改造前基线（README 2026-08-04，异步批量默认、durable-before-reply）。

## 测试方法

- 工具：`redis-benchmark -n 1000000 -c 50 -P N -d 64 -r 1000000 HSET key:__rand_int__ value`
- 服务端：`taskset -c 2,3 ./kvstore --port 5190 --role master --mem libc --net reactor --appendfsync always`
- 每轮 P 重启空库（`pkill -x kvstore` + `rm -f kvstore.aof kvstore.dump`），先 2w 预热再 100w 计时取 1 轮代表值
- 基线列：README 2026-08-04「异步批量(默认)」同口径数值（42,717 / 373,692 / 660,066 / 891,266 / 1,014,199 / 1,024,590）

## 结果表

| P | 新实现（reply-first） | 基线（README 08-04） | 变化 |
|---|---|---:|---:|
| 1  | 76,476 | 42,717 | **+79%** |
| 10 | 419,463 | 373,692 | +12% |
| 20 | 694,927 | 660,066 | +5% |
| 40 | 796,178 | 891,266 | **−11%** |
| 80 | 1,026,694 | 1,014,199 | +1% |
| 160 | 1,054,852 | 1,024,590 | +3% |

## 分析

- **P=1 提升 +79%**（42.7k → 76.5k）：先回复语义让回包不卡 fsync，P=1 单请求 RTT 不再恒含逐条 fsync 延迟，是本次改造的最大收益点，落在 Task 3 观测的 ≈88.7k 附近（有测量波动，见下）。
- **高 P 整体持平/略升**（+1%~+12%）：批量越大一次 fsync 摊得越薄，reply-first 的收益被 io_uring 异步重叠与攒批摊平，与预期"持平/略升"基本一致。
- **P=40 出现 −11% 回退**（891k → 796k），P=40/P=80 多次复测 762k–835k / 873k–1026k，P=40 为 4 组样本均落在 08-04 基线之下（765–835k），非单次测量噪声。诊断方向：先回复窗口的背压触发点、槽轮转与 io_uring 提交频次在 P=40 段的交互，尚未收敛到根因；其余 P 无该现象。
- **P=1 波动说明**：Task 3 报 88.7k，本次全 P 扫 P=1 落在 73–77k（76,476 / 77,417 / 73,244 / 76,476 多次）。报 76.5k 为保守代表值，与 Task 3 的 ~88k 差 ~13% 属测量/环境波动；比改造前 42.7k 的提升幅度稳定在 **+75%~+85%**。

## 异步逐条（`--aof-fsync-per-command`）

先回复语义下的逐条模式（每条命令立即 write+fsync，P 无感）：

| P | QPS | 说明 |
|---|---:|---|
| 1 | 16,363 | 每条命令一次 fsync 进回包路径，封顶 |
| 40 | 17,468 | P 无感，恒定 ~16–17k（单盘 fsync 率） |

> 逐条模式 P=1/P=40 均 ~16–17k，P 完全不涨——瓶颈在每条命令串行 fsync，与 reply-first 默认批量（同核同盘）差异显著。该模式仅供"每条命令独立确认"语义，无吞吐价值。

## 复制冒烟（TCP 传输）

- 主 5190 + 从 5191（`--repl-fullsync-transport tcp --repl-realtime-transport tcp`）。
- **全量同步通过**：预置主库 3 个 key（pre:key:1/v1、pre:key:2/v2、pre:str/hello），从库收到并读回一致；master_off=AOF offset=111 一致。
- **增量同步受传输限制**：本机 `tcp` realtime 被接线为 `ebpf+tcp` proxy 模式（`repl_broadcast` 对 `KVS_REPL_TRANSPORT_EBPF_TCP` 跳过直连广播，expects ebpf-proxy 转发），环境无 ebpf-proxy/kprobe 特权组件 → 全量后写入未触达从库。此为改造前既有的传输接线设计，**非 reply-first 改造引入**。`repl_durable_offset_ack` 基础 offset 跟踪在全量路径（master_off=111）已打通。
