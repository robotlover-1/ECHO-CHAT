# ECHO-CHAT Docker 全栈（docker compose）

> **未在本仓库开发机验证**（该机无 docker daemon 权限）。请在任意有 Docker 的主机跑，首次按报错微调。这套是**从源码构建**的全栈 Compose，与 `ai-chat-stack/`（已发布镜像的 Swarm 部署）是两条线。

## 一、前置
- Docker Engine + Compose v2；能访问：Go/`goproxy.cn`、pnpm registry、PyPI(清华源)、GitHub Release/加速镜像（semantic 构建期拉模型 79MB）。
- **MySQL**：compose 内各服务通过 `host.docker.internal:3306` 连**宿主机** MySQL（默认 `root/123456`，库 `ai_chat`；backend 启动会自建 `users` 表，`chat_records` 等按你既有初始化补）。若在别处跑，改 `docker/config/service.yaml` / `backend.yaml` 的 `dsn`。
- DeepSeek key 走环境变量（勿写进配置）。

## 二、启动
```bash
cd docker
DEEPSEEK_API_KEY=sk-xxx docker compose up -d --build
docker compose ps              # 全部 running
curl -s http://localhost:7080  # 前端（backend 提供静态页）
```
首次构建较久（go/pnpm/模型）。前端构建在 backend 镜像内自动完成（`node` stage + `pnpm build-only`）。

## 三、常用
```bash
docker compose logs -f ai-chat-service semantic   # 看日志
docker compose restart semantic
docker compose down                                # 停（保留数据卷）
docker compose down -v                             # 连卷一起删（注意 kvstore 数据）
```
- 语义检索：semantic 镜像**构建期**从 GitHub Release 拉 e5 模型并 sha256 校验（`semantic/Dockerfile` 的 `ARG MODEL_URL`，可覆盖）；构建需联网，或用加速镜像地址当 `MODEL_URL`。
- 服务名即容器主机名：backend→service:50055、service→tokenizer/semantic/sensitive/keyword/proxy/kvstore（见 `docker/config/*.yaml`，由各自 dev 配置把 localhost 换成服务名生成）。
- 额外想暴露调试端口可加 `ports:`（如 semantic 3003 / kvstore 5160 对外）。

## 四、kvstore（实验性）
kvstore 是 C/自研（依赖 NtyCo、liburing；默认关 RDMA/eBPF）。`docker/kvstore.Dockerfile` 从源码构建。
- 若构建失败/不想折腾：**kvstore 留在宿主跑**（`./start.sh` 只起 kvstore 或单独 `./kvstore/kvstore/kvstore configs/kvstore-ai.conf`），
  然后：注释 compose 里的 `kvstore` 服务，并把 `docker/config/service.yaml`、`backend.yaml` 的 redis `host` 由 `kvstore` 改为 `host.docker.internal`。
- kvstore 持久化目录以 `configs/kvstore-ai.conf` 的相对路径为准（容器 CWD=/）；要持久化就把它改到挂载卷内路径并放开 compose 末尾注释的 `kvstore-data` 卷。

## 五、与“本地 ./start.sh”对比
| | 本地进程（./start.sh） | Docker（本 compose） |
|---|---|---|
| 宿主依赖 | 需装 go/pip/pnpm + 语义模型 | 只要 Docker；依赖进镜像 |
| 语义模型 | `bash semantic/tools/fetch_model.sh` | 构建期自动拉（镜像自含） |
| 前端 | start.sh 按需 pnpm 重建 | backend 镜像内构建 |
| MySQL | 需本机 MySQL | 仍需宿主机/外部 MySQL（host.docker.internal） |
| kvstore | 宿主 make 运行 | 镜像构建（实验性，可退回宿主跑） |
