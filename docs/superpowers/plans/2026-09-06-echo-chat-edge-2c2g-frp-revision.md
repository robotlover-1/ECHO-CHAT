# ECHO-CHAT 边缘 2核2G FRP 部署修订 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在分支 `fix/edge-2c2g-frp-deployment` 上完成 FRP 边缘部署修订：服务端强制 TLS、鉴权扩展、Dashboard 关闭、日志轮转、云端自检确定性（`/edge-healthz` + 状态分类）、按真实 NDJSON 协议重写 smoke e2e，并更新 README。

**Architecture:** 公网 2核2G 云主机仅 frps+Nginx+Certbot；本地完整 ECHO-CHAT+frpc。本次是配置/脚本/文档修订，无业务代码改动。**不回退现有固定 FRP 链路。**

**Tech Stack:** docker compose v2、FRP 0.62.1（frps/frpc）、nginx 1.27、GNU envsubst、bash。

**Spec:** `docs/superpowers/specs/2026-09-06-echo-chat-edge-2c2g-frp-revision-design.md`（rev2，已合入评审 P0-1~4 + P1-1~8）。主方案：`docs/deploy/2026-09-06-echo-chat-direct-public-deployment-and-tunnel-removal.md`（新增归档，不修改）。

## Global Constraints

- **分支**：工作树已在 `fix/edge-2c2g-frp-deployment`（基于 `main@4022ad0`）。不要切分支。最终 push 该分支，**不动 main**。
- **提交纪律**：`openai-api-proxy/dev.config.yaml` 是未提交的真实本地 key 修改（工作树 ` M`），**任何 commit 不得包含**；`kvstore/` 未跟踪，忽略。`git add` 只用本任务列出的精确路径。
- **模板纪律**：模板只用纯 `${VAR}`（无 `${VAR:-default}`）；envsubst 白名单只含模板实际变量。**不要**让 `guard_no_residue` 误判 nginx 无花括号运行期变量（该正则已只查 `${VAR}` 花括号残留）。
- **不得运行** `docker compose up`/`docker` 起容器（本机无 daemon）。验证 = `bash -n`、yaml 解析、受限 envsubst 渲染冒烟、本机 `nginx -t`（1.18，临时 wrapper + 自签证书）、`grep` 残留扫描、e2e 断言函数用样本文件单测。
- **真实帧协议**（e2e 依据）：`ai-chat-backend/pkg/controllers/chat.go` 每 chunk `json.Marshal(result)`，帧间 `\n`，末帧(结果元数据)前补 `\n` → 响应体 = **NDJSON（新行分隔的 JSON 对象序列）**。勿改业务。
- 基线文件已核对（deploy-edge.sh / smoke-test.sh 全文、frps/frpc envsubst、edge compose、edge .env.example、render-config.sh、nginx full conf 前 60 行均含于本计划上下文）；实施前如某文件行号/锚点偏移，以**内容匹配**定位，不依赖行号。
- 不改动：`docker/compose.yaml`、backend Go、`docker/config`、master `docs/deploy/2026-09-06-*.md`、历史 spec/计划。

---

### Task 1: FRP 传输/鉴权安全 + Dashboard 关闭 + 边缘日志轮转

**Files:**
- Modify: `deploy/edge/tunnel/frps.yaml.envsubst`
- Modify: `deploy/app/tunnel/frpc.yaml.envsubst`
- Modify: `deploy/edge/compose.yaml`
- Modify: `deploy/edge/.env.example`
- Modify: `deploy/scripts/render-config.sh`

**Interfaces:**
- Consumes: 既有 `render-config.sh` edge 分支 guard/白名单；现有 compose 服务结构。
- Produces: frps 产物 `transport.tls.force: true` + `auth.additionalScopes`、无 `webServer`；frpc 产物含 `auth.additionalScopes`；edge `.env` 无需 `FRP_DASHBOARD_PASSWORD`；frps/nginx 容器日志 `20m×3`。后续 Task 2 依赖本任务渲染产物做 verify。

- [ ] **Step 1: frps 模板 —— 强制 TLS + 扩展鉴权 + 移除 webServer**

把 `deploy/edge/tunnel/frps.yaml.envsubst` 全量替换为：

```yaml
bindAddr: 0.0.0.0
bindPort: ${FRP_BIND_PORT}

# HTTP 虚拟主机只供本机 Nginx 访问（compose 将端口 publish 到 127.0.0.1）。
vhostHTTPPort: ${FRP_VHOST_HTTP_PORT}

auth:
  method: token
  # 鉴权扩展到心跳与新工作连接（与 frpc 端一致；0.62.1 支持，目标机 frps verify 复核）。
  additionalScopes:
    - HeartBeats
    - NewWorkConns
  token: "${FRP_AUTH_TOKEN}"

transport:
  tcpMux: true
  maxPoolCount: 5
  # 公网 39000 是公开控制入口：强制 TLS，拒绝非 TLS 客户端(与 frpc transport.tls.enable 配对)。
  tls:
    force: true

# Dashboard 默认关闭（最小暴露，评审 P0-3/P1-4 补位见 deploy/README 监控清单）。
# 如需监控：加回 webServer，仅绑定 127.0.0.1，经 SSH 转发访问：
#   webServer:
#     addr: 127.0.0.1
#     port: 7500
#     user: "frpadmin"
#     password: "<强密码>"

log:
  to: console
  level: info
  maxDays: 7
```

- [ ] **Step 2: frpc 模板 —— 鉴权扩展**

`deploy/app/tunnel/frpc.yaml.envsubst` 的 `auth:` 块（现为 `method: token` + `token:` 两行）替换为：

```yaml
auth:
  method: token
  additionalScopes:
    - HeartBeats
    - NewWorkConns
  token: "${FRP_AUTH_TOKEN}"
```

其余（serverAddr/serverPort/transport.tls.enable/loginFailExit/log/proxies）不动。

- [ ] **Step 3: edge compose —— 删 7500 + 日志轮转**

`deploy/edge/compose.yaml`：
1. 删除 `frps` 服务 ports 中的 `"127.0.0.1:7500:7500"` 行。
2. `frps` 与 `nginx` 服务体各追加（缩进 2 空格的同层键）：

```yaml
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "3"
```

3. 文件头注释在 Dashboard 关闭后补一句：`# 7500 dashboard 已关闭；39001/7500 均不对外。`

- [ ] **Step 4: edge .env.example**

`deploy/edge/.env.example`：删除 `FRP_DASHBOARD_PASSWORD=CHANGE_ME_dashboard_pwd` 行；在 `PUBLIC_IP` 用途处（`FRP_AUTH_TOKEN` 之后）插入两行：

```dotenv
# 可选：本机公网IPv4，用于 deploy-edge 的 DNS 校验；留空则仅提示不硬校验。
#PUBLIC_IP=203.0.113.10
```

- [ ] **Step 5: render-config.sh —— guard/白名单去 dashboard**

`deploy/scripts/render-config.sh` edge 分支：
1. `guard_required` 列表删掉 `FRP_DASHBOARD_PASSWORD`（保留 `PUBLIC_DOMAIN ADMIN_EMAIL FRP_AUTH_TOKEN`）。
2. frps `render_restricted` 白名单去掉 `${FRP_DASHBOARD_PASSWORD}`，保留 `'${FRP_BIND_PORT} ${FRP_VHOST_HTTP_PORT} ${FRP_AUTH_TOKEN}'`。

- [ ] **Step 6: 验证**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
bash -n deploy/scripts/render-config.sh && echo "syntax OK"
python3 -c "import yaml;[yaml.safe_load(open(f)) for f in ['deploy/edge/compose.yaml','deploy/edge/tunnel/frps.yaml.envsubst','deploy/app/tunnel/frpc.yaml.envsubst']];print('yaml OK')"
# 渲染冒烟（dummy edge .env，无 dashboard 变量必须能通过 guard）
cp deploy/edge/.env.example deploy/edge/.env
sed -i -E 's/CHANGE_ME_random_hex_64/1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff/; s/CHANGE_ME_admin@example.com/admin@example.com/' deploy/edge/.env
bash deploy/scripts/render-config.sh edge
echo "--- frps.yaml transport/auth 断言 ---"
grep -A3 '^transport:' deploy/edge/tunnel/frps.yaml     # 应含 tls: force: true
grep -A4 '^auth:' deploy/edge/tunnel/frps.yaml          # 应含 additionalScopes/HeartBeats/NewWorkConns
grep -c 'webServer' deploy/edge/tunnel/frps.yaml || true # 期望 0
grep -c '\${' deploy/edge/tunnel/frps.yaml || true       # 期望 0
# 清理渲染产物（不留 .env/产物）
rm -f deploy/edge/.env deploy/edge/tunnel/frps.yaml
```

- [ ] **Step 7: 残留扫描**

```bash
grep -rn 'FRP_DASHBOARD_PASSWORD' deploy/ || true   # 期望无输出
grep -rn ':7500:' deploy/edge/compose.yaml || true  # 期望无输出
```

- [ ] **Step 8: Commit**

```bash
git add deploy/edge/tunnel/frps.yaml.envsubst deploy/app/tunnel/frpc.yaml.envsubst deploy/edge/compose.yaml deploy/edge/.env.example deploy/scripts/render-config.sh
git commit -m "fix(deploy): frps 强制TLS+扩展鉴权、关闭dashboard、边缘日志轮转
- P0-1 frps transport.tls.force:true(拒非TLS客户端)
- P1-1 frps+frpc auth.additionalScopes HeartBeats/NewWorkConns
- 删 webServer/7500/FRP_DASHBOARD_PASSWORD(最小暴露)
- frps+nginx logging json-file 20m x3"
```

---

### Task 2: Nginx `/edge-healthz` + deploy-edge.sh 云端自检拆分与就绪等待

**Files:**
- Modify: `deploy/edge/nginx/echo-chat.conf.envsubst`
- Modify: `deploy/scripts/deploy-edge.sh`
- Modify: `deploy/scripts/deploy-app.sh`

**Interfaces:**
- Consumes: Task 1 渲染产物、`lib.sh` 函数、既有 deploy-edge 两阶段流程。
- Produces: nginx 443/80 server 的 `location = /edge-healthz`（返回 `200 "ok\n"`，不代理）；deploy-edge 新增 `assert_service_running <svc>`、`wait_tcp <host> <port> <secs>`；预检 `1536 20480`；结尾拆"TLS 自检 → 业务码分类"；frps verify(bind-mount)。deploy-app 的 frpc verify 也改为 bind-mount。Task 4（README）引用这些行为。

- [ ] **Step 1: nginx full conf 加 /edge-healthz（443 与 80 server）**

`deploy/edge/nginx/echo-chat.conf.envsubst`：

80 server 的 acme location 之后（`location / { return 301 ...; }` 之前）插入：

```nginx
    location = /edge-healthz {
        access_log off;
        default_type text/plain;
        return 200 "ok\n";
    }
```

443 server 的插入（锚点取唯一文本，`server_name` 在 80/443 出现两次故用其后的 `ssl_certificate` 定位）——Edit old_string：

```nginx
    server_name ${PUBLIC_DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${PUBLIC_DOMAIN}/fullchain.pem;
```

→ new_string：

```nginx
    server_name ${PUBLIC_DOMAIN};

    location = /edge-healthz {
        access_log off;
        default_type text/plain;
        return 200 "ok\n";
    }

    ssl_certificate     /etc/letsencrypt/live/${PUBLIC_DOMAIN}/fullchain.pem;
```

模板变量仍只有 `PUBLIC_DOMAIN`/`FRP_VHOST_HTTP_PORT`（本块无新变量）。

- [ ] **Step 2: deploy-edge.sh —— 预检阈值**

`deploy/scripts/deploy-edge.sh` 第 21 行 `preflight_host 2048 40960` → `preflight_host 1536 20480`。

- [ ] **Step 3: deploy-edge.sh —— frps verify（bind-mount）**

在 `bash "${REPO_ROOT}/deploy/scripts/render-config.sh" edge` 之后插入：

```bash
info "FRP 服务端配置自检(best-effort，P1-6)..."
if docker run --rm \
     -v "${EDGE_DIR}/tunnel/frps.yaml:/app/config.yaml:ro" \
     --entrypoint frps "${FRPS_IMAGE:-snowdreamtech/frps:0.62.1}" verify -c /app/config.yaml >/dev/null 2>&1; then
  info "frps verify OK"
else
  info "frps verify 不可用（镜像 ENTRYPOINT 不同/无此子命令）——跳过；上线前 docker image inspect 核对"
fi
```

- [ ] **Step 4: deploy-edge.sh —— 就绪等待与状态守卫 helper**

在 `reload_nginx()` 函数之后追加两个函数：

```bash
# 断言 compose 服务 running：ps -q 非空 且 docker inspect 状态为 running。
assert_service_running() {
  local service="$1" cid status
  cid="$(docker compose -f "${EDGE_DIR}/compose.yaml" ps -q "${service}" 2>/dev/null || true)"
  [[ -n "${cid}" ]] || die "${service} 容器不存在"
  status="$(docker inspect -f '{{.State.Status}}' "${cid}" 2>/dev/null || true)"
  [[ "${status}" == "running" ]] || die "${service} 状态=${status:-未知}(期望 running)"
}

# 宿主机 TCP 就绪等待（bash /dev/tcp，不依赖镜像工具）。超时默认 30s。
wait_tcp() {
  local host="$1" port="$2" secs="${3:-30}" i=0
  while (( i < secs * 2 )); do
    if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
      return 0
    fi
    sleep 0.5
    i=$(( i + 1 ))
  done
  die "等待 TCP ${host}:${port} 超时 ${secs}s"
}
```

在 `start_frps`（`info "启动 frps..."` 之后的那次调用处）后追加：

```bash
assert_service_running frps
wait_tcp 127.0.0.1 "${FRP_BIND_PORT:-39000}"
```

- [ ] **Step 5: deploy-edge.sh —— 结尾拆分为确定性自检**

把当前结尾三行（`check_consistency edge` / `info "HTTPS 验收..."` / `curl -fsS …api/health … || die …`）整块替换为：

```bash
check_consistency edge
assert_service_running frps
assert_service_running nginx
wait_tcp 127.0.0.1 443

info "云端自检: TLS/Nginx(/edge-healthz)..."
curl -fsS --max-time 15 "https://${PUBLIC_DOMAIN}/edge-healthz" >/dev/null \
  || die "公网 DNS/TLS/Nginx 自检失败(/edge-healthz)——查 80/443 安全组与 nginx 日志"

info "云端自检: ECHO-CHAT 业务链路(/api/health)..."
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://${PUBLIC_DOMAIN}/api/health" 2>/dev/null)" || code=000
case "${code}" in
  200)            info "ECHO-CHAT 链路已连通，建议再跑 smoke-test.sh edge/e2e" ;;
  502|503|504)    info "edge 正常(HTTP ${code})；本地 frpc/ECHO-CHAT 未就绪——frpc 上线后再跑 smoke-test.sh edge" ;;
  000)            die "HTTPS 连接失败(000)";;
  *)              die "HTTPS 返回非预期状态 ${code}，检查 Nginx 路由/后端" ;;
esac
```

保留最后的 renew-hook 提示行不变。

- [ ] **Step 6: deploy-app.sh —— frpc verify bind-mount**

`deploy/scripts/deploy-app.sh` 现有 frpc verify 的 `docker run` 增加挂载渲染产物：

```bash
if docker run --rm \
     -v "${REPO_ROOT}/deploy/app/tunnel/frpc.yaml:/app/config.yaml:ro" \
     --entrypoint frpc "${FRPC_IMAGE:-snowdreamtech/frpc:0.62.1}" verify -c /app/config.yaml >/dev/null 2>&1; then
  info "frpc verify OK"
else
  info "frpc verify 不可用（镜像 ENTRYPOINT 不同/无此子命令）——跳过；请在目标机 docker image inspect 后核对 FRPC_IMAGE"
fi
```

（提示行保留原样。）

- [ ] **Step 7: 验证（bash + nginx -t wrapper）**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
bash -n deploy/scripts/deploy-edge.sh && bash -n deploy/scripts/deploy-app.sh && echo "syntax OK"
# 全量 conf 渲染 + /edge-healthz 存在 + 无残留 + $host 保留
export PUBLIC_DOMAIN=chat.example.com FRP_VHOST_HTTP_PORT=39001
mkdir -p /tmp/echot2/conf.d && cd /tmp/echot2
openssl req -x509 -nodes -newkey rsa:2048 -days 1 -keyout privkey.pem -out fullchain.pem -subj "/CN=chat.example.com" >/dev/null 2>&1
envsubst '${PUBLIC_DOMAIN} ${FRP_VHOST_HTTP_PORT}' \
  < /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT/deploy/edge/nginx/echo-chat.conf.envsubst > conf.d/echo-chat.conf
grep -c 'edge-healthz' conf.d/echo-chat.conf            # 期望 2（80 与 443 各一）
grep -c '\$\{' conf.d/echo-chat.conf || true             # 期望 0
grep -c '\$host' conf.d/echo-chat.conf                   # 期望 >=1
sed -i 's|/etc/letsencrypt/live/chat.example.com|/tmp/echot2|g' conf.d/echo-chat.conf
printf 'pid /tmp/echot2/nginx.pid; error_log /tmp/echot2/error.log; events { worker_connections 64; } http { include /etc/nginx/mime.types; include /tmp/echot2/conf.d/*.conf; }\n' > main.conf
nginx -t -c /tmp/echot2/main.conf -p /tmp/echot2/
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT && rm -rf /tmp/echot2
```

Expected: `syntax OK`；`edge-healthz` 计数 2；无残留；`nginx: configuration ... test is successful`。

- [ ] **Step 8: Commit**

```bash
git add deploy/edge/nginx/echo-chat.conf.envsubst deploy/scripts/deploy-edge.sh deploy/scripts/deploy-app.sh
git commit -m "fix(deploy): /edge-healthz 静态探针 + edge 云端自检拆分与确定性
- P0-2 assert_service_running(ps -q+inspect) + wait_tcp(/dev/tcp) 就绪等待
- P0-3 Nginx 443/80 /edge-healthz；/api/health 业务码分类 200/502,503,504/000/*
- P1-2/P1-6 frps/frpc verify 改 bind-mount 真校验；预检降 1536 20480(2核2G)"
```

---

### Task 3: smoke-test.sh e2e 按真实 NDJSON 重写（帧计数进 lib.sh）

**Files:**
- Modify: `deploy/scripts/lib.sh`（追加 `stream_frame_count`）
- Modify: `deploy/scripts/smoke-test.sh`

**Interfaces:**
- Consumes: 真实流式帧协议（chat.go：每 chunk `json.Marshal`，帧间 `\n` → NDJSON）；smoke 已 `source lib.sh`。
- Produces: `lib.sh` 新函数 `stream_frame_count <file>`（打印有效 JSON 帧数；python3 缺失退化结构计数；返回非零=非法 JSON）；e2e 断言：HTTP 200、curl 退出码 0、帧数 ≥2、首字节/总时长。Task 5 复用该函数做样本单测。

- [ ] **Step 1: lib.sh 追加帧计数函数**

`deploy/scripts/lib.sh` 文件末尾（`check_consistency` 之后）追加：

```bash
# NDJSON 帧计数：非空 JSON 行数。有 python3 则逐行 json.loads(严格)；否则退化为
# "以 { 开头且 } 结尾的非空行"结构计数。返回 3 = 内容含非法 JSON(错误页/HTML/错误JSON)。
stream_frame_count() {
  local f="$1" n
  if command -v python3 >/dev/null 2>&1; then
    n="$(python3 - "$f" <<'PY' 2>/dev/null || true
import json, sys
c = 0
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            json.loads(s)
            c += 1
    print(c)
except Exception:
    sys.exit(3)
PY
)"
    [[ -n "$n" ]] || return 3
  else
    n="$(grep -cE '^[[:space:]]*\{.*\}[[:space:]]*$' "$f" || true)"
  fi
  printf '%s' "$n"
}
```

> 说明：函数体里 `return 3`/`exit` 语义见上；python3 段若输出非数字(空)则视为失败。`grep -c` 分支失败由 `|| true` 吸收（`grep` 零匹配返回 1）。

- [ ] **Step 2: 替换 smoke-test.sh 的 e2e 分支**

`deploy/scripts/smoke-test.sh` 的整个 `e2e)` 分支（从 `e2e)` 到 `;;`）替换为（帧计数调用 lib 函数，`trap` 用 EXIT，curl 加 `--max-time 120`）：

```bash
  e2e)
    info "L4 流式聊天..."
    [[ -n "${AUTH}" ]] || die "e2e 需 AUTH=<登录token>"
    out="$(mktemp)"
    trap 'rm -f "${out}"' EXIT
    start=$(date +%s%N)
    # 后台 curl：--max-time 120 兜底；-o 存体；-w 存 HTTP code；-N 关缓冲。
    curl -sS -N --max-time 120 -o "${out}" -w '%{http_code}' \
      "https://${DOMAIN}/api/chat-process" \
      -H "Authorization: ${AUTH}" -H 'Content-Type: application/json' \
      --data '{"prompt":"数到三","options":{}}' > "${out}.code" &
    cpid=$!
    first_byte=0
    while kill -0 "$cpid" 2>/dev/null; do
      if [[ -s "${out}" ]]; then
        first_byte=$(( ($(date +%s%N) - start) / 1000000 )); break
      fi
      sleep 0.05
    done
    crc=0; wait "$cpid" || crc=$?
    code="$(cat "${out}.code" 2>/dev/null || true)"
    end=$(( ($(date +%s%N) - start) / 1000000 ))
    rm -f "${out}.code"

    [[ "${crc}" -eq 0 ]] || die "curl 异常退出 ${crc}(28=max-time 超时/连接问题)"
    [[ "${code}" == "200" ]] || die "e2e HTTP=${code}，要求 200(网关/鉴权/上游错误)"
    [[ -s "${out}" ]] || die "e2e HTTP 200 但响应体为空"
    frames="$(stream_frame_count "${out}")" || die "e2e 响应含非 JSON 行(错误页/HTML/错误 JSON)"
    info "HTTP 200 / 首字节 ${first_byte}ms / 总时长 ${end}ms / 字节 $(wc -c < "${out}") / 帧数 ${frames}"
    [[ "${frames}" -ge 2 ]] || die "有效帧数(${frames})<2，疑似代理缓冲聚合或单帧错误"
    [[ "${first_byte}" -lt 30000 ]] || die "首字节 ≥30s，流式不通"
    [[ "${end}" -lt 120000 ]] || die "总时长 ≥120s"
    ;;
```

（其余 `app)`/`edge)`/`*)` 分支不动。）

- [ ] **Step 3: 断言函数单测（样本文件 + source lib）**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
printf '%s\n%s\n%s\n' '{"id":"a","delta":"你"}' '{"id":"a","delta":"好"}' '{"text":"你好","source":"llm"}' > /tmp/ok.ndjson
printf '%s\n' '{"status":"Fail","message":"x"}' > /tmp/one.ndjson
printf '<html>502 Bad Gateway</html>\n' > /tmp/html.body
bash -c '
source deploy/scripts/lib.sh
ok=$(stream_frame_count /tmp/ok.ndjson);  [[ "$ok" == "3" ]] || { echo "FAIL ok=$ok"; exit 1; }
one=$(stream_frame_count /tmp/one.ndjson); [[ "$one" == "1" ]] || { echo "FAIL one=$one"; exit 1; }
if stream_frame_count /tmp/html.body >/dev/null 2>&1; then echo "FAIL html should fail"; exit 1; fi
echo "frame-count unit OK: ok=$ok one=$one html-rejected"
'
rm -f /tmp/ok.ndjson /tmp/one.ndjson /tmp/html.body
```

Expected: `frame-count unit OK: ok=3 one=1 html-rejected`。

- [ ] **Step 4: Commit**

```bash
git add deploy/scripts/lib.sh deploy/scripts/smoke-test.sh
git commit -m "fix(deploy): smoke e2e 按真实 NDJSON 帧重写(P0-4)
- 协议=chat.go 每 chunk json.Marshal +\n；要求 HTTP 200 + 有效帧数>=2
- lib.stream_frame_count: python3 严格 json 校验，缺省退化结构计数
- EXIT trap、curl --max-time 120、错误 HTML/单帧/网关页均判失败"
```

### Task 4: deploy/README.md 修订

**Files:**
- Modify: `deploy/README.md`

- [ ] **Step 1: 重写双节点小节**

`## 双节点` 小节替换为：

```markdown
## 双节点角色与资源
- 本地应用节点：完整 ECHO-CHAT（docker/compose，127.0.0.1:7080 仅回环）+ frpc（deploy/app）。建议 ≥4核8GB / 80GB；本地断电/休眠/断网即断公网。
- 公网边缘节点：仅 frps + Nginx + Certbot（deploy/edge），2核2G / 40GB 即可；不运行 ECHO-CHAT/MySQL/语义模型。
- 请求链路：`https://域名 → 云端Nginx:443 → 云端127.0.0.1:39001(frps HTTP vhost) → FRP隧道 → 本地frpc → 127.0.0.1:7080`。
- 域名：A 记录 `chat.example.com → 云端公网IPv4`（与本地宽带无关；大陆服务器网站需 ICP 备案）。
- 变量对照：见 deploy/{app,edge}/.env.example（common: PUBLIC_DOMAIN/FRP_AUTH_TOKEN 两端一致）。
- FRP token：`openssl rand -hex 32`。
```

并把原"单机演示(主方案 §3.2)：入口与应用同机时可让 frpc serverAddr 指向 127.0.0.1"改为：

```markdown
- 单机演示（frpc serverAddr=127.0.0.1）：仅作功能测试，**非推荐生产方案**（生产用双节点）。
```

- [ ] **Step 2: 一键小节补首启顺序**

`## 一键` 后追加两行：

```markdown
- 首启顺序：可先跑 deploy-edge.sh（只验云端 frps/Nginx/TLS，见下），本地 frpc 上线后再跑 smoke-test.sh。
- deploy-edge.sh 结尾：`/edge-healthz` 必 200；`/api/health` 按 200=通 / 502·503·504=本地未就绪 / 其它=报错 分类，**不把本地未上线误报为云端失败**。
```

- [ ] **Step 3: MySQL 小节措辞**

把 `## MySQL（应用 VM，外部/宿主机）` 标题改为 `## MySQL（本地应用节点，宿主机/外部）`，注释中 `MYSQL_DSN=…` 不变（host.docker.internal 已指本地）。

- [ ] **Step 4: 镜像注意与观测补位**

`## 镜像注意` 替换为：

```markdown
## 镜像注意
- frps/frpc 默认 snowdreamtech:0.62.1，**两端版本必须一致**；Dashboard 默认关闭。
- 上线前核对镜像（P1-6）：架构、ENTRYPOINT、配置路径、`verify -c` 可用、目标机 `docker image inspect` 后固定 digest。
```

`## 证书续期（edge）` 之后追加：

```markdown
## 可观测性（Dashboard 关闭后的补位）
- 容器存活：docker compose ps / systemd；39000 监听：ss -lnt。
- 公网探测：`/edge-healthz`(TLS/Nginx) 与 `/api/health`(业务) 定时 curl。
- 告警建议：5xx 比例、带宽、CPU、内存、磁盘；frpc 离线检测（frps 日志 no proxy / smoke 失败即告警）。
- 2核2G 守护：可加 1–2GB swap（master §7.2）；容器内存上限需先在真实流量采样再定（量级：nginx ~256MB、frps 256–512MB），勿用未验证硬限。
```

`## 安全组` 相关既有行追加策略说明：

```markdown
- 39000 访问策略：本地出口公网IP稳定→安全组仅限该 IP；动态(家庭/移动宽带)→临时开放时**必须** frps TLS 强制 + 高强度 token + 登录失败监控 + 定期轮换 token。不要长期不限来源。
```

- [ ] **Step 5: 上线门禁清单**

`## 交付边界` 追加：

```markdown
## 上线前门禁（合并 main / 正式上线前须完成，结果记入验收文档）
- 真实 2核2G 云主机跑通 deploy-edge.sh（含首次证书申请）；本地 deploy-app.sh 后 L1–L4 分层通过。
- T 验收：见 docs/deploy/2026-09-06-echo-chat-direct-public-deployment-and-tunnel-removal.md §13 与评审 §7（T01–T18）。
- 演练：frpc 断连/恢复、云主机重启自动恢复、certbot renew --dry-run + reload hook。
- 端口扫描确认：80/443/39000 符合策略；39001/7500 公网不可达。
```

- [ ] **Step 6: 验证 + Commit**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
# 检查要点是否都出现
grep -c '非推荐生产\|edge-healthz\|502\|39000 访问策略\|上线前门禁' deploy/README.md
git add deploy/README.md
git commit -m "docs(deploy): README 双节点角色/自检语义/观测补位/39000策略/上线门禁"
```

---

### Task 5: 全量静态验收 + 分支提交

**Files:**（只读核对 + 最终 git 操作）

- [ ] **Step 1: 语法/渲染全链冒烟**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
for f in deploy/scripts/deploy-edge.sh deploy/scripts/deploy-app.sh deploy/scripts/smoke-test.sh deploy/scripts/render-config.sh deploy/scripts/lib.sh; do bash -n "$f" || exit 1; done; echo "bash -n OK"
for f in deploy/edge/compose.yaml deploy/edge/tunnel/frps.yaml.envsubst deploy/app/tunnel/frpc.yaml.envsubst deploy/edge/nginx/echo-chat.conf.envsubst deploy/edge/nginx/echo-chat.bootstrap.conf.envsubst; do
  python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1])); print('yaml OK', sys.argv[1])" "$f"
done
grep -rn 'FRP_DASHBOARD_PASSWORD\|:7500:' deploy/ || echo "no dashboard/7500 residue"
```

- [ ] **Step 2: 渲染冒烟（app+edge dummy，验证 additionalScopes/tls.force、无 webServer、无残留）**

```bash
cd /home/pp/Desktop/ls_study/proj/tmp/t1/ECHO-CHAT
cp deploy/app/.env.example deploy/app/.env; cp deploy/edge/.env.example deploy/edge/.env
sed -i -E 's/CHANGE_ME_random_hex_64/1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff/g; s/CHANGE_ME_sk-xxx/sk-dummy/; s#CHANGE_ME_vector_db_url#http://vdbtest:60000#; s/CHANGE_ME_vector_db_user/u/; s/CHANGE_ME_vector_db_pwd/p/' deploy/app/.env
sed -i -E 's/CHANGE_ME_random_hex_64/1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff/; s/CHANGE_ME_admin@example.com/admin@example.com/' deploy/edge/.env
bash deploy/scripts/render-config.sh app
bash deploy/scripts/render-config.sh edge
grep -A4 '^auth:' deploy/edge/tunnel/frps.yaml | grep -q HeartBeats && echo "frps scopes OK"
grep -A6 '^auth:' deploy/app/tunnel/frpc.yaml | grep -q NewWorkConns && echo "frpc scopes OK"
grep -A3 '^transport:' deploy/edge/tunnel/frps.yaml | grep -q 'force: true' && echo "frps tls.force OK"
grep -c 'webServer' deploy/edge/tunnel/frps.yaml || true   # 0
grep -c '\${' deploy/edge/tunnel/frps.yaml deploy/edge/tunnel/frpc.yaml || true  # 0 each
rm -f deploy/app/.env deploy/edge/.env docker/config/backend.yaml docker/config/service.yaml deploy/app/tunnel/frpc.yaml deploy/edge/tunnel/frps.yaml
```

- [ ] **Step 3: git 卫生与提交清单**

```bash
git status --short    # 期望：仅 openai-api-proxy/dev.config.yaml 的 M 与本分支未 push 提交；无渲染产物/.env 被跟踪
git log --oneline main..HEAD    # 本分支相对 main 的提交
```

- [ ] **Step 4: Push 分支**

```bash
git push -u origin fix/edge-2c2g-frp-deployment
```

完成后总结：分支上提交 SHA 列表、本机验证通过项、目标机/上线门禁待办（T01–T18）。
