#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TARGETS=()   # 元素: "name|port"，保持 SERVICES 顺序，停时逆序处理

collect_targets() {
  local arg found entry name port cwd cmd
  if [ $# -eq 0 ]; then
    for entry in "${SERVICES[@]}"; do
      IFS='|' read -r name port cwd cmd <<< "$entry"
      TARGETS+=("$name|$port")
    done
    return
  fi
  for arg in "$@"; do
    found=0
    for entry in "${SERVICES[@]}"; do
      IFS='|' read -r name port cwd cmd <<< "$entry"
      if [ "$arg" = "$name" ] || [ "$arg" = "$port" ]; then
        TARGETS+=("$name|$port"); found=1; break
      fi
    done
    if [ $found -eq 0 ]; then echo "警告: 未知服务/端口 '$arg' (可用: $SERVICE_NAMES)"; fi
  done
}

stop_one() {
  local name="$1" port="$2" pf pid i
  pf="$(pidfile "$name")"
  if [ -f "$pf" ] && pid_alive "$(cat "$pf")"; then
    pid="$(cat "$pf")"
    kill -TERM "$pid"
    echo "  [$name] TERM 已发送"
  elif port_listening "$port"; then
    fuser -k -TERM "${port}/tcp" 2>/dev/null || true
    echo "  [$name] fuser 回退停止"
  else
    echo "  [$name] 未运行"
    # 进程已不在且端口未监听: pid 为陈旧文件, 直接清理
    rm -f "$pf"
  fi
}

collect_targets "$@"

echo "== 停止 $((${#TARGETS[@]})) 个服务 =="
if [ $# -eq 0 ]; then   # 停全部: 逆序
  for ((i = ${#TARGETS[@]} - 1; i >= 0; i--)); do
    IFS='|' read -r name port <<< "${TARGETS[$i]}"
    stop_one "$name" "$port"
  done
else                    # 指定目标: 按用户顺序
  for t in "${TARGETS[@]}"; do
    IFS='|' read -r name port <<< "$t"
    stop_one "$name" "$port"
  done
fi

# 等端口释放（最多 10s）；仍占用的回退 fuser 强停(KILL)
for t in "${TARGETS[@]}"; do
  IFS='|' read -r name port <<< "$t"
  for i in $(seq 1 20); do
    port_listening "$port" || break
    sleep 0.5
  done
  if port_listening "$port"; then
    echo "  [$name] $port 未释放, fuser 强停(KILL)"
    fuser -k -KILL "${port}/tcp" 2>/dev/null || true
    sleep 1
  fi
done

# 确认进程已死后再删 pid 文件; 端口仍在监听(KILL 未生效)则保留, 便于下次精确停止
for t in "${TARGETS[@]}"; do
  IFS='|' read -r name port <<< "$t"
  if ! port_listening "$port"; then
    rm -f "$(pidfile "$name")"
  fi
done

# 汇总
remaining=0
for t in "${TARGETS[@]}"; do
  IFS='|' read -r name port <<< "$t"
  port_listening "$port" && { remaining=$((remaining+1)); echo "  [✘] $name $port 仍在监听"; }
done
if [ $remaining -eq 0 ]; then echo "✔ 全部停止, 端口已释放"; else exit 1; fi
