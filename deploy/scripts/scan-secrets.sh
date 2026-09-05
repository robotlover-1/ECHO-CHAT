#!/usr/bin/env bash
# Secret 启发式扫描：对给定路径集扫描高熵/云凭据/占位。CI/提交前可调用。
# 用法: bash scan-secrets.sh [path...]   (默认: 全部 render 产物 + docker/config)
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TARGETS=("$@")
[[ ${#TARGETS[@]} -eq 0 ]] && TARGETS=(
  "${REPO_ROOT}/docker/config" "${REPO_ROOT}/deploy" )

# 模式：云 AccessKey/SecretKey、sk- 密钥、长 hex/base64、私钥、占位
patterns=(
  'AKID[0-9A-Za-z]{13,}'
  'LTAI[0-9A-Za-z]{12,}'
  'sk-[0-9A-Za-z]{20,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'access[_-]?(key|secret|token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{16,}'
  'password[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}'
)
hits=0
while IFS= read -r f; do
  for p in "${patterns[@]}"; do
    if grep -Eiq "$p" "$f"; then
      echo "[scan] 命中 '$p' → $f"
      hits=$((hits+1))
    fi
  done
  # 渲染产物残留占位
  if grep -qE 'CHANGE_ME|REPLACE_ME|<[^>]*>|sk-placeholder' "$f"; then
    echo "[scan] 占位残留 → $f"
    hits=$((hits+1))
  fi
done < <(find "${TARGETS[@]}" -type f \( -name '*.yaml' -o -name '*.env*' -o -name '*.conf' -o -name '*.sh' \) 2>/dev/null)

if [[ "$hits" -gt 0 ]]; then
  die "发现 $hits 处可疑内容（检查是否真实凭据或需清理占位）"
fi
info "scan-secrets 通过"
