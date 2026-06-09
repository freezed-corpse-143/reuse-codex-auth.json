# Codex Proxy

复用 Codex CLI 登录后的 `auth.json`，暴露 Anthropic Messages API 格式接口，
让 **Claude Code** 能通过 ChatGPT Pro/Plus 账户调用 OpenAI 模型。

## 原理

```
Claude Code → Anthropic Messages API → Codex Proxy → ChatGPT Backend (chatgpt.com)
                                                           ↑
                                                     auth.json (access_token)
```

- 读取 `~/.codex/auth.json` 或当前目录下的 `auth.json`
- 自动检测 token 过期并刷新
- 将 Anthropic 格式翻译为 ChatGPT Backend Responses API 格式
- 支持流式（SSE）和非流式响应

## 快速开始

```bash
# 1. 确保有 auth.json（来自 codex login）
ls auth.json

# 2. 启动 proxy
uv run codex-proxy

# 3. 在 Claude Code 中设置环境变量
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-any-value

# 4. 现在 Claude Code 会通过 proxy 调用 OpenAI 模型
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CODEX_AUTH_PATH` | `auth.json` | auth.json 路径 |
| `CODEX_BASE_URL` | `https://chatgpt.com/backend-api/codex` | Codex API 地址 |
| `PROXY_HOST` | `127.0.0.1` | 监听地址 |
| `PROXY_PORT` | `8080` | 监听端口 |
| `PROXY_URL` | 无 | 出站代理地址（支持 http/socks5）|
| `HTTP_PROXY` | 无 | 同 PROXY_URL（httpx 自动识别） |
| `HTTPS_PROXY` | 无 | 同 PROXY_URL（httpx 自动识别） |

## 模型映射

| Anthropic 模型名 | Codex 模型名 |
|---|---|
| `claude-sonnet-4-20250514` | `gpt-5.5` |
| `claude-3-5-sonnet-20241022` | `gpt-5.5` |
| `claude-3-5-haiku-latest` | `gpt-5.4-mini` |
| `claude-opus-4-20250514` | `gpt-5.5` |

## API 接口

| 路径 | 格式 | 说明 |
|------|------|------|
| `GET /health` | — | 健康检查 |
| `GET /v1/models` | Anthropic | 列出可用模型 |
| `POST /v1/messages` | Anthropic | Anthropic Messages API 兼容接口（主入口） |
| `POST /v1/chat/completions` | OpenAI | OpenAI Chat Completions 兼容接口 |

## 变更 (v0.3.0)

- **原子写入** — auth.json 刷新使用临时文件 + rename，防止写操作中断导致文件损坏
- **格式兼容** — 支持 `anthropic-version` 请求头穿透；错误响应使用 Anthropic Messages API 格式
- **Tool 支持** — 正确处理 `tool_use`/`tool_result` content blocks，不再跳过或告警
- **Chat Completions** — 新增 `/v1/chat/completions` 端点，支持 OpenAI 格式的流式和非流式请求
- **入口修复** — `uv run codex-proxy` 可直接启动
