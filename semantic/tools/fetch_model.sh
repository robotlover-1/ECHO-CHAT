#!/usr/bin/env bash
# 下载并解压 multilingual-e5-small ONNX/INT8 模型到 semantic/models/e5s-v1/，并按 MANIFEST.json 校验 sha256。
# 用法:
#   bash semantic/tools/fetch_model.sh
# 环境变量可覆盖:
#   MODEL_URL   强制用指定 URL（跳过镜像列表）
#   MODEL_TARGET(默认 $BASE/semantic/models/e5s-v1)
# 默认按序尝试：GitHub Release 直连 → 常见加速镜像（ghfast.top / gh-proxy.com），
# 失败自动换下一个、带断点续传与重试（部分网络直连 GitHub release 资产会被重置）。
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIRECT="https://github.com/robotlover-1/ECHO-CHAT/releases/download/models-e5s-v1/multilingual-e5-small-onnx-int8.tar.gz"
MIRRORS=(
  "https://ghfast.top/$DIRECT"
  "https://gh-proxy.com/$DIRECT"
)
TARGET="${MODEL_TARGET:-$BASE/semantic/models/e5s-v1}"
TARBALL="$(mktemp /tmp/e5s-model-XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT

mkdir -p "$TARGET"
if [ -n "${MODEL_URL:-}" ]; then
  URLS=("$MODEL_URL")
else
  URLS=("$DIRECT" "${MIRRORS[@]}")
fi

echo "== 下载模型 =="
ok=0
for u in "${URLS[@]}"; do
  echo "  try: $u"
  rm -f "$TARBALL"
  if curl -fL --retry 5 --retry-delay 2 --connect-timeout 15 -C - -o "$TARBALL" "$u"; then
    ok=1; break
  fi
done
if [ $ok -ne 1 ] || [ ! -s "$TARBALL" ]; then
  echo "✘ 下载失败（以上 URL 均不可达）。可设 MODEL_URL 指向可达镜像/内网，或手动拷贝开发机 semantic/models/e5s-v1/。" >&2
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
