# ECHO-CHAT（ai 助手）

零声教学 AI 助手微服务版。独立仓库，kvstore 以 submodule 引入（`kvstore/`），用法与 kvstore 自身引用 NtyCo 一致。

## 仓库结构

```
ECHO-CHAT/
├── ai-chat-backend/     ← Go HTTP 网关（页面 + /api/chat-process）
├── ai-chat-service/     ← Go gRPC 核心服务（对话编排 + 语义缓存）
├── keywords-filter/     ← 敏感词/关键词过滤
├── openai-api-proxy/    ← DeepSeek 反向代理（key 走环境变量）
├── mock-openai-api/     ← 离线 mock（无 key 调试用）
├── tokenizer/           ← Python 嵌入/重排/token 计数
├── ai-chat-web/         ← Vue3 前端
├── kvstore/             ← submodule → robotlover-1/pocket-kv（自研 Redis）
└── start.sh / stop.sh   ← 一键启停
```

## 快速开始

```bash
git clone --recurse-submodules https://github.com/robotlover-1/ECHO-CHAT.git
cd ECHO-CHAT
make kvstore                    # 编译 submodule 里的 kvstore
DEEPSEEK_API_KEY=sk-xxx ./start.sh   # key 走环境变量，勿提交 git
```

## 功能说明

- **免密登录**：首次访问自动以 `device_id` 注册并分配额度，无短信验证码。
- **明文 KV 存储**：问题-回答以 `<原始问题>` → `<原始回答>` 存进 kvstore（key/value 均为原文，不做 hash，`redis-cli -p 5160 GET <问题>` 可直接读）；语义检索的向量索引在 `semcache:<原始问题>`。
- **公有大模型**：DeepSeek（OpenAI 兼容），模型 `deepseek-v4-flash`。
- **来源标注**：每条回答标注「公有大模型」/「缓存命中」。
- **tokens 统计**：页面按会话累计「总消耗」与「节省」tokens（缓存命中不计费、记为节省）。

## 相关仓库

- 存储：[pocket-kv](https://github.com/robotlover-1/pocket-kv)（kvstore，submodule 引入）
- 助手：本仓库 [ECHO-CHAT](https://github.com/robotlover-1/ECHO-CHAT)
