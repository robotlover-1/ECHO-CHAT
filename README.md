# ECHO-CHAT（ai 助手）

零声教学 AI 助手微服务版。独立仓库，kvstore 以 submodule 引入（`kvstore/`），用法与 kvstore 自身引用 NtyCo 一致。

## 仓库结构

```text
ECHO-CHAT/
├── ai-chat-backend/     ← Go HTTP 网关（页面 + /api/chat-process）
├── ai-chat-service/     ← Go gRPC 核心服务（对话编排 + 语义缓存）
├── keywords-filter/     ← 敏感词/关键词过滤
├── openai-api-proxy/    ← DeepSeek 反向代理（key 走环境变量）
├── mock-openai-api/     ← 离线 mock（无 key 调试用）
├── tokenizer/           ← token 计数（tiktoken，仅计 token）
├── semantic/            ← 语义检索独立服务(:3003)：e5 嵌入 + parse + decision + 安全指纹
├── ai-chat-web/         ← Vue3 前端
├── kvstore/             ← submodule → robotlover-1/pocket-kv（自研 Redis）
└── start.sh / stop.sh   ← 一键启停
```

> 语义检索依赖大模型文件 `semantic/models/e5s-v1/model.onnx`（multilingual-e5-small，ONNX/INT8）。该目录 **gitignored**，不在仓库内；缺失时 semantic 能启动但语义缓存不可用（`start.sh` 会打印提示、Go 优雅 miss，聊天不受影响）。

## 快速开始

```bash
git clone --recurse-submodules https://github.com/robotlover-1/ECHO-CHAT.git
cd ECHO-CHAT

# kvstore(C)与前端 dist 若缺失，start.sh 会自动 make / pnpm 重建；也可手动：
#   ( cd kvstore/kvstore && make )          # 子模块 VSEARCH 前缀参数版
# 语义模型（可选，语义缓存需要；一次即可）：
bash semantic/tools/fetch_model.sh          # 从 GitHub Release 下载+sha256 校验（~79MB）
#   或 ECHO_FETCH_MODEL=1 ./start.sh 让 start.sh 缺模型时自动下载。

DEEPSEEK_API_KEY=sk-xxx ./start.sh   # key 走环境变量，勿提交 git

# 另一种环境方案（各节点免装依赖/模型）：全栈 Docker —— 见 docker/README.md
#   cd docker && DEEPSEEK_API_KEY=sk-xxx docker compose up -d --build
```

前置依赖：Go、make/gcc（kvstore）、pnpm/node（前端，仅首次）、Python 3.8+（host 需 `pip install -r semantic/requirements.txt tokenizer/requirements.txt`；nuxt/jieba 等按既有说明）。

## 功能说明

- **免密登录**：首次访问自动以 `device_id` 注册并分配额度，无短信验证码。
- **明文 KV 存储**：问题-回答以 `<原始问题>` → `<原始回答>` 存进 kvstore（key/value 均为原文，不做 hash，`redis-cli -p 5160 GET <问题>` 可直接读）；语义检索向量索引在版本化命名空间 `semd:e5s:v1:<原始问题>`（e5 384 维），另有安全指纹 `semfp:v1:<fp>` 精确命中。
- **公有大模型**：DeepSeek（OpenAI 兼容），模型 `deepseek-v4-flash`。
- **来源标注**：每条回答标注「公有大模型」/「缓存命中」。
- **tokens 统计**：页面按会话累计「总消耗」与「节省」tokens（缓存命中不计费、记为节省）。

## 相关仓库

- 存储：[pocket-kv](https://github.com/robotlover-1/pocket-kv)（kvstore，submodule 引入）
- 助手：本仓库 [ECHO-CHAT](https://github.com/robotlover-1/ECHO-CHAT)
