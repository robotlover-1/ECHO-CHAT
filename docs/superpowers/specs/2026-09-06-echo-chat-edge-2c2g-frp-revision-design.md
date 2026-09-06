# ECHO-CHAT 边缘 2核2G FRP 部署修订 —— spec

> 日期：2026-09-06
> 主方案（权威）：`docs/deploy/2026-09-06-echo-chat-direct-public-deployment-and-tunnel-removal.md`
> 目标分支：`fix/edge-2c2g-frp-deployment`（不改 `main`）
> 基线：`main@4022ad0`（上轮 FRP 部署系列已推送 origin/main）

## 1. 背景与结论

上一版实现的固定 FRP 双节点链路是**最终正确架构**：公网 2核2G 云服务器只跑 `frps + Nginx + Certbot`；本地机器跑完整 `ECHO-CHAT + frpc`（`127.0.0.1:7080` 主动连云端 `39000`）。**不回退、不删除 FRP**（文档 §2/§3/§11）。

本 spec 只做 §10 的收敛修订（用户已确认）：
- README 双节点角色与资源明确；
- edge 预检阈值下调至适配 2GB 云主机；
- `deploy-edge.sh` 只做云端自检（frps/Nginx/TLS），端到端等 frpc 上线后用 smoke 验；
- 云端 frps/Nginx 日志轮转；
- **关闭 frps Dashboard**（移除 webServer 块、7500 映射、`FRP_DASHBOARD_PASSWORD` 密钥与 guard/白名单引用）。

## 2. 改动清单（6 文件）

### 2.1 `deploy/scripts/deploy-edge.sh`
1. `preflight_host 2048 40960` → `preflight_host 1536 20480`。
2. 结尾"HTTPS 验收"拆分（云端自检不再依赖本地 app 在线）：
   - 先 `docker compose … ps frps nginx` 确认 running；
   - `code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://${PUBLIC_DOMAIN}/api/health" 2>/dev/null) || code=000`；
   - `000`（或 curl 失败）→ `die`（TLS/nginx 真故障，查 80/443 安全组与日志）；
   - `502|503` → 不 die，info 提示"本地 ECHO-CHAT 未连接——frpc 上线后再跑 `smoke-test.sh edge`"；
   - 其它 code → info 提示跑 `smoke-test.sh edge/e2e`；
   - 保留末尾 renew-hook 提示行。

### 2.2 `deploy/edge/compose.yaml`
- 删除 frps 的 `"127.0.0.1:7500:7500"` 映射（dashboard 关闭）。
- `frps` 与 `nginx` 各加：
  ```yaml
  logging:
    driver: json-file
    options:
      max-size: "20m"
      max-file: "3"
  ```

### 2.3 `deploy/edge/tunnel/frps.yaml.envsubst`
- 删除 `webServer:`（addr/port/user/password）整块，替换为注释：Dashboard 默认关闭；如需监控加回 webServer 且只绑 127.0.0.1，经 SSH 转发访问。

### 2.4 `deploy/edge/.env.example`
- 删除 `FRP_DASHBOARD_PASSWORD` 行；
- 新增可选 `#PUBLIC_IP=<云主机公网IPv4>`（供 deploy-edge DNS 校验，注释：留空则退化为提示不硬校验）。

### 2.5 `deploy/scripts/render-config.sh`
- edge 分支 `guard_required` 去掉 `FRP_DASHBOARD_PASSWORD`；
- edge frps `render_restricted` 白名单去掉 `${FRP_DASHBOARD_PASSWORD}`（模板不再引用）。

### 2.6 `deploy/README.md`
- "双节点"改为"双节点角色与资源"：
  - 本地应用节点：完整 ECHO-CHAT（docker/compose，127.0.0.1:7080）+ frpc；建议 ≥4核8GB/80GB；本地断电/休眠/断网即断公网。
  - 公网边缘节点：仅 frps+Nginx+Certbot（deploy/edge），2核2G/40GB 即可；不运行 ECHO-CHAT/MySQL/语义模型。
  - 请求链路：`https://域名 → 云端Nginx:443 → 127.0.0.1:39001(frps vhost) → FRP隧道 → 本地frpc → 127.0.0.1:7080`。
  - 域名 A 记录：`chat.example.com → 云端公网IPv4`（与本地宽带无关；大陆服务器需 ICP 备案提示）。
- 首启顺序说明：edge 可先于本地部署；`deploy-edge.sh` 只验云端（frps/Nginx/TLS），端到端待 frpc 上线后 `smoke-test.sh edge/e2e`。
- "单机演示"标注：**仅功能测试，非推荐生产**（生产用双节点）。
- MySQL 小节措辞"应用 VM"→"本地应用节点/宿主机"（host.docker.internal）。
- 安全组/镜像注意：39001/7500 不开放、dashboard 默认关闭；镜像同版本、目标机验证后固定 digest。

### 2.7 不改动
- `docker/compose.yaml`（本地侧）、backend Go、`docker/config`、smoke/scan 逻辑、master `docs/deploy/2026-09-06-*.md` 全文（归档，仅作参考）。
- 上轮 spec `2026-09-05-*.design.md` 与计划保持历史原样（不回改）。

## 3. 验证（本机静态）
- `bash -n`：deploy-edge.sh / render-config.sh；
- `python3` yaml 解析：edge/compose.yaml、edge/tunnel/frps.yaml.envsubst；
- edge 渲染冒烟：dummy `.env`（无 dashboard 变量）→ `render-config.sh edge` 通过、frps.yaml 无 `webServer`/无 `${` 残留；
- `grep` 确认改动后仓库无 `FRP_DASHBOARD_PASSWORD` 与 `:7500:` 引用残留（deploy 范围）；
- 结尾自检分支手工 `bash` 单测（`code` 三态：000→die、502/503→不 die、200→不 die）。

## 4. 提交
- 分支 `fix/edge-2c2g-frp-deployment`，单提交：
  `fix(deploy): adapt FRP edge deployment for 2c2g server`
- 提交范围：上述 7 文件 + 本 spec + master 归档 `docs/deploy/2026-09-06-*.md`。
- push `origin fix/edge-2c2g-frp-deployment`；**不动 main**，后续可 PR 合入。
- 提交纪律同前：不 stage `openai-api-proxy/dev.config.yaml`、`kvstore/`。

## 5. 目标机待验（不属本提交）
真实 2核2G 云主机按 README 部署；`deploy-edge.sh` 首次在本地 app 未上线时可成功（无 000），frpc 上线后 `smoke-test.sh edge/e2e`；容器日志轮转生效；39001/7500 公网不可达。
