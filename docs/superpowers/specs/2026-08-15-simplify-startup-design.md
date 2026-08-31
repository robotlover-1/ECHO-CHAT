# ai-chat 一键启动/停止 设计文档

- 日期：2026-08-15
- 状态：已批准
- 位置：`ai-chat/`（与 `运行步骤.md` 同级）

## 问题

ai-chat 本地开发要手动起 8 个服务，每个都要单独开终端、`cd` 到自己的项目目录再执行（配置文件里 `logPath`/aof 都是相对路径，依赖启动 CWD），且启动顺序敏感、停止也要逐个按端口 `fuser -k`。启动麻烦。

## 目标

- 一条命令后台拉起全部 8 个服务，日志落盘，失败可查
- 一条命令全部停止
- 二进制/前端 dist 缺失时自动补编译
- 不修改任何现有服务代码与配置文件（相对路径问题用 `cd` 绕开）

## 交付物

### 1. `start.sh`

```
用法: ./start.sh            # 启动全部（缺失的二进制/前端自动补编译）
      ./start.sh --rebuild  # 强制重新编译全部后再启动
```

流程：

1. 环境准备：`export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin:$HOME/.local/bin`，`export GOPROXY=https://goproxy.cn,direct`
2. 补齐编译（仅缺失/`--rebuild` 时）：
   - 5 个 Go 二进制 → `bin/`（命令照抄 `运行步骤.md` 1.2 节）
   - 前端 `ai-chat-web/dist` 与 `ai-chat-backend/www` 缺一即 `pnpm install --fetch-retries=15 && pnpm build-only && cp -r dist/. ../ai-chat-backend/www/`
3. 按序启动 8 个服务，每个：`cd` 到启动目录 → `nohup` 后台拉起 → 输出重定向 `runtime/logs/<服务>.log` → 写 PID `runtime/pids/<服务>.pid` → 轮询 `ss -tln` 等端口就绪（15s 超时）

| 顺序 | 服务 | 端口 | 启动目录 | 命令 |
|---|---|---|---|---|
| 1 | kvstore | 5160 | `9.1-kvstore/`（父目录） | `./kvstore ai-chat/configs/kvstore-ai.conf` |
| 2 | tokenizer | 3002 | `tokenizer/` | `nuxt --port 3002 --module tokenizer.py --workers 2` |
| 3 | 敏感词过滤 | 50053 | `keywords-filter/` | `bin/keywords-filter --config=dev.config.yaml --dict=dict.txt` |
| 4 | 关键词过滤 | 50054 | `keywords-filter/` | `bin/keywords-filter --config=dev.kw.config.yaml --dict=keyword-dict.txt` |
| 5 | mock-openai-api | 8083 | `mock-openai-api/` | `bin/mock-openai-api --config=dev.config.yaml` |
| 6 | openai-api-proxy | 8084 | `openai-api-proxy/` | `bin/openai-api-proxy --config=dev.config.yaml` |
| 7 | ai-chat-service | 50055 | `ai-chat-service/` | `bin/ai-chat-service --config=dev.config.yaml` |
| 8 | ai-chat-backend | 7080 | `ai-chat-backend/` | `bin/ai-chat-backend --config=dev.config.yaml` |

4. 幂等：端口已在监听的服务跳过，标注 `already running`
5. 结果汇总：逐服务打印 `✔ 端口已监听` / `✘ 启动失败(附该服务日志尾部)`，最后提示访问 `http://localhost:7080`

脚本须 `bash` 可执行（`#!/usr/bin/env bash` + `chmod +x`）。

### 2. `stop.sh`

```
用法: ./stop.sh            # 停全部 8 个
      ./stop.sh <服务名>    # 只停一个，如 ./stop.sh ai-chat-service
```

- 优先读 `runtime/pids/<服务>.pid` 精确 SIGTERM；PID 文件缺失时回退 `fuser -k -TERM <端口>/tcp`
- 停完用 `ss` 复查端口释放情况并汇报

### 3. 状态文件与日志

- 每个服务独立日志：`runtime/logs/<服务>.log`
- PID 文件：`runtime/pids/<服务>.pid`
- `runtime/` 加入 `ai-chat/.gitignore`（内含 aof/dump/日志/pid，均为运行期状态，不进 git）

### 4. 文档更新

`运行步骤.md` 开头加「最简方式」一节：编译一次后 `cd ai-chat && ./start.sh` 启动、`./stop.sh` 停止；原手工分步保留作为排查参考。

## 明确不做（Out of scope）

- 进程守护/自动重启（服务挂了不会自愈，重跑 `./start.sh` 即可）
- 生产部署（`ai-chat-stack/` Docker Swarm 编排不动）
- `status` 子命令（`start.sh` 结尾即输出监听状态）

## 验收标准

1. 冷启动：`./start.sh` 全绿，8 端口监听，浏览器 `http://localhost:7080` 可开页
2. 幂等：服务都在跑时再 `./start.sh`，全部标 `already running`，不重复拉起
3. 停止：`./stop.sh` 后 `ss -tlnp | grep -E "3002|50053|50054|50055|7080|8083|8084|5160"` 无输出
4. 缺失构建：删掉 `bin/ai-chat-backend` 后 `./start.sh` 会自动重新编译该二进制
5. 日志可查：`runtime/logs/ai-chat-service.log` 有内容
