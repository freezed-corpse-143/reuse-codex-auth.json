# Tests: Codex Proxy Translation

## 测试环境

```bash
# 终端 1: 启动代理
cd ~/projects/reuse-codex-auth.json
uv run codex-proxy

# 终端 2: 运行测试
set -e HTTPS_PROXY HTTP_PROXY https_proxy http_proxy
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=sk-any-value
```

## 1. 单元测试（pytest）

### 1.1 `anthropic_to_codex()` 消息格式转换

```python
# tests/test_translate.py

def test_plain_text():
    """纯文本消息 → 单 item"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        messages=[AnthropicMessage(role="user", content="Hello")]
    )
    body = anthropic_to_codex(req)
    assert body["input"] == [{"role": "user", "content": "Hello"}]

def test_tool_use_in_history():
    """assistant 消息中的 tool_use → tool_calls"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        messages=[AnthropicMessage(
            role="assistant",
            content=[
                {"type": "text", "text": "I'll check"},
                {"type": "tool_use", "id": "call_1", "name": "get_weather",
                 "input": {"city": "Beijing"}},
            ]
        )]
    )
    body = anthropic_to_codex(req)
    assert len(body["input"]) == 1
    assert "tool_calls" in body["input"][0]
    assert body["input"][0]["tool_calls"][0]["function"]["name"] == "get_weather"

def test_tool_result():
    """tool_result → tool role"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        messages=[AnthropicMessage(
            role="user",
            content=[
                {"type": "tool_result", "tool_use_id": "call_1",
                 "content": "22°C"},
            ]
        )]
    )
    body = anthropic_to_codex(req)
    assert body["input"][0]["role"] == "tool"
    assert body["input"][0]["tool_call_id"] == "call_1"

def test_thinking_ignored():
    """thinking 内容块被忽略"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        messages=[AnthropicMessage(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "I should...", "signature": "..."},
                {"type": "text", "text": "The answer is 42"},
            ]
        )]
    )
    body = anthropic_to_codex(req)
    assert len(body["input"]) == 1
    assert body["input"][0]["content"] == "The answer is 42"
    assert "I should" not in body["input"][0]["content"]

def test_multi_turn_tool_call():
    """多轮工具调用对话"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        messages=[
            AnthropicMessage(role="user", content="Weather in Beijing?"),
            AnthropicMessage(role="assistant", content=[
                {"type": "tool_use", "id": "call_1", "name": "get_weather",
                 "input": {"city": "Beijing"}},
            ]),
            AnthropicMessage(role="user", content=[
                {"type": "tool_result", "tool_use_id": "call_1", "content": "22°C"},
            ]),
            AnthropicMessage(role="assistant", content="It's 22°C"),
        ]
    )
    body = anthropic_to_codex(req)
    assert len(body["input"]) == 4
    assert body["input"][0]["role"] == "user"
    assert "tool_calls" in body["input"][1]
    assert body["input"][2]["role"] == "tool"
    assert body["input"][3]["role"] == "assistant"

def test_max_tokens_forwarded():
    """max_tokens → max_output_tokens"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[AnthropicMessage(role="user", content="hi")]
    )
    body = anthropic_to_codex(req)
    assert body["max_output_tokens"] == 4096
```

### 1.2 `CodexStreamTranslator` SSE 事件转换

```python
def test_text_block():
    """正常文本 SSE → Anthropic stream events"""
    translator = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    events = translator.process_event("response.created", {})
    assert any(e[0] == "message_start" for e in events)
    
    events = translator.process_event("response.output_item.added",
        {"item": {"type": "message"}})
    assert any(e[0] == "content_block_start" for e in events)
    
    events = translator.process_event("response.output_text.delta",
        {"delta": "Hello", "content_index": 0})
    assert any(e[0] == "content_block_delta" for e in events)

def test_function_call_to_tool_use():
    """function_call → tool_use content block TODO"""
    translator = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    events = translator.process_event("response.output_item.added", {
        "item": {
            "type": "function_call",
            "id": "fc_123",
            "function_call": {
                "name": "get_weather",
                "arguments": '{"city": "Beijing"}',
            }
        }
    })
    # TODO: after translating function_call to tool_use
    # assert any(e[0] == "content_block_start" for e in events)
    # cb = json.loads([e[1] for e in events if e[0]=="content_block_start"][0])
    # assert cb["content_block"]["type"] == "tool_use"

def test_completed_with_tool_use():
    """带 tool 调用的 completed → stop_reason: tool_use TODO"""
    pass

def test_completed_no_tool():
    """纯文本 completed → stop_reason: end_turn"""
    translator = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    translator._message_started = True
    translator._content_block_started = True
    events = translator.process_event("response.completed", {
        "response": {"status": "completed", "usage": {"output_tokens": 10}}
    })
    assert any(e[0] == "message_stop" for e in events)
    md = json.loads([e[1] for e in events if e[0]=="message_delta"][0])
    assert md["delta"]["stop_reason"] == "end_turn"
```

## 2. 集成测试（curl）

### 2.1 纯对话

```bash
# 基本对话
curl -s http://127.0.0.1:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-any-value" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hi"}]
  }'
```

### 2.2 流式 SSE（stream=true）

```bash
curl -s -N http://127.0.0.1:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-any-value" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 100,
    "stream": true,
    "messages": [{"role": "user", "content": "Count to 3"}]
  }'
```

**验证**：
- 事件顺序正确：message_start → content_block_start → content_block_delta* → content_block_stop → message_delta → message_stop
- 每条 data 行是合法 JSON
- 模型名与请求匹配

### 2.3 工具调用（手动构造）

```bash
# 包含 tool_use 历史的消息
curl -s http://127.0.0.1:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-any-value" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 100,
    "messages": [
      {"role": "user", "content": "What is the weather in Beijing?"}
    ],
    "tools": [{
      "name": "get_weather",
      "description": "Get weather for a city",
      "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }]
  }'
```

## 3. Claude Code 交互测试（tmux）

### 3.1 基本交互

```bash
tmux new-session -d -s claude-test bash
tmux send-keys -t claude-test \
  "env -u HTTPS_PROXY -u HTTP_PROXY \
     ANTHROPIC_BASE_URL=http://127.0.0.1:8080 \
     ANTHROPIC_API_KEY=sk-any-value \
     /workspace/.bun/bin/claude" Enter
sleep 10
tmux send-keys -t claude-test "Say hello in exactly 3 words" Enter
sleep 15
tmux capture-pane -t claude-test -p -e -S -20
```

**验证**：
- 看到 `● Hello World` 或类似的 `●` 响应
- 有 `✻ Brewed for Xs` 计时行
- 提示符 `❯` 回到输入状态

### 3.2 工具调用测试

```bash
tmux send-keys -t claude-test \
  "Run this command and tell me the result: echo 'tool_test_ok'" Enter
sleep 30
tmux capture-pane -t claude-test -p -e -S -30
```

**验证**：
- Claude 执行 bash 命令
- 输出中看到 `tool_test_ok`
- 没有卡死或无限重试

### 3.3 多轮对话

```bash
tmux send-keys -t claude-test "My name is Alice" Enter
sleep 15
tmux send-keys -t claude-test "What is my name?" Enter
sleep 15
tmux capture-pane -t claude-test -p -e -S -20
```

**验证**：
- 第二轮回答中显示 "Alice"
- 上下文保持

## 4. 回归测试

### 4.1 环境变量冲突

```bash
# 有 HTTPS_PROXY 时 — 应能看到错误或正确路由到代理
export HTTPS_PROXY=http://172.21.160.1:7890
claude --bare --print "hi"
# 应显示 "Invalid API key" 或连接错误，而不是 hang
```

### 4.2 模型回退

```bash
# 所有模型名都应正常工作（映射到 gpt-5.5）
for model in claude-sonnet-4-6 claude-opus-4-8 claude-3-5-sonnet-latest; do
  echo "Testing: $model"
  curl -s --max-time 15 http://127.0.0.1:8080/v1/messages \
    -H "Content-Type: application/json" \
    -H "x-api-key: sk-any-value" \
    -d "{\"model\":\"$model\",\"max_tokens\":30,\"messages\":[{\"role\":\"user\",\"content\":\"say hi\"}]}" \
    | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['content'][0]['text'])"
done
```

## 5. 验证清单

| 场景 | 方法 | 预期结果 | 状态 |
|---|---|---|---|
| 纯文本 → API | curl POST | 返回 text | ⬜ |
| 流式 SSE | curl -N | 完整事件序列 | ⬜ |
| 纯对话交互 | tmux claude | 显示 `●` 回复 | ✅ 已验证 |
| 工具调用 | tmux claude | 执行工具并返回 | ⬜ |
| 多轮对话 | tmux claude | 上下文保持 | ⬜ |
| 中文输入 | tmux claude | 正确回复 | ✅ 已验证 |
| 模型映射 | curl (多个 model) | 全部返回 | ⬜ |
| 错误处理 | curl 非法请求 | Anthropic 格式错误 | ⬜ |
