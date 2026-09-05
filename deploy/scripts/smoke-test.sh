#!/usr/bin/env bash
# ECHO-CHAT 公网隧道分层验收（主方案 §9）。层数越低越先验证。
# 用法:
#   bash smoke-test.sh app                              # 应用节点本地 7080
#   bash smoke-test.sh edge                             # 入口节点: frps host 路由 + https
#   DOMAIN=chat.example.com bash smoke-test.sh e2e      # 端到端(公网)
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

MODE="${1:-app}"
DOMAIN="${DOMAIN:-chat.example.com}"
APP_BASE="http://127.0.0.1:7080"
EDGE_VHOST="http://127.0.0.1:${FRP_VHOST_HTTP_PORT:-39001}"
AUTH="${AUTH:-}"   # 不传则跳过需鉴权用例

req() { curl -fsS -o /dev/null -w '%{http_code}' "$@"; }

case "${MODE}" in
  app)
    info "L1 应用节点本地..."
    code=$(req "${APP_BASE}/api/readyz") && [[ "$code" == "200" ]] || die "L1 /api/readyz = $code"
    code=$(req "${APP_BASE}/api/health") && [[ "$code" == "200" ]] || die "L1 /api/health = $code"
    ;;
  edge)
    info "L2 frps host 路由(绕过 Nginx)..."
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${DOMAIN}" "${EDGE_VHOST}/api/health") \
      && [[ "$code" == "200" ]] || die "L2 frps vhost = $code (检查 frpc customDomains/frps token)"
    info "L3 HTTPS..."
    code=$(req "https://${DOMAIN}/api/health") && [[ "$code" == "200" ]] || die "L3 https health = $code"
    ;;
  e2e)
    info "L4 流式聊天..."
    [[ -n "${AUTH}" ]] || die "e2e 需 AUTH=<登录token>"
    out="$(mktemp)"
    trap 'rm -f "${out}"' RETURN
    start=$(date +%s%N)
    # -N 关缓冲；记录首字节到文件；统计响应时间
    curl -N -sS "https://${DOMAIN}/api/chat-process" \
      -H "Authorization: ${AUTH}" -H 'Content-Type: application/json' \
      --data '{"prompt":"数到三","options":{}}' > "${out}" &
    cpid=$!
    first_byte=0
    while kill -0 "$cpid" 2>/dev/null; do
      if [[ -s "${out}" ]]; then
        first_byte=$(( ($(date +%s%N) - start) / 1000000 )); break
      fi
      sleep 0.05
    done
    wait "$cpid" || true
    end=$(( ($(date +%s%N) - start) / 1000000 ))
    info "首字节 ${first_byte}ms / 总时长 ${end}ms / 字节 $(wc -c < "${out}")"
    [[ -s "${out}" ]] || die "e2e 无任何响应体(连接/鉴权/上游失败)"
    chunks=$(grep -c '^\n' "${out}" || true)
    [[ "$chunks" -ge 2 ]] || die "chunk 数不足(=$chunks)，疑似代理缓冲聚合"
    [[ "${first_byte}" -lt 30000 ]] || die "首字节超过 30s，流式不通"
    ;;
  *) die "用法: smoke-test.sh app|edge|e2e" ;;
esac
info "SMOKE ${MODE} PASS"
