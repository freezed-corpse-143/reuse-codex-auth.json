# Plan: Codex Proxy Translation Layer Rewrite

## Architecture

```
                        ┌──────────────────────────────┐
                        │     Anthropic Messages API    │
                        │  (Claude Code / SDK clients)  │
                        └──────────┬───┬───────────────┘
                                   │   │
                           requests│   │responses
                                   │   │
                        ┌─────────▼───▼───────────────┐
                        │      proxy.py (FastAPI)      │
                        │                              │
                        │  ┌─────────────────────────┐ │
                        │  │  Request Translator     │ │
                        │  │  anthropic_to_codex()   │ │
                        │  │  + content block parser │ │
                        │  └───────────┬─────────────┘ │
                        │              │               │
                        │  ┌───────────▼─────────────┐ │
                        │  │  HTTP Client             │ │
                        │  │  → POST /codex/responses │ │
                        │  └───────────┬─────────────┘ │
                        │              │               │
                        │  ┌───────────▼─────────────┐ │
                        │  │  Response Translator    │ │
                        │  │  CodexStreamTranslator  │ │
                        │  │  + SSE event mapper     │ │
                        │  └─────────────────────────┘ │
                        └──────────────────────────────┘
                                   │
                                   │ HTTP (via proxy)
                                   │
                        ┌─────────▼───────────────────┐
                        │  chatgpt.com/backend-api/    │
                        │  codex/responses             │
                        │  (OpenAI Codex Backend)      │
                        └──────────────────────────────┘
```

## Design Decisions

### 1. 内容块结构化流水线

不再用 `_extract_text()` 压平一切，而是拆成三个阶段的流水线：

```
Anthropic message content blocks
  │
  ├── text         → Codex role+content (string)
  ├── tool_use     → Codex input item: tool_call
  ├── tool_result  → Codex input item: tool_result
  └── thinking     → 忽略（Codex 无等效项），只保留后续 text
```

#### Codex Input 格式

Codex Responses API 的 `input` 接受结构化 item 数组：

```json
{
  "input": [
    {"role": "user", "content": "What's the weather?"},
    {"role": "assistant", "tool_calls": [
      {"id": "call_1", "type": "function",
       "function": {"name": "get_weather", "arguments": "{\"city\":\"Beijing\"}"}}
    ]},
    {"role": "tool", "tool_call_id": "call_1", "content": "22°C"},
    {"role": "assistant", "content": "It's 22°C in Beijing."}
  ]
}
```

### 2. 输入翻译（anthropic_to_codex）

改为：

```python
def anthropic_to_codex(anth_req: AnthropicRequest) -> dict[str, Any]:
    items = []
    for msg in anth_req.messages:
        items.extend(convert_message(msg))
    ...
```

`convert_message()` 根据 role 和 content block 类型决定输出：

| Anthropic Message | 输出 Codex item |
|---|---|
| role=user, type=text | `{role: "user", content: text}` |
| role=assistant, type=text | `{role: "assistant", content: text}` |
| role=assistant, type=tool_use | `{role: "assistant", tool_calls: [...]}` |
| role=user, type=tool_result | `{role: "tool", tool_call_id: ..., content: ...}` |
| role=assistant, type=thinking | 忽略 |
| role=assistant, type=redacted_thinking | 忽略 |

### 3. 输出翻译（CodexStreamTranslator）

新增 SSE 事件处理：

| Codex SSE Event | Action |
|---|---|
| `response.output_item.added` + type=`function_call` | 发出 `content_block_start` (type: `tool_use`) |
| `response.output_text.delta` | 现有，不变 |
| `response.completed` | 检查是否有未关闭的 tool_use block |
| `response.content_part.added` | 如果 type=`reasoning`，忽略或转为 text |

SSE 事件中 function_call 到 tool_use 的映射：

```python
def _on_function_call(self, data):
    item = data.get("item", data)
    fc = item.get("function_call", item.get("function", {}))
    return [
        ("content_block_start", json.dumps({
            "type": "content_block_start",
            "index": self._next_index(),
            "content_block": {
                "type": "tool_use",
                "id": item.get("id", ""),
                "name": fc.get("name", ""),
                "input": json.loads(fc.get("arguments", "{}")),
            },
        })),
        ("content_block_stop", json.dumps({
            "type": "content_block_stop",
            "index": self._current_index(),
        })),
    ]
```

### 4. 模型映射扩展

在 `config.json` 中添加 Claude v2 新模型 ID：

```json
{
  "model_mapping": {
    "claude-sonnet-4-6": "gpt-5.5",
    "claude-opus-4-8": "gpt-5.5",
    "claude-sonnet-4-20250514": "gpt-5.5",
    "claude-opus-4-20250514": "gpt-5.5"
  }
}
```

### 5. `max_tokens` 转发

```python
if anth_req.max_tokens is not None:
    body["max_output_tokens"] = anth_req.max_tokens
```

### 6. 推理/思考内容

Codex Responses API 没有 reasoning/thinking 的等效参数，因此：

- 请求方向：忽略 `thinking` 和 `thinking_config` 参数
- 响应方向：如果 Codex 返回 `reasoning` 类型的内容，忽略或转为 text

## 非目标

- Image input（Codex 不支持 image）
- Streaming 协议变更（仍用 SSE）
- 多模态内容（Codex 无等效）
- token 计数的精确映射（非流式响应中的 usage 字段可以近似）
