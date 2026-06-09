# Issues: Codex Proxy ─ Anthropic ↔ OpenAI Codex Translation

## 1. Tool Call Round-Trip (工具调用往返断裂)

### 请求方向 (Claude → Proxy → Codex)

`_extract_text()` 将所有消息内容块压为纯文本：

- `tool_use` → 转成 `[tool_use: name args={...}]` 字符串
- `tool_result` → 提取其中 text 部分合并

**结果**：Codex backend 看不到工具调用历史，也无法区分文本和工具输出。

### 响应方向 (Codex → Proxy → Claude)

`CodexStreamTranslator` 只处理 `type=message` 的 output_item：

- `response.output_item.added` 如果 `type=function_call` → **完全忽略**
- `response.content_part.added` → **空操作**

**结果**：Claude 永远收不到工具调用指令，交互卡死。

### 影响

任何需要工具的场景（bash、读文件、git 等）全部挂起，表现为 `✻ Brewed for 24s` 无响应。

---

## 2. 无推理/思考内容

`AnthropicRequest` 模型未定义 `thinking` 或 `thinking_config` 字段，Claude v2 发送的 thinking 参数被 Pydantic 静默丢弃。

Codex Responses API 可能没有等效的 reasoning 参数。

**结果**：纯对话回复正常，但无推理过程展示。

---

## 3. `max_tokens` 未转发

`AnthropicRequest` 定义了 `max_tokens: int | None = None`，但 `anthropic_to_codex()` 中**没有将 `max_tokens` 转为 `max_output_tokens`** 写入 Codex 请求体。

**结果**：Codex backend 使用默认输出 token 限制（可能很大），但对短对话无实际影响。

---

## 4. 输入消息结构化丢失

`anthropic_to_codex()` 中只调 `_extract_text()` 把消息转为纯文本，忽略：

- 多轮对话中 `tool_use` / `tool_result` 的结构化关系
- `role: "assistant"` 消息中 content 的多个 block 类型
- 需要映射为 Codex `input` 中的 `role: "assistant"` + `content` / `tool_call` / `tool_result`

---

## 5. 模型映射不完整

`config.json` 的 `model_mapping` 缺少 Claude v2 使用的模型 ID：

| Claude v2 发送 | config.json 中有？ | 映射结果 |
|---|---|---|
| `claude-sonnet-4-6` | ❌ 无 | → `gpt-5.5` (默认) |
| `claude-opus-4-8` | ❌ 无 | → `gpt-5.5` (默认) |
| `claude-sonnet-4-20250514` | ✅ 有 | → `gpt-5.5` |
| `claude-opus-4-7` | ❌ 无 | → `gpt-5.5` (默认) |

实际所有模型都落到 `gpt-5.5`，但缺少显式映射会产生歧义。

---

## 6. `HTTPS_PROXY` 环境变量冲突（已解决）

**状态：已修复（文档中记录）**

Claude Code v2 使用 `HTTPS_PROXY` 连接 `api.anthropic.com` 而非本地代理。需要在启动 claude 的终端中 `unset HTTPS_PROXY`。
