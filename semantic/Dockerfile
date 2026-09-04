FROM python:3.10-slim
WORKDIR /app
ENV PORT 3003

# 服务模块代码（semantic.py 顶层 import parse/decision/embedding/models/ontology）
ADD ./semantic.py /app
ADD ./parse.py /app
ADD ./embedding.py /app
ADD ./models.py /app
ADD ./decision.py /app
ADD ./ontology /app/ontology
# 模型工件（gitignored，不在仓库）：构建期从 GitHub Release 下载 + sha256 校验（MODEL_URL 可覆盖）
ARG MODEL_URL=https://github.com/robotlover-1/ECHO-CHAT/releases/download/models-e5s-v1/multilingual-e5-small-onnx-int8.tar.gz
RUN mkdir -p /app/models/e5s-v1 && \
    curl -fsSL --retry 3 --connect-timeout 15 -o /tmp/e5s.tar.gz "$MODEL_URL" && \
    tar -xzf /tmp/e5s.tar.gz -C /app/models/e5s-v1 --strip-components=1 && \
    rm -f /tmp/e5s.tar.gz && \
    python3 - <<'PY'
import hashlib, json, os
d = "/app/models/e5s-v1"
m = json.load(open(os.path.join(d, "MANIFEST.json")))
for f, k in [("model.onnx", "model_sha256"), ("tokenizer.json", "tokenizer_sha256")]:
    h = hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest()
    assert h == m[k], f"sha mismatch {f}"
print("model sha verified")
PY

# requirements
ADD ./requirements.txt /app

RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade pip
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt

CMD ["sh","-c","nuxt --port ${PORT} --module semantic.py --workers 2"]
