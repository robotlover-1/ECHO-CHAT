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
# 模型工件（e5s-v1: model.onnx(INT8) + tokenizer.json + MANIFEST.json + onnx-fp32(对照)）
ADD ./models/e5s-v1 /app/models/e5s-v1

# requirements
ADD ./requirements.txt /app

RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade pip
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt

CMD ["sh","-c","nuxt --port ${PORT} --module semantic.py --workers 2"]
