# AnswerMesh 边缘 2核2G FRP 部署修订 —— spec（rev2）

> 日期：2026-09-06（rev2：按 `tmp/t1/2026-09-06-answermesh-edge-2c2g-frp-revision-review.md` 合入 P0-1~4 + P1-1~8）
> 主方案（权威）：`docs/deploy/2026-09-06-answermesh-direct-public-deployment-and-tunnel-removal.md`（**新增归档，不改写原文**）
> 目标分支：`fix/edge-2c2g-frp-deployment`；基线 `main@4022ad0`
> 评审结论：**有条件通过**；完成 4 项 P0 + P1 后实施，真实 2核2G + 端到端验收后合入 main/上线。

## 1. 架构结论（不变）

固定 FRP 双节点不回退：公网 2核2G 云主机只跑 `frps + Nginx + Certbot`；本地跑完整 `AnswerMesh + frpc`（`127.0.0.1:7080` 主动连云端 `39000`）。

## 2. P0 修订决策

### P0-1 服务端强制 TLS
- `deploy/edge/tunnel/frps.yaml.envsubst` transport 加 `tls.force: true`（frpc 已 `tls.enable: true`；frps 需拒绝非 TLS 客户端）。
- 渲染冒烟断言：`grep -A5 '^transport:'` 产物含 `force: true`。
- 目标机负向用例：非 TLS 临时 frpc 登录应失败（T07）。

### P0-2 容器状态守卫不能依赖 `docker compose ps` 退出码
- `deploy-edge.sh` 新增 `assert_service_running <svc>`：`compose ps -q` 为空 → die；`docker inspect -f '{{.State.Status}}'` != `running` → die。
- 另加宿主机 TCP 就绪等待 `wait_tcp <host> <port> <secs>`（bash `/dev/tcp`，不依赖镜像内工具）：frps 起后等 `127.0.0.1:${FRP_BIND_PORT}`；nginx bootstrap 起后等 `80`；全量 HTTPS 路径等 `443`。

### P0-3 HTTPS 自检确定性（新增 `/edge-healthz` + 状态码分类）
- `deploy/edge/nginx/answermesh.conf.envsubst` 的 443 server（及 80 server）加：
  ```nginx
  location = /edge-healthz {
      access_log off;
      default_type text/plain;
      return 200 "ok\n";
  }
  ```
  （模板变量仍仅 `PUBLIC_DOMAIN`/`FRP_VHOST_HTTP_PORT`。）
- `deploy-edge.sh` 结尾拆分两类检查：
  1. TLS/Nginx 自检：`curl -fsS --max-time 15 "https://${PUBLIC_DOMAIN}/edge-healthz"` → 失败即 die（DNS/TLS/Nginx 问题，含 404/301/500 等一并暴露）。
  2. 业务链路分类 `/api/health`：
     ```bash
     code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://${PUBLIC_DOMAIN}/api/health" 2>/dev/null) || code=000
     case "$code" in
       200)   info "AnswerMesh 链路已连通" ;;
       502|503|504) info "edge 正常；本地 frpc/AnswerMesh 未就绪，frpc 上线后跑 smoke-test edge" ;;
       000)   die "HTTPS 连接失败" ;;
       *)     die "HTTPS 返回非预期状态 ${code}，检查 Nginx 路由" ;;
     esac
     ```

### P0-4 把 smoke-test.sh 纳入范围，按真实协议修复 e2e 流式验收
真实协议已由代码确认：`pkg/controllers/chat.go` 每 chunk `json.Marshal(result)`，帧间 `\n`（首帧前无分隔，EOF 前补 `\n` 后写末帧 JSON）→ **NDJSON 新行分隔**。
`smoke-test.sh e2e` 重写断言：
- `trap 'rm -f "${out}"' EXIT`（非 RETURN）；
- `curl -sS --max-time 120 -o "$out" -w '%{http_code}' …`，保存 code 与退出码；**要求 HTTP 200**，否则 die（含网关 HTML/错误 JSON 由 code/结构断言排除）；
- 按 NDJSON 统计：过滤空行后非空行数 = 帧数，**≥2**（单帧聚合/单一错误 JSON 均不通过）；
- 每帧结构校验：存在 `python3` 时逐行 `json.loads`；无 python3 时退化为行首 `{` 且行尾 `}` 的结构断言；
- 首字节 <30s、总时长 <120s（curl max-time 兜底），分开报告；
- 保留浏览器真登录+流式的人工验收提示（master §9）。

## 3. P1 采纳决策（同批提交）

- **P1-1 鉴权扩展**：frps 与 frpc `auth.additionalScopes: [HeartBeats, NewWorkConns]`（0.62.1 支持；目标机 `frps verify`/`frpc verify` 复核）。
- **P1-2 健康/就绪**：用宿主机 TCP 等待 + `assert_service_running` 替代镜像内 healthcheck（镜像工具集不确定，避免 bootstrap 期 443 未起造成 nginx unhealthy 抖动）；README 说明取舍。
- **P1-3 资源**：README 给 swap 创建步骤与"容器内存上限需先在真实流量采样（量级 nginx 256MB、frps 256–512MB），不作未验证硬标准"；`preflight_host 1536 20480` 保留。
- **P1-4 Dashboard 删除后观测补位**：README 增监控清单（容器/39000 监听/`/edge-healthz`+HTTPS 探测/5xx·带宽·CPU·内存·磁盘告警/frpc 离线告警）。
- **P1-5 39000 访问策略**：README 写明出口 IP 稳定→安全组仅限该 IP；动态→临时开放但必须 TLS 强制+高强度 token+登录失败监控+定期轮换。
- **P1-6 镜像固定为上线前验收**：README 增"上线前镜像核对"项（架构/ENTRYPOINT/配置路径/`verify -c`/两端版本/digest）。
- **P1-7 文档数量/范围修正**：见 §4/§5 精确清单（master 为新增归档、不修改）。
- **P1-8 真实 2核2G 验证为合并/上线门禁**：见 §6 验收与"合并前门禁"表述；不在本提交执行。

## 4. 改动清单（实现文件 10 个）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `deploy/edge/tunnel/frps.yaml.envsubst` | transport 加 `tls.force: true`；auth 加 `additionalScopes`；删 `webServer:` 块(注释说明按需加回且只绑回环) |
| 2 | `deploy/app/tunnel/frpc.yaml.envsubst` | auth 加 `additionalScopes`（tls.enable 已 true） |
| 3 | `deploy/edge/nginx/answermesh.conf.envsubst` | 443/80 server 加 `location = /edge-healthz` |
| 4 | `deploy/scripts/deploy-edge.sh` | 预检 `1536 20480`；加 `assert_service_running`+`wait_tcp`；起 frps/nginx 后就绪等待；frps verify(best-effort, bind-mount)；结尾拆 TLS 自检(`/edge-healthz`)与业务码分类(200/502,503,504/000/*) |
| 5 | `deploy/scripts/deploy-app.sh` | frpc verify 改为 bind-mount `frpc.yaml`（修复 no-op） |
| 6 | `deploy/edge/compose.yaml` | 删 `127.0.0.1:7500:7500`；frps/nginx 各加 `logging 20m×3` |
| 7 | `deploy/scripts/render-config.sh` | edge `guard_required`/白名单去 `FRP_DASHBOARD_PASSWORD` |
| 8 | `deploy/edge/.env.example` | 删 `FRP_DASHBOARD_PASSWORD`；加注释 `#PUBLIC_IP=` |
| 9 | `deploy/scripts/smoke-test.sh` | e2e 按 P0-4 重写 |
| 10 | `deploy/README.md` | 双节点角色/资源/请求链路/域名；单机演示=功能测试；首启顺序(edge 先、e2e 待 frpc)；MySQL 措辞；监控补位；39000 策略；镜像核对；swap/内存上限指引；上线前门禁清单 |

**不改动**：`docker/compose.yaml`、backend Go、`docker/config`、`master docs/deploy/2026-09-06-*.md`（新增归档不修改）、历史 spec/计划。

## 5. 文档与提交（数量精确）
分支 `fix/edge-2c2g-frp-deployment` 上共三类提交文件：
- 新增归档：`docs/deploy/2026-09-06-answermesh-direct-public-deployment-and-tunnel-removal.md`（1，已入 bea584f）
- 本 spec：`docs/superpowers/specs/2026-09-06-answermesh-edge-2c2g-frp-revision-design.md`（1，rev2 修订）
- 实现文件：上表 10 个
提交形态：spec 单独提交；实现为一个 `fix(deploy): adapt FRP edge deployment for 2c2g server` 提交（或按 reviewer §6 顺序拆 2 提交：a 模板/安全/自检，b smoke/README/收口）。push 分支，不动 main。

## 6. 验证
本机静态：
- `bash -n`：deploy-edge.sh/deploy-app.sh/smoke-test.sh/render-config.sh；
- yaml 解析：edge compose、frps/frpc/envsubst；
- edge 渲染冒烟：dummy `.env`（无 dashboard）→ frps.yaml 含 `tls.force: true`/`additionalScopes`、无 `webServer`、无 `${` 残留；frpc 渲染含 `additionalScopes`；
- nginx conf envsubst 含 `/edge-healthz`，本地 `nginx -t` wrapper（自签）通过；
- e2e 断言单测：构造 NDJSON≥2 帧样本通过、1 帧/HTML/非 200 均失败（抽成可测函数）；
- `grep` deploy 范围无 `FRP_DASHBOARD_PASSWORD`/`:7500:` 残留。
目标机（**合并/上线门禁，不属本提交**）：T01–T18 见 master §13 + review §7；先 L1→L4 分层，frpc 断连/恢复、云主机重启、证书续期演练；记录到验收文档。

## 7. 提交纪律
不 stage `openai-api-proxy/dev.config.yaml`、`kvstore/`；改动含 `.env.example`/模板仍遵守纯 `${VAR}`、无真实凭据。
