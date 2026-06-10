"""单元测试：Anthropic ↔ Codex 格式转换。

input 方向：Codex Responses API 只支持纯文本 input，
tool_use/tool_result 被嵌入为文本描述。
output 方向：Codex function_call → Anthropic tool_use SSE。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import (
    AnthropicRequest,
    AnthropicMessage,
    CodexStreamTranslator,
    _convert_message,
    anthropic_to_codex,
)


# ── _convert_message (input 方向) ──────────────────────────────────────
# Codex Responses API input 只支持纯文本，tool_use/tool_result 被压平

def test_plain_text_string():
    msg = AnthropicMessage(role="user", content="Hello")
    assert _convert_message(msg) == [{"role": "user", "content": "Hello"}]


def test_plain_text_block():
    msg = AnthropicMessage(role="user", content=[{"type": "text", "text": "Hi"}])
    assert _convert_message(msg) == [{"role": "user", "content": "Hi"}]


def test_multiple_text_blocks():
    msg = AnthropicMessage(role="user", content=[
        {"type": "text", "text": "Hello, "},
        {"type": "text", "text": "world!"},
    ])
    items = _convert_message(msg)
    assert items[0]["content"] == "Hello, \nworld!"


def test_tool_use_flattened():
    """tool_use → 嵌入为文本"""
    msg = AnthropicMessage(role="assistant", content=[
        {"type": "text", "text": "I'll check."},
        {"type": "tool_use", "id": "call_abc", "name": "get_weather",
         "input": {"city": "Beijing"}},
    ])
    items = _convert_message(msg)
    assert len(items) == 1
    assert items[0]["role"] == "assistant"
    assert "get_weather" in items[0]["content"]
    assert "Beijing" in items[0]["content"]


def test_tool_use_flattened_no_text():
    """纯 tool_use（无前置文本）"""
    msg = AnthropicMessage(role="assistant", content=[
        {"type": "tool_use", "id": "call_1", "name": "search",
         "input": {"q": "weather"}},
    ])
    items = _convert_message(msg)
    assert len(items) == 1
    assert "[tool_use:" in items[0]["content"]


def test_tool_result_flattened():
    """tool_result → 嵌入为文本"""
    msg = AnthropicMessage(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_abc", "content": "22°C"},
    ])
    items = _convert_message(msg)
    assert len(items) == 1
    assert items[0]["role"] == "user"
    assert "call_abc" in items[0]["content"]
    assert "22°C" in items[0]["content"]


def test_tool_result_list_content():
    msg = AnthropicMessage(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_1",
         "content": [{"type": "text", "text": "Result: 42"}]},
    ])
    items = _convert_message(msg)
    assert "Result: 42" in items[0]["content"]


def test_thinking_ignored():
    msg = AnthropicMessage(role="assistant", content=[
        {"type": "thinking", "thinking": "I should...", "signature": "sig123"},
        {"type": "text", "text": "The answer is 42"},
    ])
    items = _convert_message(msg)
    assert len(items) == 1
    assert items[0]["content"] == "The answer is 42"


def test_redacted_thinking_ignored():
    msg = AnthropicMessage(role="assistant", content=[
        {"type": "redacted_thinking", "data": "..."},
        {"type": "text", "text": "Final answer"},
    ])
    assert _convert_message(msg) == [{"role": "assistant", "content": "Final answer"}]


def test_multi_turn():
    msgs = [
        AnthropicMessage(role="user", content="Weather in Beijing?"),
        AnthropicMessage(role="assistant", content=[
            {"type": "tool_use", "id": "call_1", "name": "get_weather",
             "input": {"city": "Beijing"}},
        ]),
        AnthropicMessage(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_1", "content": "22°C"},
        ]),
        AnthropicMessage(role="assistant", content="It's 22°C."),
    ]
    all_items = []
    for m in msgs:
        all_items.extend(_convert_message(m))
    assert len(all_items) == 4
    assert all_items[0]["role"] == "user"
    assert all_items[1]["role"] == "assistant"
    assert all_items[2]["role"] == "user"
    assert all_items[3] == {"role": "assistant", "content": "It's 22°C."}


def test_none_content():
    assert _convert_message(AnthropicMessage(role="user", content=None)) == []


# ── anthropic_to_codex ──────────────────────────────────────────────────

def test_full_request_translation():
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[AnthropicMessage(role="user", content="Hello")],
    )
    body = anthropic_to_codex(req)
    assert body["model"] == "gpt-5.5"
    assert body["input"] == [{"role": "user", "content": "Hello"}]
    assert "max_output_tokens" not in body
    assert body["stream"] is True
    assert body["store"] is False


def test_max_tokens_ignored():
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[AnthropicMessage(role="user", content="Hi")],
    )
    body = anthropic_to_codex(req)
    assert "max_output_tokens" not in body


# ── CodexStreamTranslator (output 方向) ────────────────────────────────

def test_translator_text_message():
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    ev = t.process_event("response.created", {})
    assert any(e[0] == "message_start" for e in ev)

    ev = t.process_event("response.output_item.added",
                         {"item": {"type": "message"}})
    assert any(e[0] == "content_block_start" for e in ev)

    ev = t.process_event("response.output_text.delta",
                         {"delta": "Hello", "content_index": 0})
    assert any(e[0] == "content_block_delta" for e in ev)
    cb = json.loads(next(e[1] for e in ev if e[0] == "content_block_delta"))
    assert cb["delta"]["text"] == "Hello"

    ev = t.process_event("response.completed", {
        "response": {"status": "completed", "usage": {"output_tokens": 5}}
    })
    assert any(e[0] == "content_block_stop" for e in ev)
    assert any(e[0] == "message_delta" for e in ev)
    assert any(e[0] == "message_stop" for e in ev)
    md = json.loads(next(e[1] for e in ev if e[0] == "message_delta"))
    assert md["delta"]["stop_reason"] == "end_turn"


def test_translator_function_call():
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})

    ev = t.process_event("response.output_item.added", {
        "item": {
            "type": "function_call",
            "id": "fc_123",
            "function_call": {
                "name": "get_weather",
                "arguments": '{"city": "Beijing"}',
            },
        }
    })
    assert any(e[0] == "content_block_start" for e in ev)
    assert any(e[0] == "content_block_stop" for e in ev)
    start = json.loads(next(e[1] for e in ev if e[0] == "content_block_start"))
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["name"] == "get_weather"
    assert start["content_block"]["input"] == {"city": "Beijing"}

    ev = t.process_event("response.completed", {
        "response": {"status": "completed", "usage": {"output_tokens": 0}}
    })
    md = json.loads(next(e[1] for e in ev if e[0] == "message_delta"))
    assert md["delta"]["stop_reason"] == "tool_use"


def test_translator_text_then_function_call():
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})
    t.process_event("response.output_item.added",
                    {"item": {"type": "message"}})
    t.process_event("response.output_text.delta",
                    {"delta": "Let me search", "content_index": 0})

    ev = t.process_event("response.output_item.added", {
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "function_call": {"name": "search", "arguments": '{"q":"x"}'},
        }
    })
    assert any(e[0] == "content_block_stop" for e in ev)
    assert any(e[0] == "content_block_start" for e in ev)

    ev = t.process_event("response.completed", {
        "response": {"status": "completed", "usage": {"output_tokens": 10}}
    })
    md = json.loads(next(e[1] for e in ev if e[0] == "message_delta"))
    assert md["delta"]["stop_reason"] == "tool_use"


def test_translator_text_only():
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})
    t.process_event("response.output_item.added",
                    {"item": {"type": "message"}})
    t.process_event("response.output_text.delta",
                    {"delta": "Hi", "content_index": 0})
    ev = t.process_event("response.completed", {
        "response": {"status": "completed", "usage": {"output_tokens": 2}}
    })
    md = json.loads(next(e[1] for e in ev if e[0] == "message_delta"))
    assert md["delta"]["stop_reason"] == "end_turn"


def test_translator_failed():
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    ev = t.process_event("response.failed",
                         {"error": {"message": "Something broke"}})
    assert any(e[0] == "message_delta" for e in ev)
    md = json.loads(next(e[1] for e in ev if e[0] == "message_delta"))
    assert md["delta"]["stop_reason"] == "error"


# ── thinking → reasoning 转发 ─────────────────────────────────────────

def test_thinking_config_forwarded():
    """Anthropic thinking 参数 → Codex reasoning 配置。"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        thinking={"type": "enabled", "budget_tokens": 16000},
        messages=[AnthropicMessage(role="user", content="Solve a complex problem")],
    )
    body = anthropic_to_codex(req)
    assert "reasoning" in body
    assert body["reasoning"]["effort"] == "high"
    assert body["reasoning"]["summary"] == "detailed"


def test_thinking_low_budget():
    """低 budget_tokens → low effort。"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        thinking={"type": "enabled", "budget_tokens": 1024},
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    body = anthropic_to_codex(req)
    assert body["reasoning"]["effort"] == "low"


def test_thinking_medium_budget():
    """中等 budget_tokens → medium effort。"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        thinking={"type": "enabled", "budget_tokens": 4096},
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    body = anthropic_to_codex(req)
    assert body["reasoning"]["effort"] == "medium"


def test_thinking_disabled():
    """thinking type=disabled → 不发送 reasoning。"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        thinking={"type": "disabled", "budget_tokens": 0},
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    body = anthropic_to_codex(req)
    assert "reasoning" not in body


def test_no_thinking_config():
    """没有 thinking 参数 → 默认启用 reasoning。"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    body = anthropic_to_codex(req)
    assert "reasoning" in body
    assert body["reasoning"]["summary"] == "detailed"


# ── CodexStreamTranslator: reasoning → thinking SSE ──────────────────

def test_translator_reasoning_item_start():
    """reasoning output item 添加 → 标记 _in_reasoning，不立即发送 block。"""
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})
    ev = t.process_event("response.output_item.added", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    assert t._in_reasoning
    # 在 summary 文本到达前不应发送 thinking block
    assert not any("thinking" in (e[1] if len(e) > 1 else "") for e in ev)


def test_translator_reasoning_summary_delta():
    """reasoning_summary_text.delta → thinking_delta。"""
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})
    t.process_event("response.output_item.added", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    t.process_event("response.reasoning_summary_part.added", {
        "item_id": "rs_1", "output_index": 0,
        "part": {"type": "summary_text", "text": ""},
    })
    ev = t.process_event("response.reasoning_summary_text.delta", {
        "delta": "Let me think",
        "item_id": "rs_1", "output_index": 0,
    })
    assert t._thinking_started
    # 应该包含 content_block_delta with thinking_delta
    delta_events = [e for e in ev if e[0] == "content_block_delta"]
    assert len(delta_events) > 0
    d = json.loads(delta_events[0][1])
    assert d["delta"]["type"] == "thinking_delta"
    assert d["delta"]["thinking"] == "Let me think"


def test_translator_reasoning_to_text_transition():
    """reasoning → message 过渡：先关闭 thinking block，再开始 text block。"""
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})

    # Reasoning item
    t.process_event("response.output_item.added", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    t.process_event("response.reasoning_summary_part.added", {
        "item_id": "rs_1", "output_index": 0,
        "part": {"type": "summary_text", "text": ""},
    })
    t.process_event("response.reasoning_summary_text.delta", {
        "delta": "I should say hello",
        "item_id": "rs_1", "output_index": 0,
    })

    # Reasoning done
    ev_done = t.process_event("response.output_item.done", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    assert any(e[0] == "content_block_stop" for e in ev_done)
    assert any(e[0] == "signature_delta" for e in ev_done)

    # Message item (text)
    ev_msg = t.process_event("response.output_item.added", {
        "item": {"id": "msg_1", "type": "message", "status": "in_progress",
                 "content": [], "role": "assistant"},
    })
    # 应该开始新的 text content block
    cb_starts = [e for e in ev_msg if e[0] == "content_block_start"]
    assert len(cb_starts) > 0
    start = json.loads(cb_starts[0][1])
    assert start["content_block"]["type"] == "text"


def test_translator_reasoning_signature():
    """reasoning 完成时 → 发送 signature_delta。"""
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})
    t.process_event("response.output_item.added", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    t.process_event("response.reasoning_summary_part.added", {
        "item_id": "rs_1", "output_index": 0,
        "part": {"type": "summary_text", "text": ""},
    })
    t.process_event("response.reasoning_summary_text.delta", {
        "delta": "Hmm...",
        "item_id": "rs_1", "output_index": 0,
    })
    ev = t.process_event("response.output_item.done", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    sigs = [e for e in ev if e[0] == "signature_delta"]
    assert len(sigs) == 1
    sig = json.loads(sigs[0][1])
    assert sig["delta"]["signature"] == "codex-proxy-reasoning-summary"


def test_translator_empty_reasoning():
    """空 reasoning（无 summary）→ 不发送 thinking block。"""
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})

    # 空 reasoning item（无 summary_part events）
    ev = t.process_event("response.output_item.added", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    assert t._in_reasoning
    assert not t._thinking_started

    # 直接 done
    ev_done = t.process_event("response.output_item.done", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    # 不应发送 thinking 相关事件
    assert not any("thinking" in (e[1] if len(e) > 1 else "") for e in ev_done)
    assert not any(e[0] == "signature_delta" for e in ev_done)

    # Message item → 应该正常工作
    ev_msg = t.process_event("response.output_item.added", {
        "item": {"id": "msg_1", "type": "message", "status": "in_progress",
                 "content": [], "role": "assistant"},
    })
    cb_starts = [e for e in ev_msg if e[0] == "content_block_start"]
    assert len(cb_starts) > 0


# ── 集成：thinking + text + tool_use ─────────────────────────────────

def test_reasoning_text_then_function_call():
    """reasoning → text → function_call 过渡正确。"""
    t = CodexStreamTranslator("msg_test", "claude-sonnet-4-6")
    t.process_event("response.created", {})

    # Reasoning
    t.process_event("response.output_item.added", {
        "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []},
    })
    t.process_event("response.reasoning_summary_part.added", {
        "item_id": "rs_1", "output_index": 0,
        "part": {"type": "summary_text", "text": ""},
    })
    t.process_event("response.reasoning_summary_text.delta", {
        "delta": "Need to check weather",
        "item_id": "rs_1", "output_index": 0,
    })
    t.process_event("response.output_item.done", {
        "item": {"id": "rs_1", "type": "reasoning"},
    })

    # Function call
    ev = t.process_event("response.output_item.added", {
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "function_call": {"name": "get_weather", "arguments": '{"city":"BJ"}'},
        },
    })
    assert any(e[0] == "content_block_start" for e in ev)
    start = json.loads([e[1] for e in ev if e[0] == "content_block_start"][0])
    assert start["content_block"]["type"] == "tool_use"

    # Complete
    t.process_event("response.completed", {
        "response": {"status": "completed", "usage": {"output_tokens": 5}},
    })
    assert t._has_tool_use


# ── 默认 reasoning ─────────────────────────────────────────────────────

def test_default_reasoning():
    """无 thinking 参数时，默认请求 reasoning。"""
    req = AnthropicRequest(
        model="claude-3-5-haiku-latest",
        messages=[AnthropicMessage(role="user", content="Solve a complex problem")],
    )
    body = anthropic_to_codex(req)
    assert "reasoning" in body
    assert body["reasoning"]["effort"] == "high"
    assert body["reasoning"]["summary"] == "detailed"


def test_thinking_disabled_no_reasoning():
    """thinking type=disabled 时，不发送 reasoning。"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        thinking={"type": "disabled", "budget_tokens": 0},
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    body = anthropic_to_codex(req)
    assert "reasoning" not in body


def test_thinking_enabled_with_budget():
    """thinking type=enabled 时，根据 budget 设置 effort。"""
    req = AnthropicRequest(
        model="claude-sonnet-4-6",
        thinking={"type": "enabled", "budget_tokens": 8000},
        messages=[AnthropicMessage(role="user", content="Complex task")],
    )
    body = anthropic_to_codex(req)
    assert body["reasoning"]["effort"] == "high"
    assert body["reasoning"]["summary"] == "detailed"


def test_reasoning_always_for_haiku():
    """Haiku 模型（Claude Code 不发 thinking）→ 代理默认启用 reasoning。"""
    req = AnthropicRequest(
        model="claude-3-5-haiku-latest",
        messages=[AnthropicMessage(role="user", content="What is 2+2?")],
    )
    body = anthropic_to_codex(req)
    assert "reasoning" in body
