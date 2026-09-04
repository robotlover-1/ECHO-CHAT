#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# 必须用普通用户运行：sudo 会把 $HOME 切到 /root，$PATH 里依赖的 $HOME/.local/bin
# （pip --user 装的 nuxt/go 工具）就找不到；且服务均为高端口、无特权需求。
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

# kvstore(C, 子模块)：二进制缺失 / 存在更新的 .c/.h / --rebuild 时 make（新 clone 无二进制需自动补）
KV_TARGET="$BASE/kvstore/kvstore/kvstore"
kv_need=0
[ $REBUILD -eq 1 ] && kv_need=1
[ -f "$KV_TARGET" ] || kv_need=1
if [ $kv_need -eq 0 ]; then
  find "$BASE/kvstore/kvstore" \( -name '*.c' -o -name '*.h' \) -newer "$KV_TARGET" -print -quit 2>/dev/null | grep -q . && kv_need=1
fi
if [ $kv_need -eq 1 ]; then
  echo "  [build] kvstore (make)"
  if ! ( cd "$BASE/kvstore/kvstore" && make ); then
    echo "  [build] kvstore ✘ 编译失败（需 make/gcc，见 kvstore/README）"
    build_failed+=("kvstore")
  fi
fi

if [ ${#build_failed[@]} -ne 0 ]; then
  echo
  echo "✘ 编译失败: ${build_failed[*]}; 请修复后重试 ./start.sh"
  exit 1
fi

if [ ! -f "$BASE/semantic/models/e5s-v1/model.onnx" ]; then
  if [ "${ECHO_FETCH_MODEL:-}" = "1" ]; then
    echo "  [model] 未找到模型，自动从 GitHub Release 下载（ECHO_FETCH_MODEL=1）..."
    if ! bash "$BASE/semantic/tools/fetch_model.sh"; then
      echo "  [model] ✘ 模型下载/校验失败，见 semantic/tools/fetch_model.sh"
    fi
  fi
  if [ ! -f "$BASE/semantic/models/e5s-v1/model.onnx" ]; then
    echo "⚠ 未找到 semantic 模型文件（semantic/models/e5s-v1/model.onnx）：语义缓存将不可用（/embed 会 500 → 优雅 miss，聊天不受影响）。"
    echo "  获取：bash semantic/tools/fetch_model.sh   # 从 GitHub Release 下载+sha256 校验（约 79MB）"
    echo "       或手动拷贝开发机 semantic/models/e5s-v1/。Release: https://github.com/robotlover-1/ECHO-CHAT/releases/tag/models-e5s-v1"
  fi
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
