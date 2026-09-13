#!/usr/bin/env bash
# 下载并解压 multilingual-e5-small ONNX/INT8 模型到 semantic/models/e5s-v1/，并按 MANIFEST.json 校验 sha256。
# 用法:
#   bash semantic/tools/fetch_model.sh
# 环境变量可覆盖:
#   MODEL_URL   强制用指定 URL（跳过镜像列表）
#   MODEL_TARGET(默认 $BASE/semantic/models/e5s-v1)
# 默认按序尝试：加速镜像（gh-proxy.com → ghfast.top）→ GitHub Release 直连。
#
# 为什么镜像优先：直连 GitHub release 资产在国内常年只有几 KB/s（实测 4 KB/s，78MB
# 要 5 小时以上），而镜像可达 3.7 MB/s（约 20 秒）。镜像不通时会**快速失败**，代价
# 远小于把时间耗在一条慢连接上。
#
# 低速放弃：--speed-limit/--speed-time 让任何一条连接只要持续 20 秒低于 100 KB/s 就
# 主动断开换下一个。注意 --retry 只处理"失败"，不处理"慢"——没有这个的话，一条
# 4 KB/s 的连接会被一路挂到最后（这正是此前踩的坑）。
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIRECT="https://github.com/robotlover-1/Answermesh/releases/download/models-e5s-v1/multilingual-e5-small-onnx-int8.tar.gz"
MIRRORS=(
  "https://gh-proxy.com/$DIRECT"
  "https://ghfast.top/$DIRECT"
)
SPEED_LIMIT="${MODEL_SPEED_LIMIT:-102400}"   # 100 KB/s
SPEED_TIME="${MODEL_SPEED_TIME:-20}"         # 持续 20s 低于阈值就放弃
TARGET="${MODEL_TARGET:-$BASE/semantic/models/e5s-v1}"
TARBALL="$(mktemp /tmp/e5s-model-XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT

mkdir -p "$TARGET"
if [ -n "${MODEL_URL:-}" ]; then
  URLS=("$MODEL_URL")
else
  URLS=("${MIRRORS[@]}" "$DIRECT")
fi

echo "== 下载模型（约 78MB；低于 ${SPEED_LIMIT} B/s 持续 ${SPEED_TIME}s 会自动换源）=="
ok=0
for u in "${URLS[@]}"; do
  echo "  try: $u"
  rm -f "$TARBALL"
  # 每次换源都从头下：不同镜像返回的字节流未必可续传，-C - 跨源续传有拼坏文件的风险。
  if curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 \
          --speed-limit "$SPEED_LIMIT" --speed-time "$SPEED_TIME" \
          --progress-bar -o "$TARBALL" "$u"; then
    ok=1; break
  fi
  echo "    ↳ 跳过（不可达或速度不达标）"
done
if [ $ok -ne 1 ] || [ ! -s "$TARBALL" ]; then
  echo "✘ 下载失败（以上 URL 均不可达/过慢）。可设 MODEL_URL 指向可达镜像/内网，或手动拷贝开发机 semantic/models/e5s-v1/。" >&2
  exit 1
fi
echo "== 解压到 $TARGET =="
tar -xzf "$TARBALL" -C "$TARGET" --strip-components=1

echo "== 校验 sha256 (对照 MANIFEST.json) =="
python3 - "$TARGET" <<'PY'
import hashlib, json, os, sys
d = sys.argv[1]
man = json.load(open(os.path.join(d, "MANIFEST.json"), encoding="utf-8"))
for fn, key in (("model.onnx", "model_sha256"), ("tokenizer.json", "tokenizer_sha256")):
    p = os.path.join(d, fn)
    if not os.path.exists(p):
        raise SystemExit(f"缺少 {fn}")
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != man[key]:
        raise SystemExit(f"{fn} sha256 不符: {h[:16]}… != {man[key][:16]}…")
    print(f"  ok {fn}  {h[:16]}…")
print("模型文件校验通过")
PY

echo "完成 → $TARGET"
ls -la "$TARGET" | grep -E 'model.onnx|tokenizer.json|MANIFEST'
