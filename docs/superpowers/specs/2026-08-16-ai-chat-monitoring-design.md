# ai-chat 监控（Prometheus + Grafana）设计文档

- 日期：2026-08-16
- 状态：已批准
- 位置：`ai-chat/monitoring/`（独立于 8 个服务）

## 背景与目标

ai-chat-service 已在 `:8080/metrics` 暴露标准 Prometheus 格式指标（`prometheus/client_golang`，含业务计数器、gRPC 流中间件指标、Go 运行时指标），但**没有采集端**——没有任何进程去 scrape，数据躺在端点没人拉。本设计补齐"采集 → 存储 → 可视化"整条链。

目标：
- 用 Prometheus 采集 `:8080/metrics`，用 Grafana 可视化
- 监控与 ai-chat 主链路**互相独立**：8 个服务的启停不受影响，监控二进制缺失时报错提示而非拖垮主链路
- 全部用后台进程 + pid + 日志，启停脚本幂等，模式复用 ai-chat 现有 start.sh/stop.sh

## 已确认决策

- Prometheus 二进制：用户已下载 `prometheus-2.45.0.linux-amd64.tar.gz`（`/home/pp/Desktop/ls_study/proj/`）
- Grafana 二进制：用户已下载 `grafana-10.4.0.linux-amd64.tar.gz`（同上）
- 只采集 ai-chat-service :8080，**不做 kvstore exporter**（YAGNI）
- 独立 `monitoring/` 目录 + 独立 start.sh/stop.sh（不并入现有 ai-chat start.sh）

## 目录结构

```
ai-chat/monitoring/
├── bin/                        # prometheus 二进制（.gitignore）
├── prometheus/
│   ├── prometheus.yml          # scrape 配置
│   └── data/                   # TSDB 数据（.gitignore）
├── grafana/
│   ├── bin/grafana             # 用户解压后放入：grafana 二进制
│   ├── public/ conf/           # 同目录解压出的资源（homepath 依赖）
│   ├── conf/custom.ini         # 覆盖配置：http_port=3000、paths.data、paths.provisioning
│   ├── provisioning/
│   │   ├── datasources/prometheus.yml   # 自动配数据源 → http://127.0.0.1:9090
│   │   └── dashboards/dashboards.yml     # 自动载入 dashboards/ai-chat.json
│   └── data/                   # Grafana 数据（.gitignore）
├── dashboards/ai-chat.json     # 手写基础看板（Grafana dashboard model）
├── logs/ pids/                 # 运行日志与 pid（.gitignore）
├── start.sh  stop.sh           # 启停
└── README.md                   # 解压步骤 + 使用说明
```

`.gitignore` 追加：`monitoring/bin/`、`monitoring/prometheus/data/`、`monitoring/grafana/data/`、`monitoring/logs/`、`monitoring/pids/`。

## 关键配置

### prometheus.yml
```yaml
global:
  scrape_interval: 5s
  evaluation_interval: 5s
scrape_configs:
  - job_name: ai-chat-service
    static_configs:
      - targets: ["127.0.0.1:8080"]
```

### Grafana provisioning 数据源
```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
```

### Grafana provisioning 看板
`dashboards.yml` 指向 `../dashboards/` 目录，载入 `ai-chat.json`。

### Grafana custom.ini（覆盖项）
- `[server] http_port = 3000`
- `[paths] data` 指向 `monitoring/grafana/data`
- `[paths] provisioning` 指向 `monitoring/grafana/provisioning`
- 其余继承 Grafana 默认配置（admin/admin 默认登录）

## start.sh / stop.sh

复用 ai-chat 现有脚本模式（bash、`set -euo pipefail`、nohup 后台、pid 文件、`ss` 等端口、幂等跳过、fuser 回退停止）：

- `start.sh`：前置检查 `bin/prometheus` 与 `grafana/bin/grafana` 存在（缺失则打印 README 指引并退出）；`mkdir -p` 数据/日志/pid 目录；幂等（:9090/:3000 已在监听则跳过）；按序启动 Prometheus → 等 :9090 → 启动 Grafana → 等 :3000；打印访问地址与 scrape 状态
- `stop.sh`：按 pid 文件精确 TERM，缺失回退 `fuser -k -TERM 9090/tcp 3000/tcp`；复查端口释放
- 二者独立于 ai-chat 的 start.sh/stop.sh，互不调用

## 看板面板（PromQL）

| 面板 | PromQL |
|---|---|
| 请求速率 | `rate(ai_chat_chat_service_requests_total[5m])` |
| 完成问题数 | `rate(ai_chat_chat_service_questions_total[5m])` |
| 敏感词命中 | `rate(ai_chat_chat_service_sensitive_questions_total[5m])` |
| 错误数 | `rate(ai_chat_chat_service_err_questions_total[5m])` |
| 延迟 p90 | `ai_chat_chat_service_request_duration_ms{quantile="0.9"}` |
| goroutine 数 | `ai_chat_chat_service_curr_num_goroutine` |

## 用户手动步骤（README 文档化）

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat/monitoring
# 1. 解压 prometheus（压缩包已在 /home/pp/Desktop/ls_study/proj/；顶层目录 prometheus-2.45.0.linux-amd64/）
tar xzf /home/pp/Desktop/ls_study/proj/prometheus-2.45.0.linux-amd64.tar.gz -C /tmp
cp /tmp/prometheus-2.45.0.linux-amd64/prometheus bin/
# 2. 解压 grafana（顶层目录 grafana-v10.4.0/；改名为 grafana 作为 --homepath）
tar xzf /home/pp/Desktop/ls_study/proj/grafana-10.4.0.linux-amd64.tar.gz
mv grafana-v10.4.0 grafana
```

> 已核实：Grafana tarball 顶层目录为 `grafana-v10.4.0/`，含 `bin/grafana`、`conf/`、`public/`。start.sh 的 `--homepath` 指向 `monitoring/grafana/`。

## 验收标准

1. `monitoring/start.sh` → Prometheus :9090 与 Grafana :3000 均监听
2. Prometheus 已 scrape：`curl 'http://localhost:9090/api/v1/query?query=ai_chat_chat_service_questions_total'` 返回 JSON，value>0（发一条聊天后）
3. Grafana :3000 可登录（admin/admin），Prometheus 数据源已自动配置，看板自动载入且面板有数据
4. `monitoring/stop.sh` → 9090/3000 释放
5. 监控与 ai-chat 主链路互不影响：缺二进制时 start.sh 报错退出，不碰 8 个服务；`ai-chat/start.sh` 仍正常
6. 解压/启动命令 `bash -n` 通过、start/stop 幂等

## 明确不做（Out of scope）

- kvstore exporter（INFO/MEMSTAT 转 Prometheus）—— 后续再做
- 告警规则（Alertmanager）—— 需要时再加
- systemd 自启 / 生产部署
- 修改 ai-chat 现有 start.sh/stop.sh
