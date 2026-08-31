#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# 必须用普通用户运行：sudo 会把 $HOME 切到 /root，$PATH 里依赖的 $HOME/.local/bin
# （pip --user 装的 nuxt/go 工具）就找不到；且 8 个服务均为高端口、无特权需求。
if [ "$(id -u)" -eq 0 ]; then
  echo "✘ 请勿用 sudo/root 运行 start.sh（会找不到用户级 python 包 nuxt）。请先 sudo ./stop.sh 清理，再以普通用户 ./start.sh" >&2
  exit 1
fi

REBUILD=0
[[ "${1:-}" == "--rebuild" ]] && REBUILD=1

export PATH="$PATH:/usr/local/go/bin:$HOME/go/bin:$HOME/.local/bin"
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
mkdir -p "$LOG_DIR" "$PID_DIR"

echo "== 环境/补编译 =="

# 目标二进制缺失 / 存在更新的 *.go / --rebuild 时重建。格式: 名|构建目录|包|输出(相对 BASE)
GO_BUILDS=(
  "ai-chat-backend|$BASE/ai-chat-backend|./cmd/|bin/ai-chat-backend"
  "ai-chat-service|$BASE/ai-chat-service/chat-server|.|bin/ai-chat-service"
  "keywords-filter|$BASE/keywords-filter/filter-server|.|bin/keywords-filter"
  "mock-openai-api|$BASE/mock-openai-api|.|bin/mock-openai-api"
  "openai-api-proxy|$BASE/openai-api-proxy|.|bin/openai-api-proxy"
)

build_failed=()
for b in "${GO_BUILDS[@]}"; do
  IFS='|' read -r _name _dir _pkg _out <<< "$b"
  target="$BASE/$_out"
  need=0
  [ $REBUILD -eq 1 ] && need=1
  [ -f "$target" ] || need=1
  find "$_dir" -name '*.go' -newer "$target" -print -quit 2>/dev/null | grep -q . && need=1
  if [ $need -eq 1 ]; then
    echo "  [build] $_name"
    if ! ( cd "$_dir" && go build -o "$target" "$_pkg" ); then
      echo "  [build] $_name ✘ 编译失败"
      build_failed+=("$_name")
    fi
  fi
done

# 前端：dist 与 backend/www 缺一即重建
frontend_need=0
[ $REBUILD -eq 1 ] && frontend_need=1
[ -f "$BASE/ai-chat-web/dist/index.html" ] || frontend_need=1
[ -f "$BASE/ai-chat-backend/www/index.html" ] || frontend_need=1
if [ $frontend_need -eq 1 ]; then
  echo "  [build] 前端 (pnpm install + build-only)"
  if ! ( cd "$BASE/ai-chat-web" && pnpm install --fetch-retries=15 && pnpm build-only ); then
    echo "  [build] 前端 ✘ 编译失败"
    build_failed+=("前端")
  else
    mkdir -p "$BASE/ai-chat-backend/www"
    cp -r "$BASE/ai-chat-web/dist/." "$BASE/ai-chat-backend/www/"
  fi
fi

if [ ${#build_failed[@]} -ne 0 ]; then
  echo
  echo "✘ 编译失败: ${build_failed[*]}; 请修复后重试 ./start.sh"
  exit 1
fi

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "⚠ 未设置 DEEPSEEK_API_KEY：openai-api-proxy 将使用占位 key，DeepSeek 调用会失败。"
  echo "  使用真实 key：DEEPSEEK_API_KEY=sk-xxx ./start.sh"
  echo "  离线/无 key 调试：把 openai-api-proxy/dev.config.yaml 的 base_url 改回 http://localhost:8083/v1（mock）"
fi

echo "== 启动服务 =="

start_one() {
  local name="$1" port="$2" cwd="$3" cmd="$4" i
  if port_listening "$port"; then
    echo "  [$name] ✔ already running ($port)"
    return 0
  fi
  echo "  [$name] starting ..."
  ( cd "$cwd"; nohup $cmd >"$(logfile "$name")" 2>&1 & echo $! > "$(pidfile "$name")" )
  for i in $(seq 1 30); do
    port_listening "$port" && { echo "  [$name] ✔ $port listening"; return 0; }
    sleep 0.5
  done
  echo "  [$name] ✘ 启动失败, 日志尾部:"
  tail -n 5 "$(logfile "$name")" 2>/dev/null | sed 's/^/    /'
  return 1
}

total=0; ok=0; failed=()
for entry in "${SERVICES[@]}"; do
  IFS='|' read -r name port cwd cmd <<< "$entry"
  total=$((total+1))
  if start_one "$name" "$port" "$cwd" "$cmd"; then ok=$((ok+1)); else failed+=("$name"); fi
done

echo
if [ ${#failed[@]} -eq 0 ]; then
  echo "✔ $ok/$total 服务就绪 → http://localhost:7080"
else
  echo "✘ ${#failed[@]} 个失败: ${failed[*]}; 日志在 runtime/logs/, 用 ./stop.sh 清理后重试"
  exit 1
fi
