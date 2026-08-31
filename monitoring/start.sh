#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _cmd in ss fuser; do
  command -v "$_cmd" >/dev/null 2>&1 || { echo "✘ 缺依赖命令: $_cmd（请安装 iproute2/psmisc）"; exit 1; }
done
LOG_DIR="$ROOT/logs"
PID_DIR="$ROOT/pids"
mkdir -p "$LOG_DIR" "$PID_DIR" "$ROOT/prometheus/data" "$ROOT/grafana/data"

if [ ! -x "$ROOT/bin/prometheus" ]; then
  echo "✘ 缺 prometheus 二进制: $ROOT/bin/prometheus（请按 README.md 解压）"; exit 1
fi
if [ ! -x "$ROOT/grafana/bin/grafana" ]; then
  echo "✘ 缺 grafana: $ROOT/grafana/bin/grafana（请按 README.md 解压）"; exit 1
fi

port_listening() { ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${1}$"; }

start_prom() {
  if port_listening 9090; then echo "  [prometheus] ✔ already running (9090)"; return 0; fi
  ( cd "$ROOT" && nohup "$ROOT/bin/prometheus" \
      --config.file=prometheus/prometheus.yml \
      --storage.tsdb.path=prometheus/data \
      --web.listen-address=:9090 \
      >"$LOG_DIR/prometheus.log" 2>&1 & echo $! > "$PID_DIR/prometheus.pid" )
  local i
  for i in $(seq 1 40); do port_listening 9090 && { echo "  [prometheus] ✔ 9090 listening"; return 0; }; sleep 0.5; done
  echo "  [prometheus] ✘ 启动失败, 日志尾部:"; tail -n 8 "$LOG_DIR/prometheus.log" | sed 's/^/    /'; return 1
}

start_graf() {
  if port_listening 3000; then echo "  [grafana] ✔ already running (3000)"; return 0; fi
  ( cd "$ROOT" && GF_SERVER_HTTP_PORT=3000 \
      GF_PATHS_DATA="$ROOT/grafana/data" \
      GF_PATHS_PROVISIONING="$ROOT/config/grafana/provisioning" \
      GF_SECURITY_DISABLE_INITIAL_ADMIN_PASSWORD_CHANGE=true \
      nohup "$ROOT/grafana/bin/grafana" server --homepath="$ROOT/grafana" \
      >"$LOG_DIR/grafana.log" 2>&1 & echo $! > "$PID_DIR/grafana.pid" )
  local i
  for i in $(seq 1 60); do port_listening 3000 && { echo "  [grafana] ✔ 3000 listening"; return 0; }; sleep 0.5; done
  echo "  [grafana] ✘ 启动失败, 日志尾部:"; tail -n 8 "$LOG_DIR/grafana.log" | sed 's/^/    /'; return 1
}

start_prom
start_graf
echo
echo "✔ Prometheus: http://localhost:9090  （/targets 看采集状态）"
echo "✔ Grafana:    http://localhost:3000  （admin/admin）"
