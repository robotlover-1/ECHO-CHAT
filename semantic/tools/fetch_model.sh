#!/usr/bin/env bash
# 下载并解压 multilingual-e5-small ONNX/INT8 模型到 semantic/models/e5s-v1/，并按 MANIFEST.json 校验 sha256。
# 用法:
#   bash semantic/tools/fetch_model.sh
# 环境变量可覆盖:
#   MODEL_URL   (默认 GitHub Release models-e5s-v1 tarball)
#   MODEL_TARGET(默认 $BASE/semantic/models/e5s-v1)
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODEL_URL="${MODEL_URL:-https://github.com/robotlover-1/ECHO-CHAT/releases/download/models-e5s-v1/multilingual-e5-small-onnx-int8.tar.gz}"
TARGET="${MODEL_TARGET:-$BASE/semantic/models/e5s-v1}"
TARBALL="$(mktemp /tmp/e5s-model-XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT

mkdir -p "$TARGET"
echo "== 下载模型 =="
echo "  $MODEL_URL"
curl -fL --retry 3 --connect-timeout 15 -o "$TARBALL" "$MODEL_URL"
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
