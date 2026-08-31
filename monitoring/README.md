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
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/monitoring
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
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/monitoring
./start.sh    # 幂等；起 Prometheus :9090 + Grafana :3000
./stop.sh     # 停止；./stop.sh 无参数
```

- Prometheus Web UI：http://localhost:9090（Targets 页看采集状态）
- Grafana：http://localhost:3000（admin / admin）→ 数据源与看板已自动配置

## 注意事项

- 监控独立于 ai-chat 主链路；缺二进制时 `start.sh` 报错退出，不影响 8 个服务
- Prometheus 数据落在 `prometheus/data/`，Grafana 数据落在 `grafana/data/`（gitignore，删掉即重置）
- 指标计数是进程内的，服务重启会清零（由 Prometheus 侧时序持久化，重启 Prometheus 后历史保留）
- 停止时按端口 `fuser` 兜底，会杀掉占用 9090/3000 的**任何**进程（当前仅监控进程占用，无碍；若以后端口被复用需留意）
