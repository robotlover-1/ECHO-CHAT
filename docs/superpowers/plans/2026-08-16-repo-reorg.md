# kvstore + ai-chat 目录重组（InazumaPlasma 平铺式）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 9.1-kvstore 单一仓库重组为 InazumaPlasma 式平铺结构：kvstore 引擎整体迁入 `kvstore/`，ai-chat 全部组件提升到根，根 Makefile + start.sh 编排，消除 `ai-chat/` 嵌套的割裂感。

**Architecture:** 三个任务按依赖顺序：
1. 搬移 1：kvstore 引擎（src/include/Makefile/NtyCo 子模块/二进制/conf/tests/tools/clients/benchmarks 等）→ `kvstore/`，根 docs/ 引擎分析文档 → `kvstore/docs/`
2. 搬移 2：ai-chat/* 全部提升到根（服务目录 + configs/bin/runtime/docs/脚本），删除空 `ai-chat/`
3. 路径更新 + 根 Makefile + .gitignore 合并 + 文档引用修正 + 全量回归

**Tech Stack:** git mv（保历史）、bash、make

## Global Constraints

- **全部用 `git mv`** 移动跟踪的文件（保历史）；未跟踪的（`build/`、`liburing/`、`kvstore` 二进制若未跟踪）用 `mv`
- 子模块仅 NtyCo：移动后更新 `.gitmodules` path → `kvstore/NtyCo`，`git submodule sync` 后 `git submodule status` 正常
- 移动前后内容零改动（只动目录层级）；路径引用更新属于 Task 3
- kvstore 二进制在根为 `kvstore`（编译产物）；`kvstore.conf` 是默认配置
- 根 `docs/` 现状：引擎分析文档（aof/ebpf/kprobe/rdma/kvstore/save/memory/benchmark/replication/tech-roadmap/tests-guide + archive/examples/optimization-history）→ 移 `kvstore/docs/`；`docs/superpowers/`（项目级 spec/plan）→ 留根
- `ai-chat/docs/`（调用流程详解 + 图片 + sql + scaffolding + superpowers）→ 提升到根 `docs/`（superpowers 与根 `docs/superpowers` 合并）
- 8 个服务 + kvstore + 监控**当前运行中**；Task 3 回归会 stop/start（stop.sh 提升后从根执行，pid 文件在根 runtime/pids 能找到——因为 runtime/ 也被提升了）
- 提交进 9.1-kvstore 仓库 main；`.gitmodules`、`.gitignore`、`lib.sh`、`configs/kvstore-ai.conf` 等路径更新都要提交
- 移动后 git 会显示大量 rename，属预期

---

### Task 1: 搬移 1 —— kvstore 引擎 → `kvstore/`

**Files:** 大量（git mv 移动目录）

**Interfaces:**
- Produces: `kvstore/` 目录含完整引擎（含 NtyCo 子模块、Makefile、docs/）；根 `docs/` 只剩 superpowers/
- Consumes: 无

- [ ] **Step 1: 确认跟踪状态**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git ls-files | grep -E "^(src|include|Makefile|tools|tests|clients|benchmarks|artifacts|assets|testdata|third_party|kvstore|kvstore.conf|docs)/" | head -3
echo "--- 未跟踪的目录（用 mv）---"
git status --short | grep '^??' | head
```

Expected: 确认哪些用 `git mv`（跟踪）、哪些用 `mv`（未跟踪，如 `build/`、`liburing/`）。

- [ ] **Step 2: git mv 引擎目录**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
mkdir -p kvstore
git mv src include Makefile kvstore.conf tests tools clients benchmarks artifacts assets testdata third_party tmp_repl_test NtyCo kvstore/ 2>/dev/null
# 二进制 kvstore 若被跟踪 → git mv；若未跟踪 → mv
git ls-files --error-unmatch kvstore >/dev/null 2>&1 && git mv kvstore kvstore/kvstore || mv kvstore kvstore/kvstore
# 根目录残留的引擎文件（跟踪的）一并迁入：eBPF 配置/内核头/禁用脚本
git mv kvstore-ebpf.conf vmlinux.h disable kvstore/ 2>/dev/null
# 未跟踪的 eBPF 编译产物也迁入
mv kprobe_capture.bpf.o kvstore/ 2>/dev/null
# 未跟踪目录用 mv
mv build liburing kvstore/ 2>/dev/null
```
> 说明：`kvstore.aof.replstate`、`kvstore_transport.log` 是运行时状态（repl 状态/日志），**不迁入 kvstore/ 也不提交**，属临时产物。

- [ ] **Step 3: 引擎分析文档 → kvstore/docs**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
mkdir -p kvstore/docs
# 根 docs/ 除 superpowers 外的引擎文档移到 kvstore/docs/
git mv docs/* kvstore/docs/ 2>/dev/null
git mv docs/.??* kvstore/docs/ 2>/dev/null || true
# 把 superpowers 移回根 docs/
mkdir -p docs
git mv kvstore/docs/superpowers docs/superpowers 2>/dev/null
# 清理空目录
rmdir docs 2>/dev/null || true
```

- [ ] **Step 4: 更新 .gitmodules + 子模块同步**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
# .gitmodules: path = NtyCo → kvstore/NtyCo
sed -i 's#path = NtyCo#path = kvstore/NtyCo#' .gitmodules
git add .gitmodules
git submodule sync
git submodule status   # 应显示 kvstore/NtyCo (72ab5fd...)
```

- [ ] **Step 5: 验证构建**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore/kvstore
make 2>&1 | tail -3
ls -la kvstore/kvstore 2>/dev/null || ls -la ./kvstore 2>/dev/null
```

Expected: `make` 从 kvstore/ 构建通过（相对路径有效）；生成 `kvstore/kvstore` 二进制。

- [ ] **Step 6: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add -A
git commit -m "refactor(kvstore): 引擎整体迁入 kvstore/ 目录（含 NtyCo 子模块、docs、构建）"
```

---

### Task 2: 搬移 2 —— ai-chat/* → 根

**Files:** 大量（git mv 提升）

**Interfaces:**
- Consumes: Task 1 完成（根 docs/ 只剩 superpowers/）
- Produces: 所有 ai-chat 组件在根；`ai-chat/` 目录删除

- [ ] **Step 1: 提升 ai-chat 组件**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
# 服务目录 + 基础设施
for d in ai-chat-backend ai-chat-service tokenizer keywords-filter mock-openai-api openai-api-proxy ai-chat-web ai-chat-stack monitoring configs bin runtime; do
  [ -e "ai-chat/$d" ] && git mv "ai-chat/$d" "$d"
done
# 脚本提升
git mv ai-chat/start.sh ai-chat/stop.sh ai-chat/lib.sh . 2>/dev/null
```

- [ ] **Step 2: ai-chat/docs → 根 docs（合并 superpowers）**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
# 根 docs 现在只有 superpowers/
git mv ai-chat/docs/* docs/ 2>/dev/null
git mv ai-chat/docs/.* docs/ 2>/dev/null || true
# superpowers 合并：ai-chat/docs/superpowers 内容并入根 docs/superpowers
git mv docs/superpowers/* docs/superpowers/ 2>/dev/null  # 若冲突则逐个
# 清理
rmdir ai-chat/docs ai-chat 2>/dev/null || true
```

> 说明：若 `ai-chat/docs/superpowers/specs|plans` 与根 `docs/superpowers` 有同名文件冲突，逐个 `git mv` 合并（本项目根 superpowers 目前只有 reorg spec/plan，冲突面小）。

- [ ] **Step 3: 验证结构**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
echo "--- 根目录 ---"; ls -d */ | sort
echo "--- ai-chat 残留? ---"; ls -d ai-chat 2>/dev/null || echo "ai-chat/ 已删除"
echo "--- 子模块 ---"; git submodule status
echo "--- 未跟踪遗留 ---"; git status --short | grep '^??' | head
```

Expected: 根目录为 kvstore/ + 各服务 + docs/ + configs/bin/runtime；`ai-chat/` 不存在；NtyCo 子模块在 kvstore/。

- [ ] **Step 4: 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
git add -A
git commit -m "refactor(ai-chat): 组件全部提升到根，删除 ai-chat/ 嵌套（docs 合并 superpowers）"
```

---

### Task 3: 路径更新 + 根编排 + 文档引用 + 全量回归

**Files:**
- Modify: `lib.sh`、`configs/kvstore-ai.conf`、`.gitignore`（合并）、`README.md`、`统一项目说明.md`、`CLAUDE.md`、`docs/调用流程详解.md`
- Create: 根 `Makefile`（编排）

**Interfaces:**
- Consumes: Task 1/2 完成（新结构就位）
- Produces: 新结构可构建、可启动、可聊天

- [ ] **Step 1: 更新 lib.sh**

`lib.sh` 改为（BASE 现在是根，去掉 KVROOT）：
```bash
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 现在就是仓库根
LOG_DIR="$BASE/runtime/logs"
PID_DIR="$BASE/runtime/pids"
```
SERVICES 里 kvstore 行改为：
```bash
  "kvstore|5160|$BASE|./kvstore/kvstore configs/kvstore-ai.conf"
```
其余 `$KVROOT`/`$BASE/tokenizer` 等保持 `$BASE/...`（BASE 已是根，语义不变）。

- [ ] **Step 2: 更新 configs/kvstore-ai.conf**

`dump_path` / `aof_path`：
```bash
dump_path=runtime/kvstore-ai.dump
aof_path=runtime/kvstore-ai.aof
```

- [ ] **Step 3: 根 Makefile（新建）**

```makefile
.PHONY: all kvstore start stop clean
all: kvstore
kvstore:
	$(MAKE) -C kvstore
start:
	./start.sh
stop:
	./stop.sh
clean:
	$(MAKE) -C kvstore clean
```

- [ ] **Step 4: 合并 .gitignore**

根 `.gitignore` 保留 kvstore 的 + 追加 `ai-chat/.gitignore` 的内容（`bin/`、`runtime/`、`monitoring/grafana/`、`monitoring/prometheus/data/`、`monitoring/logs/`、`monitoring/pids/`、`artifacts/` 等），删除 `ai-chat/.gitignore`。

- [ ] **Step 5: 文档路径引用修正**

`README.md`、`统一项目说明.md`、`CLAUDE.md`、`docs/调用流程详解.md` 里所有 `ai-chat/` 前缀路径改到新结构（如 `ai-chat/configs/` → `configs/`，`ai-chat/start.sh` → `start.sh`，`ai-chat/ai-chat-backend/` → `ai-chat-backend/`）。

- [ ] **Step 6: 全量回归**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
# 停掉旧进程（stop.sh 已提升到根；若 pids 在 runtime/ 则能按 pid 停）
./stop.sh 2>&1 | tail -3 || true
fuser -k -TERM 5160/tcp 3002/tcp 50053/tcp 50054/tcp 8083/tcp 8084/tcp 50055/tcp 7080/tcp 2>/dev/null
sleep 3
# 新结构冷启动
./start.sh 2>&1 | tail -8
# 验证 8 端口
ss -tlnp | grep -cE ":3002|:50053|:50054|:50055|:7080|:8083|:8084|:5160" | xargs echo "服务数:"
# 聊天端到端（登录 → 问问题 → 真实回答）
curl -s -X POST http://localhost:7080/api/v1/sms/send/code -H 'Content-Type: application/json' -d '{"phone":"13800138000"}' >/dev/null; sleep 1
CODE=$(redis-cli -p 5160 -a 123456 GET sms_code:13800138000 2>/dev/null)
TOKEN=$(curl -s -X POST http://localhost:7080/api/v1/user/login -H 'Content-Type: application/json' -d "{\"user_name\":\"13800138000\",\"pwd\":\"$CODE\",\"type\":1}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST http://localhost:7080/api/chat-process -H "Authorization: $TOKEN" -H 'Content-Type: application/json' -d '{"prompt":"你好","options":{}}' | head -c 60; echo
# 监控
cd monitoring && ./start.sh 2>&1 | tail -2; ss -tln | grep -cE ":9090|:3000" | xargs echo "监控端口:"
```

Expected: 8 服务全起、首页/聊天正常（真实回答）、kvstore 用新路径、监控正常。

- [ ] **Step 7: 无残留引用 + 提交**

```bash
cd /home/pp/Desktop/ls_study/proj/9.1-kvstore
grep -rn "ai-chat/" --include='*.sh' --include='*.yaml' --include='*.md' --include='Makefile' --include='*.conf' . 2>/dev/null | grep -v "\.git/" | grep -v "node_modules" | head
# 应为空（或仅注释里的历史说明）
git add -A
git commit -m "refactor: 根编排 Makefile + lib.sh/kvstore-ai.conf/文档路径更新 + .gitignore 合并"
```

---

## 自审结论

- **Spec 覆盖**：搬移 1（引擎→kvstore/，含 NtyCo、docs）→Task 1；搬移 2（ai-chat→根，删 ai-chat/）→Task 2；路径更新（lib.sh、kvstore-ai.conf、根 Makefile、.gitignore、文档）+ 回归 →Task 3。
- **无占位符**：每步给出具体命令；git mv 与 mv（未跟踪）区分明确。
- **类型/命名一致**：目标结构 `kvstore/`、根各服务、`configs/`、`bin/`、`runtime/`、`docs/` 在三个任务一致；NtyCo → `kvstore/NtyCo`；kvstore 服务命令 `./kvstore/kvstore configs/kvstore-ai.conf`。
- **风险标注**：Task 3 会停掉运行中的 8 服务再冷启动（用户需知）；superpowers 合并可能有同名文件冲突需逐个处理；git mv 后大量 rename 属预期。
