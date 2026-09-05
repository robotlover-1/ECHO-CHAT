# ECHO-CHAT 公网部署（deploy/）

拓扑与主方案：docs/deploy/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md
落地 spec：docs/superpowers/specs/2026-09-05-echo-chat-tunnel-vm-public-deployment-design.md

## 双节点
- 应用 VM：ECHO-CHAT 主 compose（127.0.0.1:7080）+ deploy/app frpc 栈。
- 入口 VM：deploy/edge frps + nginx(80/443)。
- 变量对照：见 deploy/{app,edge}/.env.example（common: PUBLIC_DOMAIN/FRP_AUTH_TOKEN 两端一致）。
- FRP token 用: openssl rand -hex 32（放 .env 的 FRP_AUTH_TOKEN）。
- 安全组：22(限管理IP)/80/443/39000(限应用VM出口)；39001/7500/7080 不对外。

## 一键
- 应用: cp deploy/app/.env.example deploy/app/.env && sudo bash deploy/scripts/deploy-app.sh
- 入口: cp deploy/edge/.env.example deploy/edge/.env && sudo bash deploy/scripts/deploy-edge.sh
- 分层验收: bash deploy/scripts/smoke-test.sh app|edge|e2e

## MySQL（应用 VM，外部/宿主机）
CREATE DATABASE ai_chat CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'echo_chat'@'%' IDENTIFIED BY '<STRONG_PASSWORD>';
GRANT ALL PRIVILEGES ON ai_chat.* TO 'echo_chat'@'%';
FLUSH PRIVILEGES;
# MYSQL_DSN=echo_chat:<STRONG_PASSWORD>@tcp(host.docker.internal:3306)/ai_chat?charset=utf8mb4&parseTime=true

## 镜像注意
- frps/frpc 默认 snowdreamtech:0.62.1；目标机 docker image inspect 核对 ENTRYPOINT；
  若换镜像保持 frps/frpc 同版本；固定版本后再固定 digest(P1)。

## 证书续期（edge）
- /etc/letsencrypt/renewal-hooks/deploy/reload-echo-nginx.sh:
    #!/usr/bin/env bash
    docker compose -f /opt/echo-chat-edge/compose.yaml exec -T nginx nginx -s reload
- sudo certbot renew --dry-run 验证。

## 交付边界
- 本仓库改动为静态验证（go build/test、nginx -t、yaml、bash -n）。
- 公网 TLS/登录/流式/断线/回滚验收在目标 VM 执行（smoke-test.sh + 主方案 §9/§13）。
- 单机演示(主方案 §3.2)：入口与应用同机时可让 frpc serverAddr 指向 127.0.0.1。

## Secret 纪律
- .env 与 render 产物已 gitignore；提交前 bash deploy/scripts/scan-secrets.sh。
- docker/config/*.yaml 需先 render-config app（见 docker/README.md）。
