#!/usr/bin/env bash
# 应用节点一键部署：预检 → 渲染 → 主 compose build/up → 等 /api/readyz → frpc。
# 用法: sudo bash deploy/scripts/deploy-app.sh
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

APP_ENV="${REPO_ROOT}/deploy/app/.env"
APP_ENV_EXAMPLE="${REPO_ROOT}/deploy/app/.env.example"
COMPOSE_MAIN="${REPO_ROOT}/docker/compose.yaml"
COMPOSE_FRPC="${REPO_ROOT}/deploy/app/compose.yaml"
HEALTH_URL="http://127.0.0.1:7080/api/readyz"

[[ $EUID -eq 0 ]] || info "提示：非 root 运行时部分 docker 命令可能需 sudo/组权限"

require_env "${APP_ENV}" "${APP_ENV_EXAMPLE}"
info "预检主机资源..."
preflight_host 8192 40960

info "渲染配置..."
bash "${REPO_ROOT}/deploy/scripts/render-config.sh" app
check_consistency app

info "启动 AnswerMesh 主栈..."
docker compose --env-file "${APP_ENV}" -f "${COMPOSE_MAIN}" up -d --build

info "等待 /api/readyz ..."
for i in $(seq 1 60); do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    info "readyz OK (第 ${i} 次探测)"
    break
  fi
  [[ $i -eq 60 ]] && die "等待 /api/readyz 超时 60s"
  sleep 2
done

info "FRP 配置自检（best-effort，P1-4）..."
if docker run --rm \
     -v "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml:/app/config.yaml:ro" \
     --entrypoint frpc "${FRPC_IMAGE:-snowdreamtech/frpc:0.62.1}" verify -c /app/config.yaml >/dev/null 2>&1; then
  info "frpc verify OK"
else
  info "frpc verify 不可用（镜像 ENTRYPOINT 不同/无此子命令）——跳过；请在目标机 docker image inspect 后核对 FRPC_IMAGE"
fi

info "启动 frpc 独立栈..."
docker compose --env-file "${APP_ENV}" -f "${COMPOSE_FRPC}" up -d
docker compose -f "${COMPOSE_FRPC}" logs --tail=20 frpc

info "完成。继续: bash deploy/scripts/smoke-test.sh app"
