# AnswerMesh（ai 助手）

零声教学 AI 助手微服务版。独立仓库，kvstore 以 submodule 引入（`kvstore/`），用法与 kvstore 自身引用 NtyCo 一致。

## 仓库结构

```text
AnswerMesh/
├── ai-chat-backend/     ← Go HTTP 网关（页面 + /api/chat-process）
├── ai-chat-service/     ← Go gRPC 核心服务（对话编排 + 语义缓存）
├── keywords-filter/     ← 敏感词/关键词过滤
├── openai-api-proxy/    ← DeepSeek 反向代理（key 走环境变量）
├── mock-openai-api/     ← 离线 mock（无 key 调试用）
├── tokenizer/           ← token 计数（tiktoken，仅计 token）
├── semantic/            ← 语义检索独立服务(:3003)：e5 嵌入 + parse + decision + 安全指纹
├── ai-chat-web/         ← Vue3 前端
├── kvstore/             ← submodule → robotlover-1/pocket-kv（自研 Redis）
└── start.sh / stop.sh   ← 一键启停
```

> 语义检索依赖大模型文件 `semantic/models/e5s-v1/model.onnx`（multilingual-e5-small，ONNX/INT8）。该目录 **gitignored**，不在仓库内；缺失时 semantic 能启动但语义缓存不可用（`start.sh` 会打印提示、Go 优雅 miss，聊天不受影响）。

## 快速开始

```bash
git clone --recurse-submodules https://github.com/robotlover-1/Answermesh.git
cd AnswerMesh

# kvstore(C)与前端 dist 若缺失，start.sh 会自动 make / pnpm 重建；也可手动：
#   ( cd kvstore/kvstore && make )          # 子模块 VSEARCH 前缀参数版
# 语义模型（可选，语义缓存需要；一次即可）：
bash semantic/tools/fetch_model.sh          # 从 GitHub Release 下载+sha256 校验（~79MB）
#   或 ECHO_FETCH_MODEL=1 ./start.sh 让 start.sh 缺模型时自动下载。

DEEPSEEK_API_KEY=sk-xxx ./start.sh   # key 走环境变量，勿提交 git

# 另一种环境方案（各节点免装依赖/模型）：全栈 Docker —— 见 docker/README.md
#   cd docker && DEEPSEEK_API_KEY=sk-xxx docker compose up -d --build
```

前置依赖：Go、make/gcc（kvstore）、pnpm/node（前端，仅首次）、Python 3.8+（host 需 `pip install -r semantic/requirements.txt tokenizer/requirements.txt`；nuxt/jieba 等按既有说明）。

## 公网访问

`./start.sh` 默认只监听本机（`http://localhost:7080`）。要从公网访问，本项目走的是 **FRP 内网穿透**：云端一台小机器只做入口转发，整套服务仍然只跑在你本地——**所以前提是先有一台公网云主机**，只有环境变量是不够的。

三个前置条件缺一不可：

| # | 需要什么 | 怎么来 |
|---|---|---|
| 1 | 一台公网云主机，跑着 frps + Nginx | 按 [deploy/README.md](deploy/README.md) 在云主机执行 `deploy/edge`（2C2G 起步） |
| 2 | 一个域名，DNS A 记录指向云主机 IP | 如 `answermesh.xyz`；大陆机房还需 ICP 备案 |
| 3 | `bin/frpc` 客户端（与云端 frps **同版本**，本项目用 0.62.1） | `bin/` 已 gitignore，需自行下载 frp 的 Linux amd64 包并放为 `bin/frpc` + `chmod +x`，见 [docs/answermesh-frp-nat-traversal.md](docs/answermesh-frp-nat-traversal.md) |

三者齐备后，**二选一**把配置给 `start.sh`：

```bash
# A) 环境变量（最省事，不用建文件）——`FRP_AUTH_TOKEN` 必须与云端 .env 里的一致
PUBLIC_DOMAIN=example.com \
FRP_SERVER_ADDR=1.2.3.4 \
FRP_AUTH_TOKEN=<与云端一致的 token> \
./start.sh

# B) 配置文件（会自动渲染 deploy/app/tunnel/frpc.yaml）
cp deploy/app/.env.example deploy/app/.env   # 填 PUBLIC_DOMAIN / FRP_SERVER_ADDR / FRP_AUTH_TOKEN
./start.sh
```

可选项：`FRP_BIND_PORT`（云端 frps 的 bind 端口，默认 `39000`）。A 与 B 同时存在时以 **B（`.env` 文件）优先**。

成功后 `start.sh` 结尾会打印 `公网入口 → https://<域名>（frpc 隧道已建立）`；没配置时打印 `公网入口未启用（本机访问不受影响）`——**这是正常的，不影响本地使用**。

> 仅验证云端 frps/Nginx/TLS 是否正常：`bash deploy/scripts/deploy-edge.sh`；分层验收：`bash deploy/scripts/smoke-test.sh edge`。

## 功能说明

- **免密登录**：首次访问自动以 `device_id` 注册并分配额度，无短信验证码。
- **明文 KV 存储**：问题-回答以 `<原始问题>` → `<原始回答>` 存进 kvstore（key/value 均为原文，不做 hash，`redis-cli -p 5160 GET <问题>` 可直接读）；语义检索向量索引在版本化命名空间 `semd:e5s:v1:<原始问题>`（e5 384 维），另有安全指纹 `semfp:v1:<fp>` 精确命中。
- **公有大模型**：DeepSeek（OpenAI 兼容），模型 `deepseek-v4-flash`。
- **来源标注**：每条回答标注「公有大模型」/「缓存命中」。
- **tokens 统计**：页面按会话累计「总消耗」与「节省」tokens（缓存命中不计费、记为节省）。

## 相关仓库

- 存储：[pocket-kv](https://github.com/robotlover-1/pocket-kv)（kvstore，submodule 引入）
- 助手：本仓库 [AnswerMesh](https://github.com/robotlover-1/Answermesh)
