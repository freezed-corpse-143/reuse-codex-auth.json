# Codex Proxy

Reuse the `auth.json` produced by `codex login` to expose an Anthropic Messages API endpoint,
letting **Claude Code** call OpenAI models through a ChatGPT Pro/Plus subscription.

```
Claude Code → POST /v1/messages (Anthropic format) → Codex Proxy → POST /responses (ChatGPT Backend) → chatgpt.com
                                                                        ↑
                                                                  auth.json (access_token)
```

## Quick Start

```bash
# 1. Make sure auth.json exists (from codex login)
ls auth.json

# 2. Start the proxy
uv run codex-proxy

# 3. In another terminal, point Claude Code at the proxy
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-any-value

# 4. Claude Code now routes through the proxy
claude --print "your prompt"
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CODEX_AUTH_PATH` | `auth.json` | Path to the Codex auth file |
| `CODEX_BASE_URL` | `https://chatgpt.com/backend-api/codex` | Codex API base URL |
| `PROXY_HOST` | `127.0.0.1` | Listen address |
| `PROXY_PORT` | `8080` | Listen port |
| `PROXY_URL` | none | Upstream proxy (http/socks5) |
| `HTTP_PROXY` | none | Same as PROXY_URL (httpx auto-detect) |
| `HTTPS_PROXY` | none | Same as PROXY_URL (httpx auto-detect) |

### Model Mapping (`config.json`)

Model mappings are loaded from `config.json` at startup. If the file is missing or a field is absent, built-in defaults are used.

```json
{
  "model_mapping": {
    "claude-sonnet-4-20250514": "gpt-5.5",
    "claude-3-5-sonnet-latest": "gpt-5.5",
    "claude-opus-4-7": "gpt-5.5",
    "claude-3-5-haiku-latest": "gpt-5.4-mini"
  },
  "oai_model_mapping": {
    "gpt-4o": "gpt-5.5",
    "gpt-4o-mini": "gpt-5.4-mini"
  },
  "responses_model_mapping": {
    "gpt-4o": "gpt-5.5",
    "gpt-4o-mini": "gpt-5.4-mini",
    "o3": "gpt-5.5",
    "o4-mini": "gpt-5.4-mini"
  },
  "reverse_model_mapping": {
    "gpt-5.5": "claude-sonnet-4-20250514",
    "gpt-5.4-mini": "claude-3-5-haiku-latest"
  },
  "default_anthropic_model": "claude-sonnet-4-20250514",
  "default_codex_model": "gpt-5.5"
}
```

- `model_mapping` — Anthropic model name → Codex model name (used for `/v1/messages`).
- `oai_model_mapping` — OpenAI model name → Codex model name (used for `/v1/chat/completions`).
- `responses_model_mapping` — OpenAI model name → Codex model name (used for `/v1/responses`).
- `reverse_model_mapping` — Codex model name → Anthropic model name (used in Anthropic responses).
- Any model not in a map falls back to `default_codex_model` / `default_anthropic_model`.

Edit `config.json` freely — missing keys merge with built-in defaults, so only specify overrides.

## API Endpoints

| Path | Format | Description |
|---|---|---|
| `GET /` | — | Connectivity check (used by Claude Code) |
| `GET /health` | — | Health check with auth status |
| `GET /v1/models` | Anthropic | List available models (deduplicated) |
| `POST /v1/messages` | Anthropic | Main entry — Anthropic Messages API proxy |
| `POST /v1/chat/completions` | OpenAI | OpenAI Chat Completions proxy |
| `POST /v1/responses` | OpenAI | OpenAI Responses API proxy |

## Features

- **Token auto-refresh** — detects expired access tokens and refreshes via OAuth
- **Streaming** — SSE-to-SSE translation with minimal latency
- **Atomic writes** — auth.json updates use tmp+rename to prevent corruption
- **Error format** — all errors return Anthropic-compatible `{"type":"error","error":{...}}` responses
- **Tool calls** — `tool_use`/`tool_result` content blocks are passed through as text context
- **anthropic-version header** — forwarded from request to response

## Changelog (v0.3.0)

- Configurable model mapping via `config.json` instead of hardcoded dict
- `POST /v1/responses` — OpenAI Responses API proxy
- `responses_model_mapping` key in `config.json`
- Atomic tmp+rename writes for auth.json safety
- `anthropic-version` header passthrough
- Error responses in Anthropic `{"type":"error"}` format
- Proper `tool_use`/`tool_result` content block handling
- `POST /v1/chat/completions` — OpenAI Chat Completions proxy
- Root `HEAD /` route for Claude Code connectivity check
- Fixed `metadata` and `user` unsupported parameter errors
- `uv run codex-proxy` entry point
