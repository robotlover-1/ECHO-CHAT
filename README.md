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
| 3 | `bin/frpc` 客户端（与云端 frps **同版本**，本项目用 0.62.1） | 一条命令：`bash deploy/scripts/fetch_frpc.sh`（`bin/` 已 gitignore，脚本会下载 + 官方 sha256 校验 + 安装） |

### 那三个值分别怎么来

| 变量 | 是什么 | 怎么获取 |
|---|---|---|
| `PUBLIC_DOMAIN` | 你要对外用的域名 | 你自己的域名。去域名服务商控制台加一条 A 记录指向云主机 IP：`example.com.  A  1.2.3.4`（大陆机房还需 ICP 备案） |
| `FRP_SERVER_ADDR` | 云主机的公网 IPv4 | **在云主机上**执行：`curl -s https://ipinfo.io/ip` 或 `curl -s https://ifconfig.me` |
| `FRP_AUTH_TOKEN` | 两端共享的密钥，**不是"查"来的，是自己生成的** | `openssl rand -hex 32`（64 位十六进制）。**只生成一次**，然后把同一个串分别填到云端 `deploy/edge/.env` 与本机 `deploy/app/.env`（或本地启动时的环境变量） |

> ⚠️ **最容易踩的坑：不要两端各跑一次 `openssl rand -hex 32`。** 那会得到两个**不同**的
> token——frps 端渲染进 `deploy/edge/tunnel/frps.yaml` 的 `token:`，frpc 端拿自己的那个去登录，
> 结果是 `token in login doesn't match token from configuration`，隧道永远建不起来，
> 而公网访问表现为 frps 自带的 404 页（`The server is powered by frp.`）。
> 正确姿势：**一处生成，复制到另一端**。

配套的命令，抄了就能用：

```bash
# ① 生成 token —— 只跑一次，把输出复制到下面两处
openssl rand -hex 32

# ② 云端：填进 deploy/edge/.env 并让 frps 重新读到
#    （token 是渲染进 frps.yaml 的，改完 .env 必须重渲染 + 重启 frps 才生效）
#    在【云主机】上、部署目录（如 /opt/answermesh-edge）里执行：
#      vi deploy/edge/.env                    # FRP_AUTH_TOKEN=<①的同一个串>
#      bash deploy/scripts/render-config.sh edge
#      docker compose -p answermesh-edge up -d frps

# ③ 拿到云主机公网 IP —— 在【云主机】上执行
curl -s https://ipinfo.io/ip; echo        # 或 curl -s https://ifconfig.me; echo

# ④ 配好域名 A 记录后，本地验证是否指向云主机（回显的应是云 IP）
dig +short example.com

# ⑤ 装 frpc 客户端（0.62.1，与云端 frps 同版本；自动 sha256 校验）
bash deploy/scripts/fetch_frpc.sh
```

忘了云端用的是哪个 token？在**云主机**上直接查（不必重新生成）：

```bash
grep FRP_AUTH_TOKEN /opt/*edge*/deploy/edge/.env
```
```

`FRP_BIND_PORT` 通常不用管（默认 `39000`，云端 `deploy/edge/.env` 里可改）。

### 启动

三样齐备后，**二选一**把配置给 `start.sh`：

```bash
# A) 环境变量：写成一行，变量之间用空格隔开（最不容易出错）
PUBLIC_DOMAIN=example.com FRP_SERVER_ADDR=1.2.3.4 FRP_AUTH_TOKEN=<与云端一致的 token> ./start.sh

# B) 配置文件（会自动渲染 deploy/app/tunnel/frpc.yaml）——正式部署建议用这个
cp deploy/app/.env.example deploy/app/.env   # 填 PUBLIC_DOMAIN / FRP_SERVER_ADDR / FRP_AUTH_TOKEN
./start.sh
```

> **写多行时注意**：行尾续行符 `\` **前面必须留一个空格**。
> `FRP_AUTH_TOKEN=abc\` + 换行 + `./start.sh` 会被 bash 拼成 `FRP_AUTH_TOKEN=abc./start.sh`——
> 整条命令退化成"纯变量赋值"，`start.sh` 根本不会执行，而且**一行输出都没有**（很容易误判成"跑通了但没效果"）。
> 正确写法：
> ```bash
> PUBLIC_DOMAIN=example.com \
> FRP_SERVER_ADDR=1.2.3.4 \
> FRP_AUTH_TOKEN=abc \
> ./start.sh
> ```
> 判断有没有真跑起来：有输出 `== 环境/补编译 ==` 和 `[frpc] 渲染配置 ...` 才算。

A 与 B 同时存在时以 **B（`.env` 文件）优先**。也可以让 `start.sh` 顺带把 frpc 也下了：

```bash
ECHO_FETCH_FRPC=1 PUBLIC_DOMAIN=example.com FRP_SERVER_ADDR=1.2.3.4 FRP_AUTH_TOKEN=<token> ./start.sh
```

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
