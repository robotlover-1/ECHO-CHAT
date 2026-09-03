# semantic（语义检索独立服务）

承接 ECHO-CHAT 语义缓存的向量生成与规则校验，与 tokenizer（计 token）解耦。
- `/embed`：256 维 FNV 哈希加权词面嵌入 + 主题/意图/上下文/状态抽取（契约见 spec）
- `/rerank`：主题/意图/语言/操作冲突硬拒 + 关键词 Jaccard
- `/healthz`：健康检查（embedding_type=fnv_hash, dimension=256）

## 镜像构建（本机 registry 默认）
docker build -t 192.168.233.128:5000/2404/semantic:1.0.0 .

## 服务启动
docker service create --name 2404-semantic \
-p 3003:3003 --replicas 2 --with-registry-auth \
192.168.233.128:5000/2404/semantic:1.0.0

## 本地进程直跑
nuxt --port 3003 --module semantic.py --workers 2
