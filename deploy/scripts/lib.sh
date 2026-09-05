#!/usr/bin/env bash
# ECHO-CHAT deploy 共享函数。source 本文件后使用。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

die() { echo "[deploy] ERROR: $*" >&2; exit 1; }
info() { echo "[deploy] $*"; }

# 加载 .env：若缺失则提示 cp .env.example。不做 set -x、不回显内容。
require_env() {
  local envfile="$1" example="${2:-}"
  [[ -f "${envfile}" ]] || {
    [[ -n "${example}" ]] \
      && die "缺少 ${envfile}。先执行: cp ${example} ${envfile} 并填写。" \
      || die "缺少 ${envfile}。"
  }
  # 安全加载：逐行解析 KEY=VALUE，去引号后 export；不做 eval/source，值含 &、#、空格也安全。
  # shellcheck disable=SC1090
  while IFS= read -r _line || [[ -n "${_line}" ]]; do
    _line="${_line%$'\r'}"
    [[ -z "${_line}" || "${_line}" == \#* || "${_line}" != *=* ]] && continue
    if [[ "${_line}" == export\ * ]]; then _line="${_line#export }"; fi
    _key="${_line%%=*}"; _val="${_line#*=}"
    # 去掉配对引号（单/双）
    if [[ "${_val}" == \"*\" && "${#_val}" -ge 2 ]]; then _val="${_val%\"}"; _val="${_val#\"}"; fi
    if [[ "${_val}" == \'*\' && "${#_val}" -ge 2 ]]; then _val="${_val%\'}"; _val="${_val#\'}"; fi
    # declare 不再二次展开，赋值字面量
    # shellcheck disable=SC2163
    declare -gx "${_key}=${_val}"
  done < "${envfile}"
}

# 受限 envsubst：只替换显式白名单变量，防误吞 nginx $host/$remote_addr 等。
render_restricted() {
  local template="$1" out="$2" varlist="$3"
  umask 077
  local tmp
  tmp="$(mktemp "${out}.XXXXXX")"
  trap 'rm -f "${tmp}"' RETURN
  # shellcheck disable=SC2086
  envsubst "${varlist}" < "${template}" > "${tmp}"
  guard_no_residue "${tmp}"
  mv "${tmp}" "${out}"
  chmod 600 "${out}"
  info "rendered: ${out}"
}

# 渲染产物不得残留未替换变量。
guard_no_residue() {
  if grep -qE '\$\{?[A-Za-z_][A-Za-z0-9_]*' "$1"; then
    die "残留未替换变量: $(grep -oE '\$\{?[A-Za-z_][A-Za-z0-9_]*' "$1" | sort -u | tr '\n' ' ')"
  fi
}

# 必填项守卫：为空或以 CHANGE_ME/REPLACE_ME/<...>/sk-placeholder 开头 → 退出。
guard_required() {
  local envfile="$1"; shift
  require_env "${envfile}"
  local v
  for v in "$@"; do
    local val="${!v:-}"
    if [[ -z "${val}" ]] || [[ "${val}" == CHANGE_ME* ]] \
       || [[ "${val}" == REPLACE_ME* ]] || [[ "${val}" == '<'*'>' ]] \
       || [[ "${val}" == sk-placeholder* ]]; then
      die "必填变量 ${v} 未设置或仍是占位(见 ${envfile}.example)"
    fi
  done
}

# 基础预检：x86_64 + docker + 资源阈值（内存 MB、磁盘 MB）。
preflight_host() {
  local min_mem_mb="$1" min_disk_mb="$2"
  [[ "$(uname -m)" == x86_64 ]] || die "仅支持 x86_64 (当前 $(uname -m))"
  command -v docker >/dev/null || die "缺少 docker CLI"
  docker version >/dev/null 2>&1 || die "docker daemon 不可用"
  command -v envsubst >/dev/null || die "缺少 envsubst (gettext-base)"
  local mem_kb disk_kb
  mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)
  [[ $(( mem_kb / 1024 )) -ge "${min_mem_mb}" ]] \
    || die "内存不足: ${min_mem_mb}MB 需要, 实际 $(( mem_kb / 1024 ))MB"
  disk_kb=$(df -Pk . | awk 'NR==2{print $4}')
  [[ $(( disk_kb / 1024 )) -ge "${min_disk_mb}" ]] \
    || die "磁盘不足: ${min_disk_mb}MB 需要, 实际 $(( disk_kb / 1024 ))MB"
}

# 端口未占用检查（可选调用）。
assert_port_free() {
  local port="$1"
  if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -qE "[:.]${port}\b"; then
    die "端口 ${port} 已被占用"
  fi
}
