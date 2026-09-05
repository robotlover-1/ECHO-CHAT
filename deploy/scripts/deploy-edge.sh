#!/usr/bin/env bash
# 公网入口节点一键部署（两阶段 TLS）：
#   证书缺失 → bootstrap Nginx(80) → certbot webroot → 原子换全量 HTTPS conf → reload
#   证书已有 → 直接全量。幂等：不重复申请、不中断服务。
# 用法: sudo bash deploy/scripts/deploy-edge.sh
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

EDGE_DIR="${REPO_ROOT}/deploy/edge"
EDGE_ENV="${EDGE_DIR}/.env"
NGINX_CONF="${EDGE_DIR}/nginx/echo-chat.conf"
BOOT_TMPL="${EDGE_DIR}/nginx/echo-chat.bootstrap.conf.envsubst"
FULL_TMPL="${EDGE_DIR}/nginx/echo-chat.conf.envsubst"
FRPS_YAML="${EDGE_DIR}/tunnel/frps.yaml"

[[ $EUID -eq 0 ]] || die "deploy-edge 需要 root（写 /etc/letsencrypt 与 nginx reload）"

require_env "${EDGE_ENV}" "${EDGE_DIR}/.env.example"
info "预检 edge 主机..."
preflight_host 2048 40960
command -v certbot >/dev/null || die "缺少 certbot；先: apt-get install -y certbot"
command -v dig >/dev/null || die "缺少 dig；先: apt-get install -y dnsutils"

info "渲染 edge 配置..."
bash "${REPO_ROOT}/deploy/scripts/render-config.sh" edge

CERT_DIR="/etc/letsencrypt/live/${PUBLIC_DOMAIN}"
FULLCHAIN="${CERT_DIR}/fullchain.pem"

render_full() {  # 全量 conf → nginx conf 路径（先在暂存目录校验候选，再落盘）
  local stage cand
  stage="$(mktemp -d "${NGINX_CONF}.stage.XXXXXX")"
  cand="${stage}/echo-chat.conf"   # 镜像 conf.d 只 include *.conf
  trap 'rm -rf "${stage}" "${NGINX_CONF}.candidate"*' RETURN
  umask 077
  envsubst '${PUBLIC_DOMAIN} ${FRP_VHOST_HTTP_PORT}' < "${FULL_TMPL}" > "${cand}"
  guard_no_residue "${cand}"
  # 候选校验：一次性容器只挂载含候选 conf 的 stage 目录 + 真实证书 → nginx -t
  if ! docker run --rm \
       -v "${stage}:/etc/nginx/conf.d:ro" \
       -v /etc/letsencrypt:/etc/letsencrypt:ro \
       -v /var/www/certbot:/var/www/certbot:ro \
       nginx:1.27-alpine nginx -t; then
    die "nginx -t 校验失败(全量 conf 候选)，未改动现有 conf"
  fi
  rm -f "${NGINX_CONF}.prev"
  [[ -f "${NGINX_CONF}" ]] && cp -a "${NGINX_CONF}" "${NGINX_CONF}.prev"
  cp -a "${cand}" "${NGINX_CONF}"   # 覆写原 inode 内容，bind 挂载容器 reload 即可见
  chmod 644 "${NGINX_CONF}"
}

render_bootstrap() {
  umask 077
  envsubst '${PUBLIC_DOMAIN}' < "${BOOT_TMPL}" > "${NGINX_CONF}"
  guard_no_residue "${NGINX_CONF}"
  chmod 644 "${NGINX_CONF}"
}

start_frps() {
  docker compose -f "${EDGE_DIR}/compose.yaml" up -d frps
  docker compose -f "${EDGE_DIR}/compose.yaml" logs --tail=20 frps
}

start_nginx() {
  docker compose -f "${EDGE_DIR}/compose.yaml" up -d nginx
}

reload_nginx() {
  docker compose -f "${EDGE_DIR}/compose.yaml" exec nginx nginx -s reload || \
    docker compose -f "${EDGE_DIR}/compose.yaml" restart nginx
}

info "启动 frps..."
start_frps

if [[ -f "${FULLCHAIN}" ]]; then
  info "证书已存在: ${FULLCHAIN}，直接部署全量 HTTPS conf"
  render_full
  if docker compose -f "${EDGE_DIR}/compose.yaml" ps --status running nginx >/dev/null 2>&1; then
    reload_nginx
  else
    start_nginx
  fi
else
  info "未发现证书，进入两阶段：bootstrap → certbot → HTTPS"
  render_bootstrap
  start_nginx
  sleep 2

  info "校验 DNS..."
  if [[ "$(dig +short "${PUBLIC_DOMAIN}" | head -1)" != "${PUBLIC_IP:-}" ]] \
     && ! ip -4 addr show | grep -qF "$(dig +short "${PUBLIC_DOMAIN}" | head -1)"; then
    # 允许 PUBLC_IP 显式设置；否则退化为仅提示（部分云 NAT 环境下本机 IP 探测困难）。
    [[ -n "${PUBLIC_IP:-}" ]] && die "DNS ${PUBLIC_DOMAIN} 未指向 ${PUBLIC_IP}"
    info "提示：无法确认 DNS 指向本机，certbot 若失败请检查解析与安全组(80)"
  fi

  info "申请证书(webroot)..."
  if ! certbot certonly --webroot -w /var/www/certbot \
       -d "${PUBLIC_DOMAIN}" --email "${ADMIN_EMAIL}" \
       --agree-tos --no-eff-email --non-interactive; then
    info "证书申请失败：保留 bootstrap(80) 供排查。修好后再跑本脚本（幂等）。"
    exit 1
  fi
  [[ -f "${FULLCHAIN}" ]] || die "certbot 成功但证书文件缺失: ${FULLCHAIN}"

  render_full
  reload_nginx
fi

info "HTTPS 验收..."
curl -fsS "https://${PUBLIC_DOMAIN}/api/health" >/dev/null \
  && info "https://${PUBLIC_DOMAIN} 验收通过" \
  || die "HTTPS 冒烟失败，查 nginx 日志: docker compose -f ${EDGE_DIR}/compose.yaml logs --tail=50 nginx"

info "续期 deploy hook 建议写入 /etc/letsencrypt/renewal-hooks/deploy/reload-echo-nginx.sh（见 deploy/README.md）"
