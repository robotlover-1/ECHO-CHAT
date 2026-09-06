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
preflight_host 1536 20480
command -v certbot >/dev/null || die "缺少 certbot；先: apt-get install -y certbot"
command -v dig >/dev/null || die "缺少 dig；先: apt-get install -y dnsutils"

info "渲染 edge 配置..."
bash "${REPO_ROOT}/deploy/scripts/render-config.sh" edge

info "FRP 服务端配置自检(best-effort，P1-6)..."
if docker run --rm \
     -v "${EDGE_DIR}/tunnel/frps.yaml:/app/config.yaml:ro" \
     --entrypoint frps "${FRPS_IMAGE:-snowdreamtech/frps:0.62.1}" verify -c /app/config.yaml >/dev/null 2>&1; then
  info "frps verify OK"
else
  info "frps verify 不可用（镜像 ENTRYPOINT 不同/无此子命令）——跳过；上线前 docker image inspect 核对"
fi

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

# reload 成功即删除 .prev；失败则回滚 .prev 再重启；两者皆败才 die。
reload_nginx() {
  if docker compose -f "${EDGE_DIR}/compose.yaml" exec -T nginx nginx -s reload; then
    rm -f "${NGINX_CONF}.prev"
    return 0
  fi
  info "nginx reload 失败：尝试回滚 .prev 并 restart"
  if [[ -f "${NGINX_CONF}.prev" ]]; then
    cp -a "${NGINX_CONF}.prev" "${NGINX_CONF}"
    chmod 644 "${NGINX_CONF}"
  fi
  if docker compose -f "${EDGE_DIR}/compose.yaml" restart nginx; then
    info "已回滚 .prev 并 restart nginx"
    rm -f "${NGINX_CONF}.prev"
    return 0
  fi
  die "nginx reload 与 restart 均失败；.prev 已回滚到配置，请人工检查"
}

# 断言 compose 服务 running：ps -q 非空 且 docker inspect 状态为 running。
assert_service_running() {
  local service="$1" cid status
  cid="$(docker compose -f "${EDGE_DIR}/compose.yaml" ps -q "${service}" 2>/dev/null || true)"
  [[ -n "${cid}" ]] || die "${service} 容器不存在"
  status="$(docker inspect -f '{{.State.Status}}' "${cid}" 2>/dev/null || true)"
  [[ "${status}" == "running" ]] || die "${service} 状态=${status:-未知}(期望 running)"
}

# nginx 是否已在运行：ps -q 得 cid + docker inspect==running。不依赖 compose ps 退出码
# (首次运行时服务未创建，`compose ps --status running` 可能返回 0 导致误走 reload)。
nginx_up() {
  local cid
  cid="$(docker compose -f "${EDGE_DIR}/compose.yaml" ps -q nginx 2>/dev/null || true)"
  [[ -z "${cid}" ]] && return 1
  [[ "$(docker inspect -f '{{.State.Status}}' "${cid}" 2>/dev/null || true)" == "running" ]]
}

# 宿主机 TCP 就绪等待（bash /dev/tcp，不依赖镜像工具）。超时默认 30s。
wait_tcp() {
  local host="$1" port="$2" secs="${3:-30}" i=0
  while (( i < secs * 2 )); do
    if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
      return 0
    fi
    sleep 0.5
    i=$(( i + 1 ))
  done
  die "等待 TCP ${host}:${port} 超时 ${secs}s"
}

info "启动 frps..."
start_frps
assert_service_running frps
wait_tcp 127.0.0.1 "${FRP_BIND_PORT:-39000}"

if [[ -f "${FULLCHAIN}" ]]; then
  info "证书已存在: ${FULLCHAIN}，直接部署全量 HTTPS conf"
  render_full
  if nginx_up; then
    reload_nginx
  else
    start_nginx
  fi
else
  info "未发现证书，进入两阶段：bootstrap → certbot → HTTPS"
  render_bootstrap
  # nginx 已在跑(如证书被删后的重跑)→ reload 应用 bootstrap；否则首启。
  if nginx_up; then
    reload_nginx
  else
    start_nginx
  fi
  sleep 2

  info "校验 DNS..."
  dns_ip="$(dig +short "${PUBLIC_DOMAIN}" | head -1)"
  if [[ -n "${PUBLIC_IP:-}" ]]; then
    # 显式设了公网 IP：解析必须精确命中，无解析/不一致都 die(避免带假域名去 certbot)。
    [[ "${dns_ip}" == "${PUBLIC_IP}" ]] \
      || die "DNS ${PUBLIC_DOMAIN} 未指向 ${PUBLIC_IP}(当前: ${dns_ip:-无解析})"
  elif [[ -n "${dns_ip}" ]] && ! ip -4 addr show | grep -qF "${dns_ip}"; then
    # 未设 PUBLIC_IP：仅提示(部分云 NAT 下本机 IP 探测困难)。
    info "提示：域名解析到 ${dns_ip}，非本机网卡地址——确认安全组/负载均衡把 80 转发到本机"
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

check_consistency edge
assert_service_running frps
assert_service_running nginx
wait_tcp 127.0.0.1 443

info "云端自检: TLS/Nginx(/edge-healthz)..."
curl -fsS --max-time 15 "https://${PUBLIC_DOMAIN}/edge-healthz" >/dev/null \
  || die "公网 DNS/TLS/Nginx 自检失败(/edge-healthz)——查 80/443 安全组与 nginx 日志"

info "云端自检: ECHO-CHAT 业务链路(/api/health)..."
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://${PUBLIC_DOMAIN}/api/health" 2>/dev/null)" || code=000
case "${code}" in
  200)            info "ECHO-CHAT 链路已连通，建议再跑 smoke-test.sh edge/e2e" ;;
  502|503|504)    info "edge 正常(HTTP ${code})；本地 frpc/ECHO-CHAT 未就绪——frpc 上线后再跑 smoke-test.sh edge" ;;
  000)            die "HTTPS 连接失败(000)";;
  *)              die "HTTPS 返回非预期状态 ${code}，检查 Nginx 路由/后端" ;;
esac

info "续期 deploy hook 建议写入 /etc/letsencrypt/renewal-hooks/deploy/reload-echo-nginx.sh（见 deploy/README.md）"
