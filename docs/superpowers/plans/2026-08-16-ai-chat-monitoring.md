# ai-chat 监控（Prometheus + Grafana）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `ai-chat/monitoring/` 下搭一套 Prometheus + Grafana，采集并可视化 ai-chat-service 的 `:8080/metrics`，独立于 8 个服务启停。

**Architecture:** `monitoring/` 独立目录：配置文件（prometheus.yml + Grafana provisioning + 看板 JSON）提交进 git；下载的二进制（`monitoring/bin/`、`monitoring/grafana/`）和运行时数据（`monitoring/prometheus/data/`、`monitoring/logs/`、`monitoring/pids/`）gitignore。start.sh/stop.sh 复用 ai-chat 现有脚本模式（nohup + pid + `ss` 等端口 + 幂等 + fuser 回退）。Grafana 用 `GF_*` 环境变量注入路径（start.sh 动态计算），不用写死绝对路径的 custom.ini。

**Tech Stack:** Prometheus 2.45.0（二进制已下载）、Grafana 10.4.0（已下载）、bash、`ss`/`fuser`

## Global Constraints

- 端口：Prometheus :9090、Grafana :3000；两者当前未占用（已实测）
- 数据源：`job_name=ai-chat-service`，target `127.0.0.1:8080`，`scrape_interval: 5s`
- Grafana 数据源 url `http://127.0.0.1:9090`，`uid: prometheus`（看板面板引用这个 uid），`isDefault: true`
- Grafana 用环境变量注入：`GF_SERVER_HTTP_PORT=3000`、`GF_PATHS_DATA=$ROOT/grafana/data`、`GF_PATHS_PROVISIONING=$ROOT/config/grafana/provisioning`、`GF_SECURITY_DISABLE_INITIAL_ADMIN_PASSWORD_CHANGE=true`
- **不修改** ai-chat 现有 start.sh/stop.sh 与任何服务代码；监控与 8 个服务互不影响
- 二进制解压路径（tarball 顶层目录已实测）：prometheus → `bin/prometheus`、`bin/promtool`；grafana → `monitoring/grafana/`（bundle 即 homepath）
- `.gitignore` 追加：`monitoring/grafana/`、`monitoring/prometheus/data/`、`monitoring/logs/`、`monitoring/pids/`（`monitoring/bin/` 已被现有 `bin/` 覆盖）
- 所有脚本 `bash -n` 通过、`chmod +x`
- 提交只在 ai-chat 仓库 master；`git add` 只加计划内文件，不动用户其他未提交改动

### 对 spec 的偏差（实现时按此执行）

- Grafana 配置**不用** `grafana/conf/custom.ini`，改用 start.sh 里的 `GF_*` 环境变量（避免在提交的配置文件里写死绝对路径；Grafana `[paths]` 相对路径是相对 homepath 解析的，env 注入最稳）
- provisioning 文件放 `monitoring/config/grafana/provisioning/`（提交），整个 `monitoring/grafana/` bundle gitignore

---

### Task 1: 配置文件（prometheus.yml + Grafana provisioning + 看板 JSON + .gitignore）

**Files:**
- Create: `monitoring/prometheus/prometheus.yml`
- Create: `monitoring/config/grafana/provisioning/datasources/prometheus.yml`
- Create: `monitoring/config/grafana/provisioning/dashboards/dashboards.yml`
- Create: `monitoring/dashboards/ai-chat.json`
- Modify: `.gitignore`

**Interfaces:**
- Produces: 配置文件（绝对路径无关，全部相对/无路径）；`dashboards/ai-chat.json` 供 dashboards.yml 的 `options.path: dashboards`（CWD=monitoring 时解析到该目录）；看板面板引用 datasource `uid: prometheus`

- [ ] **Step 1: 建目录 + prometheus.yml**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
mkdir -p monitoring/prometheus monitoring/config/grafana/provisioning/datasources monitoring/config/grafana/provisioning/dashboards monitoring/dashboards
```

写 `monitoring/prometheus/prometheus.yml`：
```yaml
global:
  scrape_interval: 5s
  evaluation_interval: 5s

scrape_configs:
  - job_name: ai-chat-service
    static_configs:
      - targets: ["127.0.0.1:8080"]
```

- [ ] **Step 2: Grafana 数据源 provisioning**

写 `monitoring/config/grafana/provisioning/datasources/prometheus.yml`：
```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    uid: prometheus
    url: http://127.0.0.1:9090
    isDefault: true
```

- [ ] **Step 3: Grafana 看板 provisioning**

写 `monitoring/config/grafana/provisioning/dashboards/dashboards.yml`：
```yaml
apiVersion: 1
providers:
  - name: "ai-chat"
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    updateIntervalSeconds: 10
    options:
      path: dashboards
```

- [ ] **Step 4: 看板 JSON**

写 `monitoring/dashboards/ai-chat.json`（schemaVersion 39，Grafana 10）：
```json
{
  "annotations": { "list": [] },
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 0,
  "id": null,
  "links": [],
  "panels": [
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "reqps" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "id": 1,
      "options": { "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "single", "sort": "none" } },
      "targets": [ { "datasource": { "type": "prometheus", "uid": "prometheus" }, "expr": "rate(ai_chat_chat_service_requests_total[5m])", "legendFormat": "{{full_method}}", "refId": "A" } ],
      "title": "请求速率",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "reqps" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "id": 2,
      "options": { "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "single", "sort": "none" } },
      "targets": [ { "datasource": { "type": "prometheus", "uid": "prometheus" }, "expr": "rate(ai_chat_chat_service_questions_total[5m])", "legendFormat": "questions", "refId": "A" } ],
      "title": "完成问题数",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "reqps" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "id": 3,
      "options": { "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "single", "sort": "none" } },
      "targets": [ { "datasource": { "type": "prometheus", "uid": "prometheus" }, "expr": "rate(ai_chat_chat_service_sensitive_questions_total[5m])", "legendFormat": "sensitive", "refId": "A" } ],
      "title": "敏感词命中",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "reqps" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
      "id": 4,
      "options": { "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "single", "sort": "none" } },
      "targets": [ { "datasource": { "type": "prometheus", "uid": "prometheus" }, "expr": "rate(ai_chat_chat_service_err_questions_total[5m])", "legendFormat": "errors", "refId": "A" } ],
      "title": "错误数",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "ms" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 16 },
      "id": 5,
      "options": { "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "single", "sort": "none" } },
      "targets": [ { "datasource": { "type": "prometheus", "uid": "prometheus" }, "expr": "ai_chat_chat_service_request_duration_ms{quantile=\"0.9\"}", "legendFormat": "p90", "refId": "A" } ],
      "title": "请求延迟 p90",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "short" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 16 },
      "id": 6,
      "options": { "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" }, "tooltip": { "mode": "single", "sort": "none" } },
      "targets": [ { "datasource": { "type": "prometheus", "uid": "prometheus" }, "expr": "ai_chat_chat_service_curr_num_goroutine", "legendFormat": "goroutines", "refId": "A" } ],
      "title": "goroutine 数",
      "type": "timeseries"
    }
  ],
  "refresh": "5s",
  "schemaVersion": 39,
  "tags": [],
  "templating": { "list": [] },
  "time": { "from": "now-30m", "to": "now" },
  "timepicker": {},
  "timezone": "browser",
  "title": "ai-chat 监控",
  "uid": "ai-chat-monitoring",
  "version": 1,
  "weekStart": ""
}
```

- [ ] **Step 5: .gitignore 追加**

在 `ai-chat/.gitignore` 末尾追加（保留现有 `.idea`、`bin/`、`runtime/` 三行）：
```
monitoring/grafana/
monitoring/prometheus/data/
monitoring/logs/
monitoring/pids/
```

- [ ] **Step 6: 校验**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
python3 -m json.tool monitoring/dashboards/ai-chat.json > /dev/null && echo "看板 JSON 合法"
grep -n "monitoring/" .gitignore
ls -R monitoring/config monitoring/dashboards | head -20
```

Expected: 看板 JSON 合法；`.gitignore` 含 4 条 monitoring 规则；provisioning/看板目录结构齐全。

- [ ] **Step 7: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add monitoring/prometheus/prometheus.yml monitoring/config/grafana/provisioning monitoring/dashboards/ai-chat.json .gitignore
git commit -m "feat: ai-chat 监控配置文件（prometheus + grafana provisioning + 看板）"
```

---

### Task 2: 解压二进制

**Files:** 无提交（产物全部 gitignore）

- [ ] **Step 1: 解压 prometheus**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
mkdir -p bin
tar xzf /home/pp/Desktop/ls_study/proj/prometheus-2.45.0.linux-amd64.tar.gz -C /tmp
cp /tmp/prometheus-2.45.0.linux-amd64/prometheus bin/
cp /tmp/prometheus-2.45.0.linux-amd64/promtool bin/
```

- [ ] **Step 2: 解压 grafana**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
tar xzf /home/pp/Desktop/ls_study/proj/grafana-10.4.0.linux-amd64.tar.gz
mv grafana-v10.4.0 grafana
```

- [ ] **Step 3: 校验**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
bin/prometheus --version | head -1
bin/promtool check config prometheus/prometheus.yml
grafana/bin/grafana --version
```

Expected: `prometheus, version 2.45.0...`；`SUCCESS`；`Version 10.4.0...`。

- [ ] **Step 4: 无提交**（`monitoring/bin/` 与 `monitoring/grafana/` 均已被 .gitignore 忽略）

---

### Task 3: start.sh / stop.sh + 端到端验证

**Files:**
- Create: `monitoring/start.sh`
- Create: `monitoring/stop.sh`

**Interfaces:**
- Consumes: Task 1 的配置文件、Task 2 解压的 `bin/prometheus`、`grafana/bin/grafana`
- Produces: `logs/prometheus.log`、`logs/grafana.log`、`pids/prometheus.pid`、`pids/grafana.pid`

- [ ] **Step 1: 写 start.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"
PID_DIR="$ROOT/pids"
mkdir -p "$LOG_DIR" "$PID_DIR" "$ROOT/prometheus/data" "$ROOT/grafana/data"

if [ ! -x "$ROOT/bin/prometheus" ]; then
  echo "✘ 缺 prometheus 二进制: $ROOT/bin/prometheus（请按 README.md 解压）"; exit 1
fi
if [ ! -x "$ROOT/grafana/bin/grafana" ]; then
  echo "✘ 缺 grafana: $ROOT/grafana/bin/grafana（请按 README.md 解压）"; exit 1
fi

port_listening() { ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${1}$"; }

start_prom() {
  if port_listening 9090; then echo "  [prometheus] ✔ already running (9090)"; return 0; fi
  ( cd "$ROOT" && nohup "$ROOT/bin/prometheus" \
      --config.file=prometheus/prometheus.yml \
      --storage.tsdb.path=prometheus/data \
      --web.listen-address=:9090 \
      >"$LOG_DIR/prometheus.log" 2>&1 & echo $! > "$PID_DIR/prometheus.pid" )
  local i
  for i in $(seq 1 40); do port_listening 9090 && { echo "  [prometheus] ✔ 9090 listening"; return 0; }; sleep 0.5; done
  echo "  [prometheus] ✘ 启动失败, 日志尾部:"; tail -n 8 "$LOG_DIR/prometheus.log" | sed 's/^/    /'; return 1
}

start_graf() {
  if port_listening 3000; then echo "  [grafana] ✔ already running (3000)"; return 0; fi
  ( cd "$ROOT" && GF_SERVER_HTTP_PORT=3000 \
      GF_PATHS_DATA="$ROOT/grafana/data" \
      GF_PATHS_PROVISIONING="$ROOT/config/grafana/provisioning" \
      GF_SECURITY_DISABLE_INITIAL_ADMIN_PASSWORD_CHANGE=true \
      nohup "$ROOT/grafana/bin/grafana" server --homepath="$ROOT/grafana" \
      >"$LOG_DIR/grafana.log" 2>&1 & echo $! > "$PID_DIR/grafana.pid" )
  local i
  for i in $(seq 1 60); do port_listening 3000 && { echo "  [grafana] ✔ 3000 listening"; return 0; }; sleep 0.5; done
  echo "  [grafana] ✘ 启动失败, 日志尾部:"; tail -n 8 "$LOG_DIR/grafana.log" | sed 's/^/    /'; return 1
}

start_prom
start_graf
echo
echo "✔ Prometheus: http://localhost:9090  （/targets 看采集状态）"
echo "✔ Grafana:    http://localhost:3000  （admin/admin）"
```

- [ ] **Step 2: 写 stop.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$ROOT/pids"

port_listening() { ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${1}$"; }

stop_one() {
  local name="$1" port="$2" pf pid
  pf="$PID_DIR/$name.pid"
  if [ -f "$pf" ] && pid_alive "$(cat "$pf")"; then
    pid="$(cat "$pf")"; kill -TERM "$pid"; echo "  [$name] TERM 已发送"
  elif port_listening "$port"; then
    fuser -k -TERM "${port}/tcp" 2>/dev/null || true; echo "  [$name] fuser 回退停止"
  else
    echo "  [$name] 未运行"
  fi
  rm -f "$pf"
}

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

echo "== 停止监控 =="
stop_one prometheus 9090
stop_one grafana 3000

for pair in "prometheus 9090" "grafana 3000"; do
  set -- $pair
  name="$1"; port="$2"
  for i in $(seq 1 20); do port_listening "$port" || break; sleep 0.5; done
  if port_listening "$port"; then
    echo "  [$name] $port 未释放, fuser 强停"
    fuser -k -KILL "${port}/tcp" 2>/dev/null || true
    sleep 1
  fi
done

remaining=0
port_listening 9090 && { echo "  [✘] prometheus 9090 仍在监听"; remaining=1; }
port_listening 3000 && { echo "  [✘] grafana 3000 仍在监听"; remaining=1; }
if [ "$remaining" -eq 0 ]; then echo "✔ 监控已停止, 端口已释放"; else exit 1; fi
```

- [ ] **Step 3: 语法检查 + 幂等**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
bash -n start.sh stop.sh
chmod +x start.sh stop.sh
./start.sh
```

Expected: 两脚本无语法错误；`[prometheus] ✔ 9090 listening`、`[grafana] ✔ 3000 listening`、末尾打印两个访问地址。**8 个 ai-chat 服务此刻保持运行，不受影响。**

- [ ] **Step 4: 验证采集**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
sleep 7   # 等第一个 scrape 周期
curl -s 'http://localhost:9090/api/v1/query?query=up'
echo
curl -s -X POST http://localhost:7080/api/chat-process \
  -H "Content-Type: application/json" -d '{"prompt":"你好","options":{}}' > /dev/null
sleep 7   # 等指标被 scrape
curl -s 'http://localhost:9090/api/v1/query?query=ai_chat_chat_service_questions_total'
```

Expected: `up` 查询返回 `ai-chat-service` 的 value=1；`questions_total` 查询返回 value≥1（发了一条聊天后计数器 >0）。

- [ ] **Step 5: 验证 Grafana provisioning**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
curl -s -o /dev/null -w "grafana 登录页: %{http_code}\n" http://localhost:3000/login
grep -iE "Provisioning.*[Dd]atasource|Provisioning.*[Dd]ashboard|Datasource provisioning|Dashboard provisioning" logs/grafana.log | head -5
```

Expected: 登录页 200；grafana.log 里有 datasource 和 dashboard provisioning 成功记录。

- [ ] **Step 6: 幂等 + 停止 + 恢复运行**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
./start.sh                 # 幂等：两个 already running
./stop.sh                  # 停止
ss -tlnp | grep -E ":9090|:3000" || echo "9090/3000 已释放"
./start.sh                 # 重新启动，最终保持监控运行
```

Expected: 幂等全 skip；停止后 9090/3000 释放；最终监控恢复运行（供你打开看板看效果）。

- [ ] **Step 7: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add monitoring/start.sh monitoring/stop.sh
git commit -m "feat: ai-chat 监控启停脚本（prometheus + grafana）"
```

---

### Task 4: README.md

**Files:**
- Create: `monitoring/README.md`

- [ ] **Step 1: 写 README.md**

```markdown
# ai-chat 监控（Prometheus + Grafana）

采集并可视化 `ai-chat-service :8080/metrics`，独立于 8 个服务启停。

## 目录

```
monitoring/
├── bin/                  # prometheus / promtool 二进制（下载，gitignore）
├── grafana/              # grafana 解压目录 = homepath（gitignore）
├── prometheus/prometheus.yml  # scrape 配置
├── config/grafana/provisioning/  # 数据源 + 看板自动配置
├── dashboards/ai-chat.json   # 看板
├── logs/ pids/           # 运行状态（gitignore）
└── start.sh stop.sh
```

## 首次准备（解压二进制）

压缩包在 `/home/pp/Desktop/ls_study/proj/`：

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
# prometheus（顶层目录 prometheus-2.45.0.linux-amd64/）
tar xzf /home/pp/Desktop/ls_study/proj/prometheus-2.45.0.linux-amd64.tar.gz -C /tmp
cp /tmp/prometheus-2.45.0.linux-amd64/prometheus bin/
cp /tmp/prometheus-2.45.0.linux-amd64/promtool bin/
# grafana（顶层目录 grafana-v10.4.0/，改名 grafana）
tar xzf /home/pp/Desktop/ls_study/proj/grafana-10.4.0.linux-amd64.tar.gz
mv grafana-v10.4.0 grafana
```

## 使用

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
./start.sh    # 幂等；起 Prometheus :9090 + Grafana :3000
./stop.sh     # 停止；./stop.sh 无参数
```

- Prometheus Web UI：http://localhost:9090（Targets 页看采集状态）
- Grafana：http://localhost:3000（admin / admin）→ 数据源与看板已自动配置

## 注意事项

- 监控独立于 ai-chat 主链路；缺二进制时 `start.sh` 报错退出，不影响 8 个服务
- Prometheus 数据落在 `prometheus/data/`，Grafana 数据落在 `grafana/data/`（gitignore，删掉即重置）
- 指标计数是进程内的，服务重启会清零（由 Prometheus 侧时序持久化，重启 Prometheus 后历史保留）
```

- [ ] **Step 2: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add monitoring/README.md
git commit -m "docs: ai-chat 监控 README（解压与使用说明）"
```

---

## 自审结论

- **Spec 覆盖**：prometheus.yml→Task1；Grafana 数据源/看板自动配置→Task1；start.sh/stop.sh 幂等与端口→Task3；README 下载步骤→Task4；验收(9090/3000 监听、scrape 有值、Grafana 自动配好、停后释放、与主链路独立)→Task3 Step3-6。
- **无占位符**：全部文件内容已给出完整可粘贴版本。
- **类型/命名一致**：面板 PromQL 用真实指标名（`ai_chat_chat_service_requests_total` 等，已从 `/metrics` 实测核对）；datasource `uid: prometheus` 在 provisioning 与面板中一致；端口 9090/3000 全篇一致。
- **偏差已记录**：Grafana 用 GF_* 环境变量替代 custom.ini（见 Global Constraints）。
