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

# 3. In another terminal, point Claude Code at the proxy.
#    IMPORTANT: Unset HTTPS_PROXY/HTTP_PROXY — Claude Code v2 uses them
#    to reach api.anthropic.com directly, bypassing the local proxy.
#    The proxy itself will still use these vars for outbound traffic.
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-any-value
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy

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
- **Structured input** — `tool_use`/`tool_result`/`thinking` content blocks are properly decoded and
  forwarded to the Codex backend in their native format (not flattened to text)
- **Model passthrough** — response model ID matches the original request model (fixes Claude Code v2
  interactive display)
- **anthropic-version header** — forwarded from request to response

## Limitations

### Tool calls

The proxy forwards tool definitions (`tools`/`tool_choice`) to the Codex backend and translates
Codex `function_call` output events back into Anthropic `tool_use` content blocks. However, the
Codex Responses API may reject or fail on certain tool configurations. Tool call reliability
depends on the backend.

### Reasoning / Thinking

The Codex Responses API does not support Anthropic's `thinking`/`thinking_config` parameters.
Thinking content blocks in the input are silently ignored; any `reasoning` output from Codex is
skipped.

### `max_tokens`

Codex Responses API does not support `max_output_tokens`. The `max_tokens` field is accepted but
ignored.

### `temperature` / `top_p`

Codex Responses API does not support `temperature` or `top_p`. These fields are accepted but
ignored.

## Troubleshooting

### Claude Code v2 hangs or ignores the proxy

**Symptom**: Claude Code returns "Invalid API key" or hangs without output when `ANTHROPIC_BASE_URL` is set.

**Cause**: `HTTPS_PROXY` or `HTTP_PROXY` is set. Claude Code v2 uses these env vars to connect
to `api.anthropic.com` directly instead of the local proxy.

**Fix**: Unset proxy env vars in the Claude Code terminal:

```bash
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-any-value
claude --print "your prompt"
```

The proxy process itself retains `HTTPS_PROXY` for its own outbound traffic to chatgpt.com.
If both processes share a terminal, start the proxy first, then run claude with `env -u`:

```bash
env -u HTTPS_PROXY -u HTTP_PROXY \
  ANTHROPIC_BASE_URL=http://127.0.0.1:8080 \
  ANTHROPIC_API_KEY=sk-any-value \
  claude --print "your prompt"
```

## Changelog

### v0.4.0

- **Structured message conversion** — `tool_use`/`tool_result`/`thinking` content blocks are now
  properly converted to Codex input format (tool_calls / tool role) instead of flattened text
- **Tool call SSE translation** — Codex `function_call` output items are translated to Anthropic
  `tool_use` content blocks with correct `stop_reason: "tool_use"`
- **Model passthrough** — SSE response model ID now matches the original request model, fixing
  Claude Code v2 interactive display
- **Config** — added `claude-sonnet-4-6` and `claude-opus-4-8` to model mapping
- **Limitations documented** — Codex API constraints on `max_tokens`, `temperature`, `thinking`,
  and tool call reliability are now explicitly listed
- **Unit tests** — 18 tests covering message conversion, request translation, and SSE event handling

### v0.3.1

- Documented `HTTPS_PROXY`/`HTTP_PROXY` conflict with Claude Code v2 in Quick Start
- Added Troubleshooting section with env var conflict resolution

### v0.3.0

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