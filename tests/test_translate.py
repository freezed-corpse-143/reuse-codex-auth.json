"""单元测试：Anthropic ↔ Codex 格式转换。"""

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


# ── _convert_message ────────────────────────────────────────────────────

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
    assert _convert_message(msg) == [{"role": "user", "content": "Hello, world!"}]


def test_tool_use():
    msg = AnthropicMessage(role="assistant", content=[
        {"type": "text", "text": "I'll check."},
        {"type": "tool_use", "id": "call_abc", "name": "get_weather",
         "input": {"city": "Beijing"}},
    ])
    items = _convert_message(msg)
    assert len(items) == 2
    assert items[0] == {"role": "assistant", "content": "I'll check."}
    assert items[1]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert items[1]["tool_calls"][0]["function"]["arguments"] == '{"city": "Beijing"}'


def test_tool_use_no_text():
    msg = AnthropicMessage(role="assistant", content=[
        {"type": "tool_use", "id": "call_1", "name": "search",
         "input": {"q": "weather"}},
    ])
    items = _convert_message(msg)
    assert len(items) == 1
    assert items[0]["tool_calls"][0]["function"]["name"] == "search"


def test_tool_result():
    msg = AnthropicMessage(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_abc", "content": "22°C"},
    ])
    items = _convert_message(msg)
    assert items == [{"role": "tool", "tool_call_id": "call_abc", "content": "22°C"}]


def test_tool_result_list_content():
    msg = AnthropicMessage(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_1",
         "content": [{"type": "text", "text": "Result: 42"}]},
    ])
    assert _convert_message(msg) == [
        {"role": "tool", "tool_call_id": "call_1", "content": "Result: 42"}
    ]


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
    assert all_items[1]["role"] == "assistant" and "tool_calls" in all_items[1]
    assert all_items[2]["role"] == "tool"
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


# ── CodexStreamTranslator ──────────────────────────────────────────────

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
