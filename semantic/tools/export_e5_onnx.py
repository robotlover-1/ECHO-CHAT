"""一次性导出 multilingual-e5-small → ONNX FP32 → INT8 + MANIFEST。torch 仅用于本工具。

用法:  /tmp/e5exp/bin/python semantic/tools/export_e5_onnx.py [--rev <commit sha>]
REVISION 为该模型 main 分支已固化的 commit sha；产物写 semantic/models/e5s-v1/。
"""
import argparse, hashlib, json, os, sys, urllib.request, shutil, subprocess

MODEL_ID = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"  # main 固化 commit（2026-09-04 确认）
OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "models", "e5s-v1"))
HF = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")


def dl(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        print("dl", url)
        # hf-mirror 403 默认 python-urllib UA；带 huggingface_hub UA 正常放行
        req = urllib.request.Request(url, headers={"User-Agent": "huggingface_hub/0.30.0; python/3.8"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        with open(dest, "wb") as f:
            f.write(data)


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def find_model(dirp):
    """返回 dirp 下引导出的 ONNX 主模型路径（兼容 optimum 两种产物布局）。"""
    cands = [
        os.path.join(dirp, "model.onnx"),
        os.path.join(dirp, "onnx", "model.onnx"),
        os.path.join(dirp, "encoder_model.onnx"),
        os.path.join(dirp, "decoder_model.onnx"),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    raise RuntimeError("未找到 optimum 导出的 ONNX 主模型: %s" % os.listdir(dirp) or dirp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default=REVISION)
    a = ap.parse_args()

    # 1) 下载 tokenizer/config 三件套
    base = f"{HF}/{MODEL_ID}/resolve/{a.rev}"
    for fn in ["config.json", "tokenizer.json", "special_tokens_map.json"]:
        dl(f"{base}/{fn}", os.path.join(OUT, fn))

    # 2) optimum 导出 ONNX FP32 → onnx-fp32/model.onnx
    fp32_tmp = os.path.join(OUT, "onnx-api")
    fp32 = os.path.join(OUT, "onnx-fp32")
    os.makedirs(fp32, exist_ok=True)
    # optimum 1.21+ CLI: python -m optimum.exporters.onnx <flags> <output_dir>
    cmd = [sys.executable, "-m", "optimum.exporters.onnx",
           "-m", MODEL_ID, "--task", "feature-extraction", "--opset", "13",
           fp32_tmp]
    subprocess.run(cmd, check=True)
    os.makedirs(fp32, exist_ok=True)
    shutil.copy(find_model(fp32_tmp), os.path.join(fp32, "model.onnx"))

    # 3) 动态量化 INT8 → model.onnx
    import onnxruntime.quantization as q
    q.quantize_dynamic(os.path.join(fp32, "model.onnx"),
                       os.path.join(OUT, "model.onnx"),
                       weight_type=q.QuantType.QInt8)

    # 4) MANIFEST (sha256)
    ver = subprocess.run([sys.executable, "-c", "import onnxruntime;print(onnxruntime.__version__)"],
                         capture_output=True, text=True).stdout.strip()
    man = {
        "upstream_model": MODEL_ID,
        "upstream_revision": a.rev,
        "export_tool_version": "optimum-onnx",
        "onnxruntime_version": ver or "unknown",
        "quantization_config": {"dynamic": "int8", "weight_type": "QInt8"},
        "model_sha256": sha(os.path.join(OUT, "model.onnx")),
        "tokenizer_sha256": sha(os.path.join(OUT, "tokenizer.json")),
        "config_sha256": sha(os.path.join(OUT, "config.json")),
        "export_timestamp": "2026-09-04",
    }
    with open(os.path.join(OUT, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print("OUT=", os.path.abspath(OUT))
    print(json.dumps(man, indent=2))


if __name__ == "__main__":
    main()
