# ECHO-CHAT 公网 tunnel 部署 — 仓库落地 spec（rev2）

> 日期：2026-09-05（rev2：按 `2026-09-05-echo-chat-tunnel-repository-spec-review.md` 修订，合入 P0-1~4 + P1 选定项）
> 主方案（权威，含真实 VM 操作/§7-14）：`docs/deploy/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`
> 评审文档：`tmp/t1/2026-09-05-echo-chat-tunnel-repository-spec-review.md`（未入仓库；要点已合入本 spec 结论）
> 本 spec 定义**本仓库内可静态交付的改动子集**。真实公网 VM/DNS/证书执行由部署者按脚本在目标环境完成。

## 1. 背景与目标

把 ECHO-CHAT Docker 全栈安全暴露公网：公网入口 Nginx(80/443) → frps → frpc → `127.0.0.1:7080`。本次在 ECHO-CHAT（main）内新增 `deploy/` 制品 + compose 收紧 + 配置模板化 + backend 健康检查/可信代理改动，使主方案可直接执行。

评审结论为**有条件通过**：修订本 spec 的 4 个 P0 后进入实现。本 rev2 逐条落实。

已确认约束：

- 真实公网 VM/DNS/Certbot 不在本机执行（无 docker daemon）；只交付制品 + 代码。
- 密钥注入：**部署时受限 envsubst 渲染**；产物 gitignore。
- backend：最小安全改动 + 真实 readiness（P0-4 要求扩展）。
- 结构路线 A：`deploy/{app,edge,scripts}`；frpc 独立栈（host 网络）。

## 2. P0 修订决策（对照评审）

### P0-1：渲染机制 —— 弃用 `${VAR:-default}`（envsubst 不展开参数默认值）

- 模板只使用**纯 `${VAR}`**；**默认值不在模板里**。
- `.env.example`（提交）携带"本地开发默认"占位（内部 token 取既有 dev 值并注释"生产必须覆盖"；**云侧/FRP/API key 类只放 `CHANGE_ME` 占位**，无真实默认）。
- 渲染脚本：`set -a; source .env; set +a` → 受限 `envsubst '<白名单>'` → 产物。
- 必填守卫：`REQUIRED` 变量清单（FRP_AUTH_TOKEN / VECTOR_DB_URL / VECTOR_DB_USER / VECTOR_DB_PWD / 等）为空或等于 `CHANGE_ME*` 占位 → **非零退出，不起容器**。
- 残留检查：渲染产物 `rg '\$\{'` 不得命中。
- 不打印 Secret：禁止 `set -x` 输出秘密、不回显 .env。
- 原子写：`umask 077` → 模板渲染到 `out.tmp` → 校验 → `mv out.tmp out`；产物与 `.env` 权限 `0600`；`trap` 清理临时文件。

### P0-2：TLS 首次启动闭环（两阶段 Nginx）

- 证书目录 `/etc/letsencrypt/live/<domain>/` 不存在时，Nginx 不能以引用不存在的 ssl 证书启动 → 死锁。
- 新增**两个** Nginx 模板（同一挂载点 `deploy/edge/nginx/echo-chat.conf`，脚本按阶段写入再 reload，进程不重启、80 端口全程在线）：
  - `deploy/edge/nginx/echo-chat.bootstrap.conf.envsubst`：仅 80 —— `.well-known/acme-challenge/` + 兜底 503。
  - `deploy/edge/nginx/echo-chat.conf.envsubst`：80(acme+301→https) + 443 正式流式配置。
- `deploy-edge.sh` 流程：
  1. 渲染并启动 frps；
  2. 证书已存在 → 直接写正式 conf → `nginx -t` → reload → 验收；
  3. 证书不存在 → 写 bootstrap conf → 启动 nginx → 校验 DNS(== 本机公网 IP) → `certbot certonly --webroot` → 验证证书存在可读 → 原子换正式 conf → `nginx -t` 成功才 reload → 失败保持旧 conf/不中断；
  4. 幂等：重复运行不重复申请；`certbot renew --dry-run` 成功；deploy hook reload Nginx。
- edge `.env.example` 增加 `ADMIN_EMAIL`。

### P0-3：XFF / 可信代理修正

- 单公网入口、无可信 CDN/LB：edge Nginx **覆盖而非追加**：

  ```nginx
  proxy_set_header X-Forwarded-For $remote_addr;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header Host $host;
  ```

  阻止外部客户端伪造链首。与主方案 §5.6 的一处有意偏差（`$proxy_add_x_forwarded_for` → `$remote_addr`），在 conf 注释说明。
- 后端可信代理：`trusted_proxies` 未配置 → 默认 `["127.0.0.1", "::1"]`（frpc host 网络，后端看到一跳=本机回环）；配置存在但非法（空切片/无法解析）→ **启动失败（fatal），不静默回退信任全部**。
- 表述修正：`SetTrustedProxies` 只影响客户端 IP 解析（gin `ClientIP`/`RemoteIP`），不承载 `X-Forwarded-Proto` 语义；后者由 Nginx 覆盖头 + 业务自行决定，当前无业务读取。
- 单测覆盖：伪造 XFF、单代理、多代理、IPv4/IPv6 回环（gin helper）。

### P0-4：真实 readiness 替代空 /api/health

- 保留 `/api/health`（存活，兼容）；新增 **`GET /api/readyz`**：
  - 检查 MySQL(ping)、kvstore/Redis(ping)、tokenizer(HTTP)、ai-chat-service(zrpc 连通)；逐一列 `{依赖: ok|fail|degraded}`，任一 fail → 503；响应不暴露 DSN/token/内部地址。
  - ai-chat-service 探测方式以现有 client 能力为准（计划阶段确认：优先轻量 dial/健康调用，不用完整 chat）。
  - semantic 由 service 调用、backend 不直连 → 不在 readyz，语义降级语义保留给后续。
- `deploy-app.sh` 等待 **`/api/readyz`**（非空 `/health`）；`smoke-test.sh` 之后做真实登录 + 聊天。

## 3. P1 采纳决策

- **P1-1（开发流不破坏）**：本机开发主路径 = `./start.sh`（宿主进程，不受影响）+ `ai-chat-stack/`（已发布镜像，不受影响）。`docker compose` 流水线**有意改为"先渲染后 up"**（安全破坏性变更，README 明示 + 一键 `deploy/scripts/render-config.sh app`）。不在本机 `cd docker && docker compose up` 上做兼容（docker/README 一直标注该机无 daemon、未验证）。
- **P1-2（原子渲染/权限/trap）**：见 P0-1。
- **P1-3（配置一致性）**：以 `.env` 为单一来源；脚本内校验 frpc `serverPort`==frps `bindPort`、Nginx upstream==frps `vhostHTTPPort`、frpc `customDomains`==Nginx `server_name`==证书域名、`7080` 仅回环、frps/frpc 镜像版本一致。
- **P1-4（FRP 配置 verify）**：启动前用同版本镜像试 `frps/frpc verify -c`（镜像入口不确定则 `docker image inspect` 后尝试；不支持则告警 + 文档）；合法 YAML ≠ 合法 FRP 字段，不能只靠 YAML parser。
- **P1-5（流式验收）**：`smoke-test.sh` 断言：限时内收到首字节、≥2 个分离 chunk、未在 Nginx 聚合、输出首包/总耗时；不打印 Authorization。
- **P1-6（Secret 扫描）**：新增 `deploy/scripts/scan-secrets.sh`（启发式扫描待提交/产物：高熵 token、云 AccessKey、`CHANGE_ME`/示例值拒启）；gitleaks 完整 CI 门槛 → follow-up。
- **P1-7（双节点渲染分离）**：`render-config.sh {app|edge}` 子命令；app 只要求应用侧变量，edge 只要求域名/FRP/Dashboard/ADMIN_EMAIL；**edge VM 不持有 DeepSeek/MySQL/向量库秘密**。
- **P1-8（前置检查）**：`lib.sh` 预检 x86_64、docker daemon + compose v2、内存（app≥8G/edge≥2G）、磁盘（app≥40G/edge≥40G 建议值）、目标端口未占用。
- **proxy:8084**：删除 Compose `proxy` 服务的 `ports` 映射（仅 Compose 网络内 `proxy:8084` 可达，ai-chat-service 已用服务名访问）；比 `127.0.0.1:8084` 更小攻击面。

## 4. In scope 交付清单

### 4.1 新增 `deploy/`

```text
deploy/
├── README.md
│   拓扑 / .env 变量对照(common·app·edge) / MySQL SQL(主方案§5.4.1) / 镜像 ENTRYPOINT 验证 /
│   单机演示(§3.2) / 「docker compose 需先 render」说明 / 交付边界(静态验证，公网验收待执行)
├── app/                                  # 应用节点
│   ├── compose.yaml                      # frpc 独立栈：network_mode:host → 127.0.0.1:7080；挂载渲染后 tunnel/frpc.yaml
│   ├── .env.example                      # common+app 变量（占位/dev 默认；无云真实凭据）
│   └── tunnel/frpc.yaml.envsubst         # 主方案 §5.5 同款；受限渲染
├── edge/                                 # 公网入口节点
│   ├── compose.yaml                      # frps + nginx；frps 39000:39000、127.0.0.1:39001/7500；nginx host 网络
│   ├── .env.example                      # PUBLIC_DOMAIN/ADMIN_EMAIL/FRP_AUTH_TOKEN/FRP_DASHBOARD_PASSWORD/镜像
│   ├── tunnel/frps.yaml.envsubst         # 主方案 §5.3 同款
│   └── nginx/
│       ├── echo-chat.bootstrap.conf.envsubst   # P0-2 HTTP 引导
│       └── echo-chat.conf.envsubst             # P0-2/P0-3 正式流式 HTTPS（XFF 覆盖）
└── scripts/
    ├── lib.sh                            # source-env/受限 envsubst/原子写/必填守卫/残留检查/一致性校验/预检/日志
    ├── render-config.sh                  # {app|edge} 幂等渲染（P0-1/P1-7）
    ├── deploy-app.sh                     # 预检→render app→compose build/up→等 /api/readyz→frp verify→起 frpc
    ├── deploy-edge.sh                    # 预检→render edge→frps up→P0-2 两阶段 Nginx→HTTPS 验收
    ├── smoke-test.sh                     # 主方案 §9.2 分层 + 真实登录/聊天 + P1-5 流式断言
    └── scan-secrets.sh                   # P1-6
```

渲染产物（gitignore）：`.env`（app/edge）、`docker/config/{backend,service}.yaml`、`deploy/app/tunnel/frpc.yaml`、`deploy/edge/tunnel/frps.yaml`、`deploy/edge/nginx/echo-chat.conf`。

### 4.2 `docker/config/` 模板化

- 提交 `docker/config/backend.yaml.envsubst`、`docker/config/service.yaml.envsubst`（模板，纯 `${VAR}`）。
- `docker/config/backend.yaml`、`service.yaml` → `git rm --cached` + gitignore（**有意破坏性变更**，README 明示）。
- compose.yaml 挂载路径不变（`./config/backend.yaml`），先 render 后 up。
- 模板内云侧（vectorDB）真实腾讯 CLB 凭据从 git 清除；内部 token/本机 MySQL 默认经 `.env.example` 注入（dev 值 + "生产覆盖"注释）。

### 4.3 `docker/compose.yaml` 修改

- `7080:7080` → `127.0.0.1:7080:7080`。
- `proxy` 服务**删除 `ports`（8084）**（P1 采纳）。
- 顶部注释：容器内仍 `0.0.0.0`；公网边界=宿主机回环 publish+安全组；frpc host 网络连回环；`docker compose` 需先 `render-config.sh app`。

### 4.4 ai-chat-backend 改动

1. `cmd/main.go`：删 `fmt.Printf("%+v", cnf)`（主方案 §8.5）→ log 非敏感摘要（http、model、auth.enabled、log.level）。
2. `pkg/config/config.go`：`Http.TrustedProxies []string`（yaml `trusted_proxies`）；空→默认 `127.0.0.1`/`::1`；非法→fatal。
3. gin 装配 helper（供 main 与单测复用）：`SetTrustedProxies` + `ClientIP` 语义测试（伪造 XFF/多代理/IPv6）。
4. 新增 `GET /api/readyz`（P0-4）；保留 `/api/health` 为存活。
5. 健康检查细节（mysql/redis/tokenizer/ai-chat-service）在计划阶段敲定，实现到能反映"服务不可聊"的粒度。

## 5. Out of scope（本会话不做）

- 真实公网 VM/DNS/Certbot/安全组执行与端到端验收
- MySQL 容器化（沿用外部/宿主机 MySQL，主方案 §5.4.1）
- 跨 keywords-filter/mock/openai-api-proxy 的**完整** token 轮换 + git 历史清理（主方案 §8）→ 遗留凭据轮换清单 follow-up
- 完整 tunnel 平台 / K8s 控制面（主方案 §7，第二阶段）
- Prometheus/告警、镜像 digest 固定、gitleaks 全量 CI 门槛（主方案 §12 P1/P2）

## 6. 验收（本机可达）

- `cd ai-chat-backend && go build ./... && go test ./pkg/... ./cmd/...`（trusted-proxy 单测通过）
- 受限 envsubst 渲染冒烟：dummy `.env` → 渲染全部模板 → 产物 `rg '\$\{'` 无残留 → pyyaml 校验 → 必填守卫拒 `CHANGE_ME` → 确认 nginx 产物 `$host/$remote_addr` 未被吞（受限白名单）
- `nginx -t`：临时 wrapper + 自签证书对正式 conf 语法校验；bootstrap conf 独立 `-t`
- `bash -n` 全部脚本；`shellcheck`（若可用）
- 一致性检查（P1-3）以 dummy 值脚本自测
- **不可达**：`docker compose up` 端到端（无 daemon）→ 目标机按 README/smoke-test.sh 分层验收

## 7. 文档落位与提交

- 主方案：`docs/deploy/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`（已提交）
- 本 spec（rev2）：`docs/superpowers/specs/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md`
- 后续实施计划：`docs/superpowers/plans/2026-09-05-echo-chat-tunnel-vm-public-deployment.md`（writing-plans）
- 提交范围：上述改动；**不得包含**未提交的 `openai-api-proxy/dev.config.yaml`（本地真实 key）
