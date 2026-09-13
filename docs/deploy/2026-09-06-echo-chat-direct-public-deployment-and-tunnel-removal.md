# AnswerMesh 基于 FRP 的公网访问部署与现有 Tunnel 改造方案

> 日期：2026-09-06  
> 适用仓库：`robotlover-1/Answermesh`  
> 核对分支：`main`，提交 `4022ad0`  
> 约束：公网云服务器仅 2 核 2 GB；完整 AnswerMesh 继续运行在本地电脑或虚拟机。

## 1. 修正结论

本项目仍然需要 FRP，但不需要部署 `11.3-tunnel-master` 的完整多用户管理平台。正确架构是：

```text
公网用户
   │ HTTPS 443
   ▼
2核2G公网云服务器
   ├── Nginx：TLS、域名、限流、流式代理
   └── frps：FRP服务端
          │ 39000/TCP
          ▼
本地电脑/虚拟机
   ├── frpc：FRP客户端
   └── AnswerMesh：127.0.0.1:7080
          ├── ai-chat-backend / ai-chat-service / zrpc
          ├── tokenizer / semantic / keyword / sensitive
          └── proxy / kvstore / MySQL / 向量数据库
```

云服务器只做公网入口和流量转发，不运行 AnswerMesh、不加载语义模型、不运行 MySQL。frpc 从本地主动连接公网 frps，因此本地无需公网 IP，也无需路由器端口映射。

## 2. FRP 与 Tunnel 的边界

| 内容 | 是否需要 | 原因 |
|---|---:|---|
| frps | 需要 | 公网接收 frpc 连接和 HTTP vhost 请求 |
| frpc | 需要 | 将本地 `7080` 映射到云端 |
| Nginx + HTTPS | 需要 | 提供域名、TLS、限流和流式代理 |
| tunnel Web/API | 不需要 | 只有一个固定应用，无自助管理需求 |
| Kubernetes | 不需要 | 不需要动态创建多个 frps 实例 |
| tunnel MySQL/Redis | 不需要 | 不需要保存多用户、多隧道状态 |
| 自动分配端口/子域名 | 不需要 | 域名和端口固定 |
| 阿里云 DNS 自动化 | 不需要 | 手工配置一次 A 记录即可 |

`11.3-tunnel-master` 本质上是 FRP 上层的多用户管理平台。AnswerMesh 只需复用其底层 FRP 能力，不应部署整套管理平台。

## 3. 当前 GitHub 实现如何处理

截至 `4022ad0`，仓库已经实现的是轻量固定 FRP 方案，并没有把完整 tunnel 平台接入 AnswerMesh：

| 当前文件/功能 | 部署位置 | 处理意见 |
|---|---|---|
| `deploy/edge/compose.yaml` | 公网云服务器 | 保留，只启动 frps + Nginx |
| `deploy/edge/tunnel/frps.yaml.envsubst` | 公网云服务器 | 保留 |
| `deploy/edge/nginx/*` | 公网云服务器 | 保留 |
| `deploy/app/compose.yaml` | 本地机器 | 保留，只启动 frpc |
| `deploy/app/tunnel/frpc.yaml.envsubst` | 本地机器 | 保留 |
| `docker/compose.yaml` | 本地机器 | 保留，运行完整 AnswerMesh |
| `7080` 绑定 `127.0.0.1` | 本地机器 | 保留，frpc 使用 host 网络访问 |
| 配置模板化、密钥外置 | 两端 | 保留 |
| TLS bootstrap、流式代理、安全扫描 | 云端/仓库 | 保留 |

因此不需要回退当前 FRP 部署系列提交，也不能删除 `deploy/app/tunnel`、`deploy/edge/tunnel`、frpc 或 frps。上一版文档中“删除 FRP、把完整 AnswerMesh 部署到云服务器”的建议不适用于本项目，本版已纠正。

## 4. 资源和端口规划

### 4.1 公网云服务器

2 核 2 GB 足以运行 Nginx、frps 和 Certbot。建议：

- Ubuntu Server 22.04 LTS x86_64；
- 40 GB SSD，至少保留 20 GB 可用空间；
- 3～5 Mbps 可供演示，5～10 Mbps 更稳妥；
- 固定公网 IPv4；
- 可创建 2 GB swap 防止偶发 OOM，但 swap 不能替代内存；
- 不在云端构建或运行完整 AnswerMesh。

### 4.2 本地机器

- 最低 4 核 8 GB，推荐 8 核 16 GB；
- 80～100 GB 可用磁盘；
- 无 GPU 也可以；
- 保持开机并关闭自动休眠；
- 能主动连接云服务器 `39000/TCP`。

公网可用性依赖本地机器和网络。本地断电、休眠、断网或 frpc 停止时，公网访问会中断。

### 4.3 云端安全组

| 端口 | 来源 | 用途 |
|---:|---|---|
| 22/TCP | 仅管理员固定 IP | SSH |
| 80/TCP | 全网 | ACME验证和HTTPS跳转 |
| 443/TCP | 全网 | 公网AnswerMesh |
| 39000/TCP | 优先限制为本地出口公网IP | frpc连接frps |
| 39001/TCP | 不开放 | frps HTTP vhost，仅供同机Nginx |
| 7500/TCP | 不开放 | frps dashboard，仅绑定回环 |

本地 `7080` 不对公网或局域网开放；MySQL、zrpc、kvstore、semantic 等端口不得做路由器映射。

## 5. 域名与请求链路

配置 A 记录：

```text
chat.example.com → 云服务器公网IPv4
```

完整请求链路：

```text
https://chat.example.com
  → 云端Nginx:443
  → 云端127.0.0.1:39001（frps HTTP vhost）
  → FRP隧道
  → 本地frpc
  → 本地127.0.0.1:7080（AnswerMesh）
```

域名只解析到云服务器，与本地宽带 IP 无关。若使用中国大陆服务器，对外网站通常需要 ICP 备案。

## 6. 配置准备

### 6.1 生成 FRP token

```bash
openssl rand -hex 32
```

相同 token 分别写入本地 `deploy/app/.env` 和云端 `deploy/edge/.env`。不要提交 `.env`。

### 6.2 本地 `deploy/app/.env`

```dotenv
PUBLIC_DOMAIN=chat.example.com
FRP_AUTH_TOKEN=<与云端一致的随机token>
FRP_SERVER_ADDR=<云服务器公网IPv4或域名>
FRP_BIND_PORT=39000
FRPC_IMAGE=snowdreamtech/frpc:0.62.1

DEEPSEEK_API_KEY=<真实API Key>
CHAT_SERVICE_TOKEN=<重新生成的内部token>
FILTER_SERVICE_TOKEN=<重新生成的内部token>
PROXY_API_KEY=<重新生成的内部token>

KVSTORE_HOST=kvstore
KVSTORE_PORT=5160
MYSQL_DSN="root:<密码>@tcp(host.docker.internal:3306)/ai_chat?collation=utf8mb4_unicode_ci&charset=utf8mb4"

VECTOR_DB_URL=<向量库地址>
VECTOR_DB_USER=<向量库用户>
VECTOR_DB_PWD=<向量库密码>
VECTOR_DB_NAME=ai-chat
```

### 6.3 云端 `deploy/edge/.env`

```dotenv
PUBLIC_DOMAIN=chat.example.com
ADMIN_EMAIL=admin@example.com
PUBLIC_IP=<云服务器公网IPv4>

FRP_AUTH_TOKEN=<与本地一致的随机token>
FRP_BIND_PORT=39000
FRP_VHOST_HTTP_PORT=39001
FRP_DASHBOARD_PASSWORD=<随机高强度密码>
FRPS_IMAGE=snowdreamtech/frps:0.62.1
```

### 6.4 frpc 核心配置

当前仓库模板应渲染出等价配置：

```yaml
serverAddr: "<云服务器公网IP>"
serverPort: 39000

auth:
  method: token
  token: "<FRP_AUTH_TOKEN>"

proxies:
  - name: answermesh-web
    type: http
    localIP: 127.0.0.1
    localPort: 7080
    customDomains:
      - chat.example.com
```

`customDomains`、Nginx `server_name` 和 DNS 域名必须一致。

## 7. 公网云服务器部署

### 7.1 安装依赖

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git gettext-base certbot dnsutils
docker version
docker compose version
```

Docker Engine和Compose需按Docker官方方式提前安装。

### 7.2 可选：创建 swap

先确认没有现有 swap：

```bash
swapon --show
free -h
```

没有时可创建：

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 7.3 拉取代码并配置 edge

```bash
sudo mkdir -p /opt/answermesh-edge
sudo chown "$USER":"$USER" /opt/answermesh-edge
git clone https://github.com/robotlover-1/Answermesh.git /opt/answermesh-edge
cd /opt/answermesh-edge

cp deploy/edge/.env.example deploy/edge/.env
chmod 600 deploy/edge/.env
```

填写云端 `.env`，确认 DNS 已生效以及安全组已放行 80、443、39000。

### 7.4 启动 edge

```bash
sudo bash deploy/scripts/deploy-edge.sh
```

该脚本渲染 frps 配置，启动 frps，通过两阶段 Nginx 配置申请证书并启用 HTTPS。

首次运行时如果本地 frpc 尚未连接，脚本最后的应用健康检查可能失败，但不代表 frps、Nginx或证书失败。完成本地部署后重新执行脚本或单独执行端到端冒烟测试。

## 8. 本地 AnswerMesh 部署

### 8.1 拉取代码与配置

```bash
git clone https://github.com/robotlover-1/Answermesh.git
cd AnswerMesh
cp deploy/app/.env.example deploy/app/.env
chmod 600 deploy/app/.env
```

填写本地 `.env`，重点确认 FRP服务器地址、token、域名、MySQL、向量库和 DeepSeek API配置。

### 8.2 启动应用与 frpc

```bash
sudo bash deploy/scripts/deploy-app.sh
```

脚本会：

1. 渲染 AnswerMesh 和 frpc 配置；
2. 构建并启动完整 AnswerMesh；
3. 等待 `http://127.0.0.1:7080/api/readyz`；
4. 应用就绪后启动 frpc。

验证：

```bash
curl -fsS http://127.0.0.1:7080/api/readyz
docker compose -f deploy/app/compose.yaml ps
docker compose -f deploy/app/compose.yaml logs --tail=100 frpc
```

frpc 日志应显示成功登录 frps、代理注册成功。

## 9. 分层联调

```bash
# L1：本地AnswerMesh
curl -fsS http://127.0.0.1:7080/api/readyz

# L2：在云服务器检查frps vhost
curl -fsS -H 'Host: chat.example.com' \
  http://127.0.0.1:39001/api/health

# L3：任意外部机器检查完整HTTPS链路
curl -I https://chat.example.com/
curl -fsS https://chat.example.com/api/health
```

- L1成功、L2失败：检查 frpc/frps token、`customDomains`、39000安全组和 frpc日志；
- L2成功、L3失败：检查 DNS、证书、Nginx以及80/443安全组；
- 健康检查成功但聊天失败：检查模型API、业务配置、登录鉴权和流式代理。

最终还必须在浏览器完成登录、创建会话和一次真实的流式聊天。

## 10. 对现有实现的建议调整

当前实现总体无需回退，建议补充一个小型修订提交：

### 10.1 明确双节点角色

README 将角色写清楚：

```text
本地应用节点：完整AnswerMesh + frpc
公网边缘节点：2核2G云服务器，仅frps + Nginx + Certbot
```

“单机演示”只能作为功能测试，不应写成推荐生产方案。

### 10.2 降低 edge 预检阈值

`deploy-edge.sh` 当前 `preflight_host 2048 40960` 对标称2 GB实例可能过严：Linux报告的总内存可能略低于2048 MB，且40 GB是推荐磁盘容量，不应是剩余空间硬门槛。

建议改为：

```bash
preflight_host 1536 20480
```

### 10.3 拆分 edge 自检与端到端检查

云端首次部署时 frpc 可能还没上线。建议：

- `deploy-edge.sh` 只验证 frps、Nginx配置和TLS握手；
- frpc连接后再运行 `smoke-test.sh edge` 验证 AnswerMesh；
- 避免把“本地应用未上线”误报为“云端部署失败”。

### 10.4 增加日志轮转

在云端两个服务中加入：

```yaml
logging:
  driver: json-file
  options:
    max-size: "20m"
    max-file: "3"
```

### 10.5 镜像固定与 dashboard

- frps/frpc保持相同版本；验证后固定镜像digest；
- dashboard无明确需求时建议关闭；保留时只绑定 `127.0.0.1:7500`，通过SSH转发访问；
- 不向公网开放39001或7500。

## 11. 回退或修改说明

### 11.1 当前远端无需回退

当前 `main@4022ad0` 已经符合“云端edge、本地app”的基本结构。不要整体执行 `git revert 776fb78^..4022ad0`，否则会同时丢失 FRP、TLS、端口收紧、配置模板化、安全扫描和就绪检查。

推荐在新分支完成第10节的小改动：

```bash
git pull --ff-only origin main
git switch -c fix/edge-2c2g-frp-deployment
# 修改README、edge预检、健康检查和日志轮转
git add deploy docs
git commit -m "fix(deploy): adapt FRP edge deployment for 2c2g server"
git push -u origin fix/edge-2c2g-frp-deployment
```

### 11.2 如果误部署了完整 tunnel 平台

先启动固定 frps/frpc 链路并完成 L1/L2/L3 验证，再停止 tunnel Web/API、Kubernetes workload、MySQL、Redis和DNS自动化。稳定后撤销不再使用的数据库、K8s和云DNS凭据。

### 11.3 如果已经按上一版错误建议删除 FRP

恢复这些内容：

```text
deploy/app/compose.yaml
deploy/app/tunnel/frpc.yaml.envsubst
deploy/edge/tunnel/frps.yaml.envsubst
deploy/edge/compose.yaml中的frps服务
deploy-app.sh中的frpc启动逻辑
deploy-edge.sh中的frps启动逻辑
render-config.sh中的FRP渲染逻辑
```

若错误改动已经推送，应新增修复提交，不要改写共享 `main` 历史。

## 12. 安全要求

1. FRP token使用 `openssl rand -hex 32` 生成，两端一致保存。
2. 39000只供frpc连接；39001和7500必须绑定云端回环。
3. Nginx继续用覆盖式 `X-Forwarded-For $remote_addr`，避免伪造来源地址。
4. 本地7080继续绑定127.0.0.1。
5. `.env` 权限设为600且不得提交。
6. 仓库历史中曾出现的固定token、数据库或向量库凭据必须全部轮换。
7. 云端和本地FRP版本保持一致。
8. 云端定期安装安全更新并监控磁盘、内存、带宽和5xx。

## 13. 验收清单

### 云服务器

- [ ] 只运行frps、Nginx和必要系统服务；
- [ ] 未运行完整AnswerMesh、MySQL、Redis、K8s或tunnel管理平台；
- [ ] 2 GB内存下无持续OOM或swap抖动；
- [ ] 80/443公网可达；
- [ ] 39000可供frpc连接；
- [ ] 39001和7500公网不可达；
- [ ] TLS证书正确且可续期；
- [ ] 容器日志已轮转。

### 本地机器

- [ ] AnswerMesh全部服务正常；
- [ ] `/api/readyz`成功；
- [ ] 7080只监听127.0.0.1；
- [ ] frpc成功登录frps；
- [ ] zrpc内部调用正常；
- [ ] 已关闭自动休眠；
- [ ] 应用与frpc可自动重启。

### 端到端

- [ ] 域名解析到云服务器；
- [ ] HTTPS首页、登录和会话创建正常；
- [ ] 流式回答无整段缓冲；
- [ ] 本地断开frpc时公网请求按预期失败；
- [ ] frpc恢复后公网访问自动恢复；
- [ ] 云服务器重启后frps和Nginx自动恢复。

## 14. 最终方案

最终采用：

> 公网2核2G云服务器运行 `frps + Nginx + Certbot`；本地机器运行完整 `AnswerMesh + frpc`。

不部署完整 tunnel 管理平台，不在云服务器运行 AnswerMesh，也不回退当前已实现的固定 FRP 链路。代码侧只需进一步明确双节点说明，降低edge资源预检阈值，拆分云端自检与端到端自检，并增加日志轮转。
