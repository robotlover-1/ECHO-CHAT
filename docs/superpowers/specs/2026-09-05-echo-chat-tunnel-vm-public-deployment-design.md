# ECHO-CHAT 公网 tunnel 部署 — 仓库落地 spec

> 日期：2026-09-05
> 主方案（权威，含 §5 真实 VM 操作/§7-14）：`docs/deploy/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`
> 本 spec 只定义**本仓库内可静态交付的改动子集**（主方案 §4 + §5 的制品部分），真实公网 VM/DNS/证书执行由部署者在目标环境按脚本完成。

## 1. 背景与目标

把 ECHO-CHAT 的 Docker 全栈（`docker/compose.yaml`）安全暴露到公网：公网入口 Nginx(80/443) → frps → frpc → `127.0.0.1:7080`。本次在 ECHO-CHAT 仓库（main）内新增 `deploy/` 制品、做 compose 回环收紧、密钥模板化、ai-chat-backend 最小安全改动，使主方案的部署脚本可以直接执行。

已确认约束：

- 真实公网 VM/DNS/Certbot 无法在本机执行（本机无 docker daemon）；只交付制品 + 代码。
- 密钥注入采用**部署时受限 envsubst 渲染**（committed 只留模板/占位，产物 gitignore）。
- backend 只做**最小安全改动**，不动业务逻辑。
- 结构路线 A：`deploy/{app,edge,scripts}` doc 同形；frpc 做成**独立 compose 栈**（host 网络），不并入 `docker/compose.yaml`，避免多 `-f` 跨目录相对路径/挂载解析的脆弱性。

## 2. In scope（本会话交付）

### 2.1 新增 `deploy/`

```text
deploy/
├── README.md                       # 拓扑、.env 变量对照表、MySQL SQL 片段(§5.4.1)、镜像 ENTRYPOINT 验证提示、单机演示(§3.2)
├── app/                            # 应用节点（ECHO-CHAT + frpc）
│   ├── compose.yaml                # frpc 独立栈：network_mode: host；连宿主机回环 127.0.0.1:7080
│   ├── .env.example                # PUBLIC_DOMAIN / FRP_SERVER_ADDR / FRP_AUTH_TOKEN / DEEPSEEK_API_KEY / MYSQL_* / 云凭据
│   └── tunnel/frpc.yaml.envsubst   # 渲染模板，对应主方案 §5.5
├── edge/                           # 公网入口节点（自包含栈）
│   ├── compose.yaml                # frps + nginx；frps 39001/7500 publish 到 127.0.0.1
│   ├── .env.example
│   ├── tunnel/frps.yaml.envsubst   # 渲染模板，对应主方案 §5.3
│   └── nginx/echo-chat.conf.envsubst  # 流式配置模板，对应主方案 §5.6；模板变量仅 PUBLIC_DOMAIN
└── scripts/
    ├── lib.sh                      # source-env / 受限 envsubst / 日志 / 健康等待
    ├── render-config.sh            # 幂等渲染：docker/config/* + app/edge 配置
    ├── deploy-app.sh               # 渲染 → docker compose build/up → 等 127.0.0.1:7080/api/health → 起 frpc 栈
    ├── deploy-edge.sh              # 渲染 → compose up -d → 端口/日志校验
    └── smoke-test.sh               # 主方案 §9.2 分层验收 + 流式 curl -N
```

要点：

- **frpc 独立栈**：`deploy/app/compose.yaml`（project: echo-chat-frpc），镜像默认 `snowdreamtech/frpc:0.62.1`（env 可覆盖），`network_mode: host`，挂载渲染后的 `tunnel/frpc.yaml`。`depends_on` 无法跨栈，由 `deploy-app.sh` 以"等 7080 健康"替换依赖顺序。
- **edge 栈**：frps 镜像默认 `snowdreamtech/frps:0.62.1`，端口映射 `39000:39000`、`127.0.0.1:39001:39001`、`127.0.0.1:7500:7500`（Dashboard 收紧，主方案 §5.3 安全收紧点）；nginx `network_mode: host`，挂 `/etc/letsencrypt` 与 certbot webroot。
- 镜像可拉取性/ENTRYPOINT 需在目标机 `docker image inspect` 复核（README 注明）；frps/frpc 版本须一致。
- 变量默认值需与 `PUBLIC_DOMAIN` 对应，nginx conf 证书路径 `/etc/letsencrypt/live/${PUBLIC_DOMAIN}/`。

### 2.2 密钥渲染机制

- `docker/config/backend.yaml`、`docker/config/service.yaml` 从**跟踪文件**改为**渲染产物**（`git rm --cached` 后 gitignore），新提交 `docker/config/backend.yaml.envsubst`、`docker/config/service.yaml.envsubst` 模板。
- 云侧真实凭据（vectorDB url/username/pwd —— 腾讯 CLB 真实值等）在模板中**无 committed 默认**，由 `.env` 必填注入。
- 内部网络 token（backend↔service、service↔filter）+ 本机 MySQL 开发默认，模板用 `${VAR:-开发默认}` 回退并注释"生产必须覆盖"；完整轮换列为 follow-up（避免牵动 keywords-filter / mock / openai-api-proxy 等另 5 个 config）。
- frpc/frps/nginx conf 同样：模板 `*.envsubst` + 渲染产物 gitignore。
- **受限 envsubst**：只替换白名单变量（如 nginx 仅 `$PUBLIC_DOMAIN`），避免误吞 `$host`/`$remote_addr`/`$proxy_add_x_forwarded_for` 等 nginx 内建变量。
- `.env` 生成后 `chmod 600`；`.env.example` 占位值提交。
- 各相关目录 `.gitignore` 追加：`.env`、渲染产物（`docker/config/backend.yaml`、`docker/config/service.yaml`、`deploy/*/tunnel/*.yaml`、`deploy/edge/nginx/echo-chat.conf`）。

### 2.3 `docker/compose.yaml` 修改

- `ports: "7080:7080"` → `"127.0.0.1:7080:7080"`（主方案 §4.1）
- `proxy` 服务 `8084:8084` → `127.0.0.1:8084:8084`
- 顶部注释补：容器内仍 `0.0.0.0`；公网边界靠宿主机回环 publish + 安全组；frpc host 网络连 `127.0.0.1:7080`。
- 其余（服务关系/构建上下文/端口集合）不变。

### 2.4 ai-chat-backend 最小安全改动

1. `ai-chat-backend/cmd/main.go`：删 `fmt.Printf("%+v\n", cnf)` 全文配置打印（主方案 §8.5），改为 `log` 输出非敏感摘要（http ip:port、model、auth.enabled、log.level）。
2. `ai-chat-backend/pkg/config/config.go`：`Http` 增加 `TrustedProxies []string`（yaml `trusted_proxies`）；main 用 `gin.New()` + `SetTrustedProxies`。**语义**：yaml 未配置或为空 → 等价 `["127.0.0.1"]`（只信本机一跳，即公网部署时只信同机 Nginx/frps）；`SetTrustedProxies` 不接受空切片，空值必须在代码里落为 `["127.0.0.1"]`。由部署方按需扩网段。注释说明 `X-Forwarded-For/Proto` 与 gin `ClientIP` 语义。
   - 现状核对：backend 无任何业务读取 `ClientIP`/`X-Forwarded-*`/scheme（grep 已确认），故为卫生性/正确性配置，不含业务逻辑变化。
   - 流式与超时/缓冲已在 Nginx 层处理（deploy conf），backend 无需改动 ChatProcess。

## 3. Out of scope（本会话不做）

- 真实公网 VM / DNS / Certbot / 安全组执行与端到端验证
- MySQL 容器化（沿用外部/宿主机 MySQL，主方案 §5.4.1）
- 跨 keywords-filter/mock/openai-api-proxy 等的**完整** token 轮换（含 git 历史清理，主方案 §8）
- 完整 tunnel 管理平台 / K8s 控制面（主方案 §7，第二阶段）
- frps Dashboard 反代、Prometheus/告警、镜像 digest 固定（主方案 §12 P1/P2）

## 4. 验收（本机可达）

- `cd ai-chat-backend && go build ./...`（Go 1.20 已装）
- 受限 envsubst 渲染冒烟：以 dummy 变量渲染全部模板 → `python3 -c yaml.safe_load` 校验 YAML；确认 nginx 渲染产物中 `$host`/`$remote_addr` 未被吞
- `nginx -t`：用临时 wrapper + 自签证书对渲染后的 nginx conf 做语法校验（不触碰真实证书）
- `bash -n` 全部脚本
- **不可达**：`docker compose up` 端到端（无 daemon）——留待目标机，按 `deploy/README.md` 与 `smoke-test.sh` 分层验收（主方案 §9）

## 5. 文档落位

- 主方案全量复制：`docs/deploy/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`（供 clone 自包含）
- 本 spec：`docs/superpowers/specs/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`
- 后续实施计划：`docs/superpowers/plans/2026-09-05-echo-chat-tunnel-vm-public-deployment.md`（writing-plans 生成）
- 提交范围：以上 + §2 所述仓库改动；**不得**包含未提交的 `openai-api-proxy/dev.config.yaml`（本地真实 key）。
