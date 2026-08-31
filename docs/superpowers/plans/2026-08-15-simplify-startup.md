# ai-chat 一键启动/停止 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 `start.sh`/`stop.sh` 两个脚本一键后台启动/停止 ai-chat 全部 8 个服务，缺失的二进制/前端自动补编译。

**Architecture:** 三个 shell 文件共享服务定义：`lib.sh` 存服务表（name|port|启动目录|命令，符合 config 里相对日志路径的 CWD 依赖）+ 公共工具函数；`start.sh` 负责环境/补编译/按序后台启动+等端口+结果汇总；`stop.sh` 按 PID 文件 SIGTERM（回退 `fuser -k -TERM <port>/tcp`）停止。不修改任何现有服务代码与配置文件。

**Tech Stack:** bash（`#!/usr/bin/env bash`）、`ss`、`fuser`、`nohup`、`go`、`pnpm`

**前置条件（已确认）：**
- 8 个服务二进制已在 `ai-chat/bin/`，前端已 build 进 `ai-chat-backend/www/`
- 8 个服务当前正在运行（`ss` 可见）
- `fuser` 在 `/usr/bin/fuser`，`nuxt` 在 `~/.local/bin/nuxt`
- `ai-chat` 是独立 git 仓库（`master` 分支），有若干未提交改动 —— **只提交本计划新增/修改的文件，`git add` 只列具体路径**

## Global Constraints

- 不改任何服务源码、`dev.config.yaml`、`kvstore-ai.conf`；相对路径问题一律用「启动时 `cd` 到服务目录」绕开
- 启动顺序固定：kvstore → tokenizer → sensitive → keyword → mock → proxy → service → backend（依赖方在后）
- 端口表：kvstore=5160、tokenizer=3002、sensitive=50053、keyword=50054、mock=8083、proxy=8084、service=50055、backend=7080
- 服务名（脚本内）：`kvstore tokenizer sensitive keyword mock proxy service backend`
- 日志重定向到 `ai-chat/runtime/logs/<name>.log`，PID 到 `ai-chat/runtime/pids/<name>.pid`，目录自动 `mkdir -p`
- `runtime/` 加入 `.gitignore`
- 幂等：端口已在监听的跳过（标 `already running`），不重复拉起
- 补编译规则：目标二进制缺失 / 存在更新的 `*.go` / `--rebuild` 时重建；前端 `ai-chat-web/dist/index.html` 或 `ai-chat-backend/www/index.html` 缺失或 `--rebuild` 时重跑 `pnpm install --fetch-retries=15 && pnpm build-only && cp`
- 所有脚本 `bash -n` 通过、`chmod +x`

---

### Task 1: `lib.sh` — 服务定义表 + 公共工具

**Files:**
- Create: `lib.sh`

**Interfaces:**
- Produces: `BASE`（ai-chat 目录）、`KVROOT`（9.1-kvstore）、`LOG_DIR`、`PID_DIR`；`SERVICES`（8 行 `name|port|cwd|command`）；函数 `logfile <name>`、`pidfile <name>`、`port_listening <port>`、`pid_alive <pid>`、`SERVICE_NAMES`

- [ ] **Step 1: 写 lib.sh**

```bash
#!/usr/bin/env bash
# ai-chat 服务定义与公共工具 —— 被 start.sh / stop.sh source
# 每个服务一行：name|port|cwd|command
# cwd 是启动工作目录（配置文件里 logPath/aof 为相对路径，依赖 CWD）
set -u

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KVROOT="$(dirname "$BASE")"
LOG_DIR="$BASE/runtime/logs"
PID_DIR="$BASE/runtime/pids"

# 顺序即启动顺序（依赖方在后），停止时按逆序处理
SERVICES=(
  "kvstore|5160|$KVROOT|./kvstore ai-chat/configs/kvstore-ai.conf"
  "tokenizer|3002|$BASE/tokenizer|nuxt --port 3002 --module tokenizer.py --workers 2"
  "sensitive|50053|$BASE/keywords-filter|$BASE/bin/keywords-filter --config=dev.config.yaml --dict=dict.txt"
  "keyword|50054|$BASE/keywords-filter|$BASE/bin/keywords-filter --config=dev.kw.config.yaml --dict=keyword-dict.txt"
  "mock|8083|$BASE/mock-openai-api|$BASE/bin/mock-openai-api --config=dev.config.yaml"
  "proxy|8084|$BASE/openai-api-proxy|$BASE/bin/openai-api-proxy --config=dev.config.yaml"
  "service|50055|$BASE/ai-chat-service|$BASE/bin/ai-chat-service --config=dev.config.yaml"
  "backend|7080|$BASE/ai-chat-backend|$BASE/bin/ai-chat-backend --config=dev.config.yaml"
)

SERVICE_NAMES="$(for e in "${SERVICES[@]}"; do echo "${e%%|*}"; done | tr '\n' ' ')"

logfile() { echo "$LOG_DIR/$1.log"; }
pidfile() { echo "$PID_DIR/$1.pid"; }

# 端口是否在监听（ss -tln 的 Local Address 列，IPv4 形如 0.0.0.0:5160，IPv6 形如 *:50053）
port_listening() {
  local port="$1"
  ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"
}

pid_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
```

- [ ] **Step 2: 语法检查 + 冒烟测试**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
bash -n lib.sh
source lib.sh
echo "BASE=$BASE KVROOT=$KVROOT"
echo "SERVICE_NAMES=$SERVICE_NAMES"
port_listening 5160 && echo "5160 listening OK"          # 当前 kvstore 在跑 → true
port_listening 9999 || echo "9999 not listening OK"       # 未占用 → false
echo "kvstore pidfile=$(pidfile kvstore)"
```

Expected: 无语法错误；`BASE=/home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat`、`KVROOT=/home/pp/Desktop/ls_study/proj/9.1-kvstore`；`SERVICE_NAMES` 含全部 8 个名字；`5160 listening OK`；`9999 not listening OK`。

- [ ] **Step 3: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add lib.sh
git commit -m "feat: ai-chat 服务定义表 lib.sh"
```

---

### Task 2: `start.sh` — 一键后台启动

**Files:**
- Create: `start.sh`

**Interfaces:**
- Consumes: `lib.sh` 的 `BASE/KVROOT/LOG_DIR/PID_DIR/SERVICES/logfile/pidfile/port_listening`
- Produces: `runtime/logs/<name>.log`、`runtime/pids/<name>.pid`；退出码 0（全部就绪）/非 0（有失败）

- [ ] **Step 1: 写 start.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

REBUILD=0
[[ "${1:-}" == "--rebuild" ]] && REBUILD=1

export PATH="$PATH:/usr/local/go/bin:$HOME/go/bin:$HOME/.local/bin"
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
mkdir -p "$LOG_DIR" "$PID_DIR"

echo "== 环境/补编译 =="

# 目标二进制缺失 / 存在更新的 *.go / --rebuild 时重建。格式: 名|构建目录|包|输出(相对 BASE)
GO_BUILDS=(
  "ai-chat-backend|$BASE/ai-chat-backend|./cmd/|bin/ai-chat-backend"
  "ai-chat-service|$BASE/ai-chat-service/chat-server|.|bin/ai-chat-service"
  "keywords-filter|$BASE/keywords-filter/filter-server|.|bin/keywords-filter"
  "mock-openai-api|$BASE/mock-openai-api|.|bin/mock-openai-api"
  "openai-api-proxy|$BASE/openai-api-proxy|.|bin/openai-api-proxy"
)

for b in "${GO_BUILDS[@]}"; do
  IFS='|' read -r _name _dir _pkg _out <<< "$b"
  target="$BASE/$_out"
  need=0
  [ $REBUILD -eq 1 ] && need=1
  [ -f "$target" ] || need=1
  find "$_dir" -name '*.go' -newer "$target" -print -quit 2>/dev/null | grep -q . && need=1
  if [ $need -eq 1 ]; then
    echo "  [build] $_name"
    ( cd "$_dir" && go build -o "$target" "$_pkg" )
  fi
done

# 前端：dist 与 backend/www 缺一即重建
frontend_need=0
[ $REBUILD -eq 1 ] && frontend_need=1
[ -f "$BASE/ai-chat-web/dist/index.html" ] || frontend_need=1
[ -f "$BASE/ai-chat-backend/www/index.html" ] || frontend_need=1
if [ $frontend_need -eq 1 ]; then
  echo "  [build] 前端 (pnpm install + build-only)"
  ( cd "$BASE/ai-chat-web" && pnpm install --fetch-retries=15 && pnpm build-only )
  mkdir -p "$BASE/ai-chat-backend/www"
  cp -r "$BASE/ai-chat-web/dist/." "$BASE/ai-chat-backend/www/"
fi

echo "== 启动服务 =="

start_one() {
  local name="$1" port="$2" cwd="$3" cmd="$4" i
  if port_listening "$port"; then
    echo "  [$name] ✔ already running ($port)"
    return 0
  fi
  echo "  [$name] starting ..."
  ( cd "$cwd"; nohup $cmd >"$(logfile "$name")" 2>&1 & echo $! > "$(pidfile "$name")" )
  for i in $(seq 1 30); do
    port_listening "$port" && { echo "  [$name] ✔ $port listening"; return 0; }
    sleep 0.5
  done
  echo "  [$name] ✘ 启动失败, 日志尾部:"
  tail -n 5 "$(logfile "$name")" 2>/dev/null | sed 's/^/    /'
  return 1
}

total=0; ok=0; failed=()
for entry in "${SERVICES[@]}"; do
  IFS='|' read -r name port cwd cmd <<< "$entry"
  total=$((total+1))
  if start_one "$name" "$port" "$cwd" "$cmd"; then ok=$((ok+1)); else failed+=("$name"); fi
done

echo
if [ ${#failed[@]} -eq 0 ]; then
  echo "✔ $ok/$total 服务就绪 → http://localhost:7080"
else
  echo "✘ ${#failed[@]} 个失败: ${failed[*]}; 日志在 runtime/logs/, 用 ./stop.sh 清理后重试"
  exit 1
fi
```

- [ ] **Step 2: 语法检查 + 幂等测试（当前 8 服务都在跑，正好验证跳过逻辑）**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
bash -n start.sh
chmod +x start.sh
./start.sh
```

Expected: `[build]` 阶段**全部跳过**（二进制齐全、无新改动）；`[name] ✔ already running` ×8；末尾 `✔ 8/8 服务就绪 → http://localhost:7080`。

- [ ] **Step 3: 验证「缺失自动补编译」**（不影响运行中的服务）

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
mv bin/ai-chat-backend /tmp/ai-chat-backend.bak
./start.sh    # 应显示 [build] ai-chat-backend，随后 backend 仍是 already running
ls -la bin/ai-chat-backend    # 二进制已重建
ss -tln | grep 7080            # 端口仍在（服务未重启）
```

Expected: `[build] ai-chat-backend` 出现、`bin/ai-chat-backend` 重建成功、7080 保持监听。

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add start.sh
git commit -m "feat: ai-chat 一键后台启动 start.sh"
```

---

### Task 3: `stop.sh` — 一键停止

**Files:**
- Create: `stop.sh`

**Interfaces:**
- Consumes: `lib.sh` 的 `SERVICES/SERVICE_NAMES/pidfile/port_listening/pid_alive`
- 用法：`./stop.sh`（停全部，按 SERVICES 逆序）/ `./stop.sh <name> [<name>...]`（停指定，name 或端口号均可）

- [ ] **Step 1: 写 stop.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TARGETS=()   # 元素: "name|port"，保持 SERVICES 顺序，停时逆序处理

collect_targets() {
  local arg found entry name port cwd cmd
  if [ $# -eq 0 ]; then
    for entry in "${SERVICES[@]}"; do
      IFS='|' read -r name port cwd cmd <<< "$entry"
      TARGETS+=("$name|$port")
    done
    return
  fi
  for arg in "$@"; do
    found=0
    for entry in "${SERVICES[@]}"; do
      IFS='|' read -r name port cwd cmd <<< "$entry"
      if [ "$arg" = "$name" ] || [ "$arg" = "$port" ]; then
        TARGETS+=("$name|$port"); found=1; break
      fi
    done
    if [ $found -eq 0 ]; then echo "警告: 未知服务/端口 '$arg' (可用: $SERVICE_NAMES)"; fi
  done
}

stop_one() {
  local name="$1" port="$2" pf pid i
  pf="$(pidfile "$name")"
  if [ -f "$pf" ] && pid_alive "$(cat "$pf")"; then
    pid="$(cat "$pf")"
    kill -TERM "$pid"
    echo "  [$name] TERM 已发送"
  elif port_listening "$port"; then
    fuser -k -TERM "${port}/tcp" 2>/dev/null || true
    echo "  [$name] fuser 回退停止"
  else
    echo "  [$name] 未运行"
  fi
  rm -f "$pf"
}

collect_targets "$@"

echo "== 停止 $((${#TARGETS[@]})) 个服务 =="
for ((i = ${#TARGETS[@]} - 1; i >= 0; i--)); do
  IFS='|' read -r name port <<< "${TARGETS[$i]}"
  stop_one "$name" "$port"
done

# 等端口释放（最多 10s）；仍占用的回退 fuser 强停
for t in "${TARGETS[@]}"; do
  IFS='|' read -r name port <<< "$t"
  for i in $(seq 1 20); do
    port_listening "$port" || break
    sleep 0.5
  done
  if port_listening "$port"; then
    echo "  [$name] $port 未释放, fuser 强停"
    fuser -k -TERM "${port}/tcp" 2>/dev/null || true
    sleep 1
  fi
done

# 汇总
remaining=0
for t in "${TARGETS[@]}"; do
  IFS='|' read -r name port <<< "$t"
  port_listening "$port" && { remaining=$((remaining+1)); echo "  [✘] $name $port 仍在监听"; }
done
if [ $remaining -eq 0 ]; then echo "✔ 全部停止, 端口已释放"; else exit 1; fi
```

- [ ] **Step 2: 语法检查 + 优雅失败测试**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
bash -n stop.sh
chmod +x stop.sh
./stop.sh does-not-exist
```

Expected: 无语法错误；警告 `未知服务/端口 'does-not-exist'`；随后 `== 停止 0 个服务 ==`、`✔ 全部停止, 端口已释放`（TARGETS 空，remaining=0）。**不杀任何服务**。

- [ ] **Step 3: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add stop.sh
git commit -m "feat: ai-chat 一键停止 stop.sh"
```

---

### Task 4: 端到端验证（会短暂停掉正在运行的 8 个服务，执行前先跟用户确认）

**Files:** 无新增/修改

**说明：** 本任务要停掉当前在跑的 8 个服务再冷启动，属破坏性操作 —— 动手前先向用户说明并获确认（用户可能正在用 http://localhost:7080 页面）。

- [ ] **Step 1: 完整周期验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
./start.sh                      # 现在全在跑 → 应 8 个 already running
./stop.sh                       # 全部停止
ss -tlnp | grep -E "3002|50053|50054|50055|7080|8083|8084|5160" || echo "全部已释放"
./start.sh                      # 冷启动
./stop.sh                       # 再次停止，验证可重复
./start.sh                      # 最后一次启动，恢复用户环境为「运行中」
```

Expected:
- 停干净：8 端口无监听
- 冷启动全绿：`✔ 8/8 服务就绪 → http://localhost:7080`

- [ ] **Step 2: 业务可用性验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7080/                      # 期望 200
curl -s -X POST http://localhost:7080/api/chat-process \
  -H "Content-Type: application/json" -d '{"prompt":"你好","options":{}}' | head -c 200   # 期望流式 JSON
mysql -h127.0.0.1 -uroot -p123456 ai_chat \
  -e "SELECT id, user_msg FROM chat_records ORDER BY id DESC LIMIT 1;"
tail -n 3 runtime/logs/service.log    # 日志落盘可查
```

Expected: 页面 200；chat-process 返回 JSON 片段；MySQL 有新记录；`runtime/logs/*.log` 有内容。

- [ ] **Step 3: 无提交**（无文件改动；最终状态：8 服务运行中，与验证前一致）

---

### Task 5: `.gitignore` + 运行步骤文档

**Files:**
- Modify: `.gitignore`
- Modify: `运行步骤.md`

- [ ] **Step 1: `.gitignore` 加 `runtime/`**

在 `.gitignore` 末尾追加一行：

```
runtime/
```

- [ ] **Step 2: `运行步骤.md` 开头加「最简方式」节**

在 `运行步骤.md` 第 1 行（`# ai-chat 本地运行步骤`）后、`> 本项目现已整合...` 引言前插入：

````markdown
## 最简方式（推荐）

二进制与前端编译好之后，日常启停只需两条命令：

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
./start.sh    # 一键后台启动全部 8 个服务（二进制/前端缺失或过期会自动补编译）
./stop.sh     # 一键全部停止；或 ./stop.sh <服务名> 停单个
```

- 各服务日志在 `runtime/logs/<服务名>.log`，排查时 `tail -f` 对应文件
- 强制全量重编译后启动：`./start.sh --rebuild`
- 下方手工分步命令保留，供单独启动/排查单个服务时使用
````

- [ ] **Step 3: 验证**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
grep -n 'runtime/' .gitignore
git check-ignore runtime/logs/kvstore.log || echo "runtime 未被忽略!"
grep -n '最简方式' 运行步骤.md
```

Expected: `.gitignore` 含 `runtime/`；`git check-ignore` 命中（输出 `runtime/logs/kvstore.log`）；`运行步骤.md` 含「最简方式」。

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/ai-chat
git add .gitignore 运行步骤.md
git commit -m "docs: 一键启停最简方式 + runtime 忽略"
```

---

## 自审结论

- **Spec 覆盖：** start.sh（补编译/幂等/等端口/汇总）→Task 2；stop.sh（PID→fuser 回退/复查）→Task 3；runtime 目录与 .gitignore →Task 1/5；运行步骤.md 最简方式 →Task 5；冷启动验收 →Task 4。
- **无占位符：** 所有脚本代码已给出完整可粘贴内容。
- **类型一致性：** `lib.sh` 暴露的 `SERVICES` 字段序 `name|port|cwd|command` 在 start.sh/stop.sh 中统一用 `IFS='|' read` 解析；`logfile/pidfile/port_listening/pid_alive` 签名在三个任务中一致。服务名 `sensitive/keyword/mock/proxy/service/backend` 全局一致。
