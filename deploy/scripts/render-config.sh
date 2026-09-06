#!/usr/bin/env bash
# 部署前渲染：把 .env 注入模板，产出 gitignore 的最终配置。
# 用法: render-config.sh app | edge
#   app  : docker/config/{backend,service}.yaml + deploy/app/tunnel/frpc.yaml
#   edge : deploy/edge/tunnel/frps.yaml + deploy/edge/nginx/echo-chat.conf
# 说明: nginx 完整/引导 conf 由 deploy-edge.sh 按证书阶段决定渲染哪个模板到 echo-chat.conf。
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

case "${1:-}" in
  app)
    APP_ENV="${REPO_ROOT}/deploy/app/.env"
    guard_required "${APP_ENV}" \
      PUBLIC_DOMAIN FRP_AUTH_TOKEN FRP_SERVER_ADDR \
      VECTOR_DB_URL VECTOR_DB_USER VECTOR_DB_PWD
    render_restricted "${REPO_ROOT}/docker/config/backend.yaml.envsubst" \
      "${REPO_ROOT}/docker/config/backend.yaml" \
      '${CHAT_SERVICE_TOKEN} ${KVSTORE_HOST} ${KVSTORE_PORT} ${MYSQL_DSN}'
    render_restricted "${REPO_ROOT}/docker/config/service.yaml.envsubst" \
      "${REPO_ROOT}/docker/config/service.yaml" \
      '${CHAT_SERVICE_TOKEN} ${FILTER_SERVICE_TOKEN} ${PROXY_API_KEY} ${KVSTORE_HOST} ${KVSTORE_PORT} ${MYSQL_DSN} ${VECTOR_DB_URL} ${VECTOR_DB_USER} ${VECTOR_DB_PWD} ${VECTOR_DB_NAME}'
    render_restricted "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml.envsubst" \
      "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml" \
      '${FRP_SERVER_ADDR} ${FRP_BIND_PORT} ${FRP_AUTH_TOKEN} ${PUBLIC_DOMAIN}'
    ;;
  edge)
    EDGE_ENV="${REPO_ROOT}/deploy/edge/.env"
    guard_required "${EDGE_ENV}" \
      PUBLIC_DOMAIN ADMIN_EMAIL FRP_AUTH_TOKEN
    render_restricted "${REPO_ROOT}/deploy/edge/tunnel/frps.yaml.envsubst" \
      "${REPO_ROOT}/deploy/edge/tunnel/frps.yaml" \
      '${FRP_BIND_PORT} ${FRP_VHOST_HTTP_PORT} ${FRP_AUTH_TOKEN}'
    # echo-chat.conf 由 deploy-edge.sh 决定写入哪个模板，此处不渲染。
    ;;
  *) die "用法: render-config.sh app|edge" ;;
esac
