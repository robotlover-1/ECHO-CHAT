import json
import os
import pytest

HERE = os.path.dirname(__file__)
D = os.path.normpath(os.path.join(HERE, "..", "models", "e5s-v1"))


def test_manifest():
    m = json.load(open(os.path.join(D, "MANIFEST.json"), encoding="utf-8"))
    assert m["upstream_model"] == "intfloat/multilingual-e5-small"
    assert isinstance(m.get("model_sha256"), str) and len(m["model_sha256"]) == 64
    assert isinstance(m.get("tokenizer_sha256"), str) and len(m["tokenizer_sha256"]) == 64
    assert isinstance(m.get("quantization_config"), dict)
    assert os.path.exists(os.path.join(D, "model.onnx"))
    assert os.path.exists(os.path.join(D, "tokenizer.json"))


def test_manifest_sha():
    import hashlib
    m = json.load(open(os.path.join(D, "MANIFEST.json"), encoding="utf-8"))
    h = hashlib.sha256(open(os.path.join(D, "model.onnx"), "rb").read()).hexdigest()
    assert h == m["model_sha256"]
