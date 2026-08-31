# kvstore + ai-chat 目录重组（InazumaPlasma 平铺式）设计文档

- 日期：2026-08-16
- 状态：已批准
- 位置：9.1-kvstore 单一仓库（main）

## 背景与目标

当前结构"割裂"：kvstore 引擎散在仓库根（src/include/Makefile 等），ai-chat 嵌套在 `ai-chat/` 子目录，两套脚本/文档在不同层级。参考 `proj/tmp/InazumaPlasma`（每个组件顶层目录 + 根级编排），把仓库重组为**单一平铺项目**，消除割裂。**全部用 `git mv` 移动，保留历史。**

## 目标结构

```
9.1-kvstore/
├── kvstore/                          # 引擎整体迁入
│   ├── src/ include/ Makefile/ NtyCo/(子模块) liburing/
│   ├── kvstore(二进制) kvstore.conf/ tests/ tools/ clients/
│   ├── benchmarks/ build/ artifacts/ assets/ testdata/ third_party/ tmp_repl_test/
│   └── docs/                         # 原根 docs/ 的引擎分析文档（aof/ebpf/bench 等）移这里
├── ai-chat-backend/  ai-chat-service/  tokenizer/  keywords-filter/
├── mock-openai-api/  openai-api-proxy/  ai-chat-web/  ai-chat-stack/
├── monitoring/  configs/  bin/  runtime/     # ai-chat/configs|bin|runtime 提升
├── docs/                              # ai-chat/docs 提升（调用流程 + superpowers）
├── Makefile                           # 根编排
├── start.sh  stop.sh  lib.sh          # ai-chat 脚本提升
├── README.md  CLAUDE.md  统一项目说明.md
```

## 两个大搬移

### 搬移 1：kvstore 引擎 → `kvstore/`
`git mv` 以下到 `kvstore/`：src、include、Makefile、kvstore(二进制)、kvstore.conf、tools、tests、clients、benchmarks、build、artifacts、assets、testdata、third_party、tmp_repl_test、NtyCo(子模块)、liburing、以及**根 docs/ 里的引擎分析文档**（aof-*.md、ebpf-*.md、benchmark-*.md 等）。
- kvstore Makefile 全是相对路径（SRC_DIR=src、-I./include 等），整目录一起搬后 `cd kvstore && make` 仍有效
- **NtyCo 是子模块**：移动后需更新 `.gitmodules` 的 path（`NtyCo` → `kvstore/NtyCo`）

### 搬移 2：ai-chat/* → 根
`git mv` 以下从 `ai-chat/` 提升到根：ai-chat-backend、ai-chat-service、tokenizer、keywords-filter、mock-openai-api、openai-api-proxy、ai-chat-web、ai-chat-stack、monitoring、configs、bin、runtime、docs、start.sh、stop.sh、lib.sh
- 原 `ai-chat/docs/superpowers/` → `docs/superpowers/`
- 原 `ai-chat/configs/kvstore-ai.conf` → `configs/kvstore-ai.conf`
- 原 `ai-chat/bin/`（8 服务二进制）→ `bin/`；`monitoring/bin/` 是 prometheus/grafana，随 monitoring 一起提升，二者不冲突

## 关键路径更新

| 文件 | 改什么 |
|---|---|
| `lib.sh` | `BASE` = 脚本所在目录（现为根）；删 `KVROOT`；kvstore 服务行 → `cd $ROOT && ./kvstore/kvstore configs/kvstore-ai.conf`；其余 `$BASE/xxx` 语义不变（BASE 已是根）；日志/PID 目录 `runtime/logs`、`runtime/pids` 不变 |
| `configs/kvstore-ai.conf` | `dump_path` / `aof_path`：`ai-chat/runtime/...` → `runtime/...`（相对根 CWD） |
| 根 `Makefile`（新建） | 编排：`all` 进 `kvstore/` 构建；`start`/`stop` 委托 `./start.sh`/`./stop.sh` |
| `.gitignore` | 合并根 kvstore 的 + `ai-chat/.gitignore`（bin/runtime/monitoring 数据/artifacts 等），放根 |
| `README.md` / `统一项目说明.md` / `CLAUDE.md` | 去掉 `ai-chat/` 前缀的路径引用，指向新结构 |
| 调用流程文档 | 更新引用的组件路径（如需） |
| `NtyCo` | `.gitmodules` path 更新为 `kvstore/NtyCo` |

## 明确保留 / 不动的

- `mock-openai-api`：已不用（proxy 直连 DeepSeek）但保留，供离线开发
- 各组件源码内容不动，只动目录层级
- `monitoring/` 自包含（自己的 bin/、config/、dashboards/），整体提升即可

## 验证

1. `cd kvstore && make` 构建通过
2. 根 `./start.sh` 8 服务全起（含 kvstore 用 `./kvstore/kvstore configs/kvstore-ai.conf`），`./stop.sh` 正常
3. 登录/聊天/语义缓存端到端正常（发消息能回真实 DeepSeek 答案、语义缓存命中）
4. `monitoring/start.sh` Prometheus/Grafana 正常
5. `git mv` 保留历史、工作区干净；无 `ai-chat/` 残留引用（grep 校验）

## 风险与注意

- 大移动后需**全量回归**（构建 + 启动 + 聊天 + 监控）
- NtyCo 子模块移动易漏 `.gitmodules` 更新
- `ai-chat/docs` 与 `kvstore/docs` 文档分家：根 `docs/` 放项目级（调用流程、superpowers），`kvstore/docs/` 放引擎分析
- 移动后 `git status` 可能出现大量 rename——属预期
