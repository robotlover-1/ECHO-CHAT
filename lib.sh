#!/usr/bin/env bash
# ai-chat 服务定义与公共工具 —— 被 start.sh / stop.sh source
# 每个服务一行：name|port|cwd|command
# cwd 是启动工作目录（配置文件里 logPath/aof 为相对路径，依赖 CWD）
set -u

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 现在就是仓库根
LOG_DIR="$BASE/runtime/logs"
PID_DIR="$BASE/runtime/pids"

# 顺序即启动顺序（依赖方在后），停止时按逆序处理
SERVICES=(
  "kvstore|5160|$BASE|./kvstore/kvstore/kvstore configs/kvstore-ai.conf"
  "tokenizer|3002|$BASE/tokenizer|nuxt --port 3002 --module tokenizer.py --workers 2"
  "semantic|3003|$BASE/semantic|nuxt --port 3003 --module semantic.py --workers 2"
  "sensitive|50053|$BASE/keywords-filter|$BASE/bin/keywords-filter --config=dev.config.yaml --dict=dict.txt"
  "keyword|50054|$BASE/keywords-filter|$BASE/bin/keywords-filter --config=dev.kw.config.yaml --dict=keyword-dict.txt"
  "mock|8083|$BASE/mock-openai-api|$BASE/bin/mock-openai-api --config=dev.config.yaml"
  "proxy|8084|$BASE/openai-api-proxy|$BASE/bin/openai-api-proxy --config=dev.config.yaml"
  "service|50055|$BASE/ai-chat-service|$BASE/bin/ai-chat-service --config=dev.config.yaml"
  "backend|7080|$BASE/ai-chat-backend|$BASE/bin/ai-chat-backend --config=dev.config.yaml"
)

# frpc 公网隧道：不在 SERVICES 里（它不监听本地端口，无端口可探活），
# 由 start.sh 的 ensure_frpc / stop.sh 单独处理。
FRPC_NAME="frpc"
FRPC_BIN="$BASE/bin/frpc"
FRPC_ENV="$BASE/deploy/app/.env"
FRPC_TEMPLATE="$BASE/deploy/app/tunnel/frpc.yaml.envsubst"
FRPC_CONFIG="$BASE/deploy/app/tunnel/frpc.yaml"
FRPC_PUBLIC_PORT=39000          # 云端 frps 控制口，用于探测隧道是否已建立

SERVICE_NAMES="$(for e in "${SERVICES[@]}"; do echo "${e%%|*}"; done | tr '\n' ' ')"

logfile() { echo "$LOG_DIR/$1.log"; }
pidfile() { echo "$PID_DIR/$1.pid"; }

# 端口是否在监听（ss -tln 的 Local Address 列，IPv4 形如 0.0.0.0:5160，IPv6 形如 *:50053）
port_listening() {
  local port="$1"
  ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"
}

pid_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# 隧道是否已建立：frpc 到云端 frps 控制口的 TCP 连接处于 ESTABLISHED。
# frpc 无本地监听端口，只能这样探活（进程存在 ≠ 已登录成功）。
frpc_registered() {
  ss -tn state established 2>/dev/null \
    | awk '{print $4}' | grep -qE "[:.]${FRPC_PUBLIC_PORT}$"
}
