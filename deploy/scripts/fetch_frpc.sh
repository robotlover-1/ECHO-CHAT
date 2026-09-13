#!/usr/bin/env bash
# 下载 frp 客户端二进制到 bin/frpc，并用官方 frp_sha256_checksums.txt 校验 sha256。
#
# 用法:
#   bash deploy/scripts/fetch_frpc.sh
# 环境变量可覆盖:
#   FRP_VERSION   版本号（默认 0.62.1，**须与云端 frps 同版本**）
#   FRPC_BIN      安装路径（默认 $BASE/bin/frpc）
#   FRP_URL       强制指定下载 base URL（跳过镜像列表），如内网镜像
#   FRP_SHA256    手工指定 tar.gz 的 sha256（官方校验和拉不到时用）
#
# 来源顺序：加速镜像（gh-proxy.com → ghfast.top）→ GitHub 直连。直连在国内常年只有
# 几 KB/s，镜像可达 MB/s；--speed-limit/--speed-time 让慢连接主动让位而不是一路挂着。
# 理由与 fetch_model.sh 同，见该脚本注释。
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${FRP_VERSION:-0.62.1}"
TARGET="${FRPC_BIN:-$BASE/bin/frpc}"

case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "✘ 不支持的架构: $(uname -m)（仅验证过 linux amd64/arm64）" >&2; exit 1 ;;
esac
ASSET="frp_${VERSION}_linux_${ARCH}.tar.gz"
MEMBER="frp_${VERSION}_linux_${ARCH}/frpc"

if [ -n "${FRP_URL:-}" ]; then
  BASE_URLS=("${FRP_URL%/}")
else
  REL="https://github.com/fatedier/frp/releases/download/v${VERSION}"
  BASE_URLS=("https://gh-proxy.com/$REL" "https://ghfast.top/$REL" "$REL")
fi

SPEED_LIMIT="${FRP_SPEED_LIMIT:-102400}"   # 100 KB/s
SPEED_TIME="${FRP_SPEED_TIME:-20}"         # 持续 20s 低于阈值就换源
# 免费公共镜像会限流：短时间连续拉过几个大文件后，gh-proxy 会短暂拒绝，几十秒后恢复。
# 所以一轮全失败不等于"网络不通"，默认歇 10s 再走一轮。
ROUNDS="${FRP_FETCH_ROUNDS:-2}"
RETRY_WAIT="${FRP_FETCH_WAIT:-10}"

TMPD="$(mktemp -d /tmp/frp-fetch-XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT

# 轮流尝试各来源，任一成功即返回；整轮失败则等待后重来。换源时从头下
# （不同镜像的字节流不保证可续传，-C - 跨源续传有拼坏文件的风险）。
fetch_to() {  # $1=远端文件名 $2=本地输出路径
  local name="$1" out="$2" base round u
  for round in $(seq 1 "$ROUNDS"); do
    # 注意：不能写 for u in "${BASE_URLS[@]}/$name" —— bash 只会把 "/$name" 拼到
    # 数组**最后一个**元素上，前几个会拿到没有文件名的 URL。必须逐个拼。
    for base in "${BASE_URLS[@]}"; do
      u="$base/$name"
      echo "  try: $u"
      rm -f "$out"
      if curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 \
              --speed-limit "$SPEED_LIMIT" --speed-time "$SPEED_TIME" \
              --progress-bar -o "$out" "$u"; then
        return 0
      fi
      echo "    ↳ 跳过（不可达或速度不达标）"
    done
    if [ "$round" -lt "$ROUNDS" ]; then
      echo "  … 第 ${round} 轮全部来源失败，${RETRY_WAIT}s 后重试（镜像限流通常是暂时的）"
      sleep "$RETRY_WAIT"
    fi
  done
  return 1
}

echo "== 校验和来源 =="
if [ -n "${FRP_SHA256:-}" ]; then
  EXPECT="$FRP_SHA256"
  echo "  使用 FRP_SHA256 指定的值"
else
  if ! fetch_to frp_sha256_checksums.txt "$TMPD/sums.txt"; then
    echo "✘ 拉不到官方校验和 frp_sha256_checksums.txt。" >&2
    echo "  可改用 FRP_SHA256=<sha256> 手工指定（值见官方 release 的该文件）。" >&2
    exit 1
  fi
  EXPECT="$(awk -v a="$ASSET" '$2 == a {print $1}' "$TMPD/sums.txt" | head -1)"
  if [ -z "$EXPECT" ]; then
    echo "✘ 校验和文件里没有 $ASSET（版本号 $VERSION 是否正确？）" >&2
    exit 1
  fi
  echo "  官方 sha256: $EXPECT"
fi

echo "== 下载 frp 客户端 $VERSION ($ARCH) =="
if ! fetch_to "$ASSET" "$TMPD/$ASSET"; then
  echo "✘ 下载失败（以上 URL 均不可达/过慢）。可设 FRP_URL 指向可达镜像，或手动下载后放到 $TARGET。" >&2
  exit 1
fi

echo "== 校验 sha256 =="
ACTUAL="$(sha256sum "$TMPD/$ASSET" | awk '{print $1}')"
if [ "$ACTUAL" != "$EXPECT" ]; then
  echo "✘ sha256 不符，已中止（文件可能损坏或被篡改）:" >&2
  echo "    期望 $EXPECT" >&2
  echo "    实际 $ACTUAL" >&2
  exit 1
fi
echo "  ok $ACTUAL"

echo "== 安装到 $TARGET =="
mkdir -p "$(dirname "$TARGET")"
# 只取 tar 里的 frpc，不要 frps（云端 frps 走 docker 镜像，本地不需要）。
tar -xzf "$TMPD/$ASSET" -C "$TMPD" --strip-components=1 "$MEMBER"
install -m 0755 "$TMPD/frpc" "$TARGET"

echo "== 自检 =="
"$TARGET" --version
echo "完成 → $TARGET"
echo "提示：客户端版本必须与云端 frps 一致（本项目 0.62.1）。"
