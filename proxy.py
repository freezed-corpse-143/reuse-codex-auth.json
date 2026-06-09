"""
codex-proxy — 复用 Codex CLI auth.json，暴露 Anthropic Messages API 供 Claude Code 使用。

架构:
  Claude Code → POST /v1/messages (Anthropic 格式) → proxy.py → POST /responses (ChatGPT Backend) → chatgpt.com
                                                                     ↑
                                                               auth.json (access_token)
"""

from __future__ import annotations

import asyncio
import base64

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# ── 日志 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("codex-proxy")

# ── 配置 ──────────────────────────────────────────────────────────────────
AUTH_JSON_PATH = Path(os.environ.get("CODEX_AUTH_PATH", "auth.json"))
CODEX_BASE_URL = os.environ.get(
    "CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex"
)
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# 代理配置 — 优先级: PROXY_URL > HTTPS_PROXY > HTTP_PROXY > ALL_PROXY
PROXY_URL: str | None = (
    os.environ.get("PROXY_URL")
    or os.environ.get("HTTPS_PROXY")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("ALL_PROXY")
)

# ── Built-in defaults for model mapping (overridden by config.json) ──
_DEFAULT_MODEL_MAP: dict[str, str] = {
    "claude-sonnet-4-20250514": "gpt-5.5",
    "claude-sonnet-4": "gpt-5.5",
    "claude-3-5-sonnet-20241022": "gpt-5.5",
    "claude-3-5-sonnet-latest": "gpt-5.5",
    "claude-3-5-haiku-latest": "gpt-5.4-mini",
    "claude-3-haiku-20240307": "gpt-5.4-mini",
    "claude-3-5-haiku-20241022": "gpt-5.4-mini",
    "claude-opus-4-20250514": "gpt-5.5",
    "claude-opus-4": "gpt-5.5",
    "claude-opus-4-7": "gpt-5.5",
    "claude-3-opus-latest": "gpt-5.5",
}
_DEFAULT_REVERSE: dict[str, str] = {
    "gpt-5.5": "claude-sonnet-4-20250514",
    "gpt-5.4": "claude-sonnet-4",
    "gpt-5.4-mini": "claude-3-5-haiku-latest",
    "gpt-5.3-codex-spark": "claude-sonnet-4",
}
_DEFAULT_OAI_MAP: dict[str, str] = {
    "gpt-4": "gpt-5.5",
    "gpt-4o": "gpt-5.5",
    "gpt-4o-mini": "gpt-5.4-mini",
    "gpt-5": "gpt-5.5",
    "gpt-5.5": "gpt-5.5",
    "o3": "gpt-5.5",
    "o4-mini": "gpt-5.4-mini",
}
_DEFAULT_RESPONSES_MAP: dict[str, str] = {
    "gpt-4o": "gpt-5.5",
    "gpt-4o-mini": "gpt-5.4-mini",
    "gpt-4.1": "gpt-5.5",
    "o3": "gpt-5.5",
    "o4-mini": "gpt-5.4-mini",
    "o1": "gpt-5.5",
}


def _load_config() -> dict[str, Any]:
    """Load config.json from CWD (or file next to proxy.py), merging with built-in defaults.

    The user can copy config.json and edit any field — missing keys fall back to defaults.
    """
    cfg_path = next(
        (p for p in (Path("config.json"), Path(__file__).parent / "config.json") if p.exists()),
        None,
    )
    cfg: dict[str, Any] = {}
    if cfg_path:
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = raw
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("failed to parse %s: %s — using built-in defaults", cfg_path, exc)

    def _merge(key: str, default: dict[str, str]) -> dict[str, str]:
        val = cfg.get(key)
        return {**default, **(val if isinstance(val, dict) else {})}

    return {
        "model_mapping": _merge("model_mapping", _DEFAULT_MODEL_MAP),
        "reverse_model_mapping": _merge("reverse_model_mapping", _DEFAULT_REVERSE),
        "oai_model_mapping": _merge("oai_model_mapping", _DEFAULT_OAI_MAP),
        "responses_model_mapping": _merge("responses_model_mapping", _DEFAULT_RESPONSES_MAP),
        "default_anthropic_model": (
            cfg.get("default_anthropic_model") or "claude-sonnet-4-20250514"
        ),
        "default_codex_model": cfg.get("default_codex_model") or "gpt-5.5",
    }

_CONFIG = _load_config()
MODEL_MAP: dict[str, str] = _CONFIG["model_mapping"]
REVERSE_MODEL_MAP: dict[str, str] = _CONFIG["reverse_model_mapping"]
OAI_MODEL_MAP: dict[str, str] = _CONFIG["oai_model_mapping"]
DEFAULT_ANTHROPIC_MODEL: str = _CONFIG["default_anthropic_model"]
RESPONSES_MODEL_MAP: dict[str, str] = _CONFIG["responses_model_mapping"]
DEFAULT_CODEX_MODEL: str = _CONFIG["default_codex_model"]
PROXY_REQUEST_ID_PREFIX = "msg_"


# ── JWT 工具 ─────────────────────────────────────────────────────────────
def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """解码 JWT 的 payload 部分（不验签）。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        decoded = base64.urlsafe_b64decode(payload_b64)
        return json.loads(decoded)
    except Exception as exc:
        log.warning("JWT 解码失败: %s", exc)
        return None


# ── 认证管理器 ────────────────────────────────────────────────────────────
@dataclass
class AuthManager:
    """读取 auth.json，管理 access_token 的过期检测与自动刷新。"""

    auth_path: Path = AUTH_JSON_PATH
    _access_token: str = ""
    _account_id: str = ""
    _refresh_token: str = ""
    _api_key: str = ""
    _last_refresh: str = ""
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def load(self) -> None:
        """从 auth.json 加载凭证。"""
        if not self.auth_path.exists():
            log.error("auth.json 不存在: %s", self.auth_path.absolute())
            raise FileNotFoundError(
                f"auth.json 未找到: {self.auth_path.absolute()}\n"
                "请先登录 Codex CLI 以生成该文件，"
                "或设置 CODEX_AUTH_PATH 环境变量指向正确路径。"
            )

        with open(self.auth_path, encoding="utf-8") as f:
            data = json.load(f)

        tokens = data.get("tokens") or {}
        self._access_token = tokens.get("access_token", "")
        self._account_id = tokens.get("account_id", "")
        self._refresh_token = tokens.get("refresh_token", "")
        self._api_key = data.get("OPENAI_API_KEY", "")
        self._last_refresh = data.get("last_refresh", "")

        if not self._access_token and not self._api_key:
            raise ValueError("auth.json 中未找到 access_token 或 OPENAI_API_KEY")

        log.info("凭证加载完成, account_id=%s", self._account_id or "N/A")

    def is_expired(self) -> bool:
        """检查 access_token 是否过期（提前 5 分钟视为过期）。"""
        if not self._access_token:
            return True
        payload = decode_jwt_payload(self._access_token)
        if not payload:
            return True
        exp = payload.get("exp", 0)
        return time.time() >= exp - 300

    def refresh(self) -> None:
        """用 refresh_token 刷新 access_token。"""
        if not self._refresh_token:
            log.error("无 refresh_token，无法自动刷新。请重新登录 Codex CLI。")
            raise RuntimeError("缺少 refresh_token，请重新登录 Codex CLI")

        log.info("正在刷新 access_token...")
        client_kwargs: dict[str, Any] = {}
        if PROXY_URL:
            client_kwargs["proxy"] = PROXY_URL
        with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = client.post(
                OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": CODEX_CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                log.error("令牌刷新失败: HTTP %s %s", resp.status_code, resp.text)
                raise RuntimeError(f"令牌刷新失败: HTTP {resp.status_code}")

            token_data = resp.json()
            new_access = token_data.get("access_token", "")
            if not new_access:
                raise RuntimeError("刷新响应中未包含 access_token")

            self._access_token = new_access
            self._refresh_token = token_data.get("refresh_token", self._refresh_token)

            # 原子写入更新 auth.json
            old_data = json.loads(self.auth_path.read_text(encoding="utf-8"))
            if "tokens" not in old_data:
                old_data["tokens"] = {}
            old_data["tokens"]["access_token"] = self._access_token
            old_data["tokens"]["refresh_token"] = self._refresh_token
            old_data["last_refresh"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            tmp = self.auth_path.with_suffix(".auth.json.tmp")
            tmp.write_text(json.dumps(old_data, indent=2), encoding="utf-8")
            tmp.replace(self.auth_path)

            log.info("access_token 刷新成功，已保存到 %s", self.auth_path)

    async def ensure_token(self) -> str:
        """确保 token 有效并返回（线程安全）。"""
        async with self._refresh_lock:
            if self.is_expired():
                self.refresh()
            return self._access_token

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def api_key(self) -> str:
        return self._api_key


# ── Pydantic 模型 ─────────────────────────────────────────────────────────
class AnthropicMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None


class AnthropicRequest(BaseModel):
    model: str = DEFAULT_ANTHROPIC_MODEL
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    stop_sequences: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    metadata: dict[str, Any] | None = None


# ── Anthropic → Codex (ChatGPT Backend) 翻译 ─────────────────────────────
def _extract_text(content: str | list[dict[str, Any]] | None) -> str:
    """提取 Anthropic content 中的纯文本，遇非 text block 发出警告。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            bt = block.get("type", "")
            if bt == "text":
                texts.append(block.get("text", ""))
            elif bt == "tool_use":
                name = block.get("name", "unknown")
                inp = block.get("input", {})
                texts.append(f"[tool_use: {name} args={json.dumps(inp)}]")
            elif bt == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    for part in result_content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            texts.append(part.get("text", ""))
                elif isinstance(result_content, str):
                    texts.append(result_content)
            elif bt == "image":
                texts.append("[image]")
            else:
                log.warning("未知 content block 类型: %s", bt)
    return "\n".join(texts)


def map_model(anthropic_model: str) -> str:
    """将 Anthropic 模型名映射为 Codex 模型名。"""
    return MODEL_MAP.get(anthropic_model, DEFAULT_CODEX_MODEL)


def _extract_system_text(system: str | list[dict[str, Any]] | None) -> str:
    """从 Anthropic system 字段中提取纯文本（支持 string 和 list 两种格式）。"""
    if system is None:
        return "You are a helpful assistant."
    if isinstance(system, str):
        return system
    texts: list[str] = []
    for block in system:
        if isinstance(block, dict) and block.get("type") in ("text", "ephemeral"):
            texts.append(block.get("text", ""))
    return "\n".join(texts) if texts else "You are a helpful assistant."


def anthropic_to_codex(anth_req: AnthropicRequest) -> dict[str, Any]:
    """将 Anthropic Messages API 请求转换为 ChatGPT Backend Responses API 请求。"""
    openai_input: list[dict[str, Any]] = []
    for msg in anth_req.messages:
        if msg.content is None:
            continue
        text = _extract_text(msg.content)
        if text:
            openai_input.append({"role": msg.role, "content": text})

    # instructions = system prompt（处理 string 和 list 两种格式）
    instructions = _extract_system_text(anth_req.system)

    # 工具
    tools: list[dict[str, Any]] | None = None
    if anth_req.tools:
        tools = []
        for tool in anth_req.tools:
            if tool.get("type") in ("function", None):
                tools.append({
                    "type": "function",
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                    "strict": tool.get("strict", False),
                })
            else:
                tools.append(tool)

    # tool_choice
    tool_choice: str | dict[str, Any] = "auto"
    if anth_req.tool_choice:
        if isinstance(anth_req.tool_choice, str):
            tool_choice = {
                "any": "required",
                "auto": "auto",
                "none": "none",
            }.get(anth_req.tool_choice, anth_req.tool_choice)
        elif isinstance(anth_req.tool_choice, dict):
            name = anth_req.tool_choice.get("name", "")
            tool_choice = {"type": "function", "name": name}

    # 构建请求体
    # ChatGPT Backend 要求 stream=True，且不支持 max_output_tokens
    body: dict[str, Any] = {
        "model": map_model(anth_req.model),
        "input": openai_input,
        "instructions": instructions,
        "stream": True,
        "store": False,
    }
    # Codex Responses API 不支持 temperature/top_p
    # Claude Code v2 可能会发送这些参数，忽略即可
    if tools:
        body["tools"] = tools
    if tool_choice != "auto" or tools:
        body["tool_choice"] = tool_choice
    if anth_req.stop_sequences:
        body["stop"] = anth_req.stop_sequences

    return body


# ── Codex (ChatGPT Backend) SSE → Anthropic SSE 翻译 ──────────────────────
class CodexStreamTranslator:
    """将 ChatGPT Backend 的 SSE 流转换为 Anthropic Messages API 格式的 SSE 流。"""

    def __init__(self, request_id: str, anthropic_model: str):
        self.request_id = request_id
        self.anthropic_model = anthropic_model
        self._message_started = False
        self._content_block_started = False
        self._buffer = ""

    def process_event(
        self, event_name: str, data: dict[str, Any]
    ) -> list[tuple[str, str]]:
        """处理一条 SSE 事件，返回 0~N 条 Anthropic 格式的 (event, data) 元组。"""
        events: list[tuple[str, str]] = []

        if event_name == "response.created":
            events.extend(self._on_created())

        elif event_name == "response.output_item.added":
            events.extend(self._on_item_added(data))

        elif event_name == "response.content_part.added":
            events.extend(self._on_part_added(data))

        elif event_name == "response.output_text.delta":
            events.extend(self._on_text_delta(data))

        elif event_name == "response.completed":
            events.extend(self._on_completed(data))

        elif event_name == "response.failed":
            events.extend(self._on_failed(data))

        return events

    def _ensure_message_started(self) -> list[tuple[str, str]]:
        """如果还没发 message_start，先补发。"""
        events: list[tuple[str, str]] = []
        if not self._message_started:
            msg_start = {
                "type": "message_start",
                "message": {
                    "id": self.request_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.anthropic_model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            }
            events.append(("message_start", json.dumps(msg_start)))
            self._message_started = True
        return events

    def _ensure_content_block_started(self) -> list[tuple[str, str]]:
        """如果还没发 content_block_start，先补发。"""
        events: list[tuple[str, str]] = []
        if not self._content_block_started:
            cb_start = {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
            events.append(("content_block_start", json.dumps(cb_start)))
            self._content_block_started = True
        return events

    def _on_created(self) -> list[tuple[str, str]]:
        return self._ensure_message_started()

    def _on_item_added(self, data: dict[str, Any]) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        events.extend(self._ensure_message_started())
        item = data.get("item", data)
        if item.get("type") == "message":
            events.extend(self._ensure_content_block_started())
        return events

    def _on_part_added(self, data: dict[str, Any]) -> list[tuple[str, str]]:
        return []

    def _on_text_delta(self, data: dict[str, Any]) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        events.extend(self._ensure_message_started())
        events.extend(self._ensure_content_block_started())

        delta = data.get("delta", "")
        if delta:
            self._buffer += delta
            cbd = {
                "type": "content_block_delta",
                "index": data.get("content_index", 0),
                "delta": {"type": "text_delta", "text": delta},
            }
            events.append(("content_block_delta", json.dumps(cbd)))

        return events

    def _emit_content_block_stop(self) -> list[tuple[str, str]]:
        """如果 content_block 已开始，发送 content_block_stop。"""
        if self._content_block_started:
            self._content_block_started = False
            return [(
                "content_block_stop",
                json.dumps({"type": "content_block_stop", "index": 0}),
            )]
        return []

    def _on_completed(self, data: dict[str, Any]) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        # 先关闭 content block
        events.extend(self._emit_content_block_stop())

        resp = data.get("response", data)
        status = resp.get("status", "completed")
        usage = resp.get("usage", {}) or {}

        stop_reason_map = {
            "completed": "end_turn",
            "incomplete": "max_tokens",
        }
        stop_reason = stop_reason_map.get(status, status)

        md = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {
                "output_tokens": usage.get("output_tokens", 0),
            },
        }
        events.append(("message_delta", json.dumps(md)))
        events.append(("message_stop", json.dumps({"type": "message_stop"})))

        return events

    def _on_failed(self, data: dict[str, Any]) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        # 先关闭 content block
        events.extend(self._emit_content_block_stop())

        err = data.get("error", {})
        log.error("Codex backend 流式错误: %s", err)
        md = {
            "type": "message_delta",
            "delta": {"stop_reason": "error", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        }
        events.append(("message_delta", json.dumps(md)))
        events.append(("message_stop", json.dumps({"type": "message_stop"})))
        return events


# ── 全局状态 ──────────────────────────────────────────────────────────────
auth = AuthManager()
http_client: httpx.AsyncClient | None = None


def _build_httpx_client(**kwargs: Any) -> httpx.AsyncClient:
    """构建 httpx 客户端，若配置了 PROXY_URL 则自动使用代理。"""
    base_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
        "follow_redirects": True,
    }
    base_kwargs.update(kwargs)
    if PROXY_URL:
        log.info("使用代理: %s", PROXY_URL)
        base_kwargs["proxy"] = PROXY_URL
    return httpx.AsyncClient(**base_kwargs)  # type: ignore[arg-type]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    auth.load()
    http_client = _build_httpx_client()
    yield
    if http_client:
        await http_client.aclose()


app = FastAPI(
    title="Codex Proxy — Anthropic API for Claude Code",
    version="0.2.0",
    lifespan=lifespan,
)

# Anthropic 格式的错误类型映射
_ERROR_TYPE_MAP: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "api_error",
    504: "api_error",
}


def _anthropic_error(status: int, message: str) -> JSONResponse:
    """返回 Anthropic Messages API 格式的错误响应。"""
    err_type = _ERROR_TYPE_MAP.get(status, "api_error")
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {
                "type": err_type,
                "message": message,
            },
        },
    )


@app.exception_handler(HTTPException)
async def _anthropic_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """将 FastAPI HTTPException 转换为 Anthropic 格式的错误响应。"""
    return _anthropic_error(exc.status_code, exc.detail)


# ── SSE 流式响应生成器 ────────────────────────────────────────────────────
async def codex_stream_to_anthropic_sse(
    request_id: str,
    anthropic_model: str,
    codex_response: httpx.Response,
) -> AsyncIterator[str]:
    translator = CodexStreamTranslator(request_id, anthropic_model)
    current_event = ""

    async for raw_line in codex_response.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event: "):
            current_event = line[7:]
            continue
        if line.startswith("data: ") and current_event:
            json_str = line[6:]
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            events = translator.process_event(current_event, data)
            for ev_name, ev_data in events:
                yield f"event: {ev_name}\ndata: {ev_data}\n\n"
            current_event = ""

    # 流结束 — 连接自然关闭
    return


# ── 辅助路由 ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """健康检查。"""
    return {
        "status": "ok",
        "auth_file": str(auth.auth_path),
        "auth_loaded": bool(auth.access_token),
        "auth_account": auth.account_id,
        "codex_base_url": CODEX_BASE_URL,
    }


@app.get("/")
@app.head("/")
async def root():
    """Claude Code 连接性检查。"""
    return ""


@app.get("/v1/models")
async def list_models():
    """列出支持的模型（去重后的映射列表）。"""
    seen_codex_models: set[str] = set()
    models = []
    for anthro_model, codex_model in MODEL_MAP.items():
        if codex_model in seen_codex_models:
            continue
        seen_codex_models.add(codex_model)
        models.append({
            "id": anthro_model,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "codex-proxy",
            "permission": [],
            "root": anthro_model,
            "parent": None,
            "codex_model": codex_model,
        })
    return {"object": "list", "data": models}


# ── 核心路由: POST /v1/messages ──────────────────────────────────────────
@app.post("/v1/messages")
async def proxy_messages(request: Request):
    """接收 Anthropic Messages API 格式请求，转发给 ChatGPT Backend，返回 Anthropic 格式。"""
    # ── 1. 解析请求 ──
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    anthropic_version = request.headers.get("anthropic-version", "")
    anthro_request = AnthropicRequest(**body)
    stream = anthro_request.stream

    # ── 2. 确保 token 有效 ──
    try:
        token = await auth.ensure_token()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    # ── 3. 翻译为 Codex 格式 ──
    codex_body = anthropic_to_codex(anthro_request)
    codex_model = codex_body["model"]

    log.info(
        "→ %s → %s  stream=%s  input=%d msgs",
        anthro_request.model,
        codex_model,
        stream,
        len(codex_body.get("input", [])),
    )

    # ── 4. 调用 ChatGPT Backend ──
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ChatGPT-Account-ID": auth.account_id,
        "Origin": "https://chatgpt.com",
        "User-Agent": "Codex-CLI/1.0",
    }

    if http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client not initialized")

    codex_resp = await http_client.post(
        f"{CODEX_BASE_URL}/responses",
        json=codex_body,
        headers=headers,
    )

    # ── 5. 错误处理 + 自动重试 ──
    if codex_resp.status_code >= 400:
        error_detail = await codex_resp.aread()
        err_text = error_detail.decode(errors="replace")
        log.error("Codex backend error: HTTP %s %s", codex_resp.status_code, err_text)

        # 401 → token 过期，刷新后重试一次
        if codex_resp.status_code == 401:
            log.info("Token 过期，尝试刷新后重试...")
            try:
                auth.refresh()
                token = auth.access_token
                headers["Authorization"] = f"Bearer {token}"
                codex_resp = await http_client.post(
                    f"{CODEX_BASE_URL}/responses",
                    json=codex_body,
                    headers=headers,
                )
                if codex_resp.status_code < 400:
                    log.info("Token 刷新后重试成功")
                else:
                    err2 = await codex_resp.aread()
                    raise HTTPException(
                        status_code=codex_resp.status_code,
                        detail=err2.decode(errors="replace"),
                    )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc))
        else:
            # 非 401 错误直接返回
            raise HTTPException(
                status_code=codex_resp.status_code,
                detail=err_text,
            )

    # ── 6. 流式响应 ──
    if stream:
        request_id = f"{PROXY_REQUEST_ID_PREFIX}{uuid.uuid4().hex[:24]}"
        resp_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        if anthropic_version:
            resp_headers["anthropic-version"] = anthropic_version
        return StreamingResponse(
            codex_stream_to_anthropic_sse(request_id, anthro_request.model, codex_resp),
            media_type="text/event-stream",
            headers=resp_headers,
        )

    # ── 7. 非流式响应 ──
    # ChatGPT Backend 强制 stream=True，所以需要缓冲流式响应
    request_id = f"{PROXY_REQUEST_ID_PREFIX}{uuid.uuid4().hex[:24]}"
    translator = CodexStreamTranslator(request_id, anthro_request.model)
    current_event = ""
    final_response_data: dict[str, Any] | None = None

    async for raw_line in codex_resp.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event: "):
            current_event = line[7:]
            continue
        if line.startswith("data: ") and current_event:
            json_str = line[6:]
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            if current_event == "response.completed":
                final_response_data = data.get("response", data)
            elif current_event == "response.failed":
                error_msg = data.get("error", {}).get("message", str(data))
                raise HTTPException(status_code=502, detail=error_msg)

            # 处理事件让 translator 累积 _buffer
            translator.process_event(current_event, data)
            current_event = ""

    # 从 translator 的缓冲和 completed 事件构建响应
    anthropic_model = anthro_request.model
    output_text = translator._buffer

    content_blocks: list[dict[str, Any]] = []
    if output_text:
        content_blocks.append({"type": "text", "text": output_text})

    if final_response_data:
        status = final_response_data.get("status", "completed")
        usage = final_response_data.get("usage", {}) or {}
        stop_reason_map = {
            "completed": "end_turn",
            "incomplete": "max_tokens",
        }
        stop_reason = stop_reason_map.get(status, status)
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
    else:
        stop_reason = "end_turn"
        input_tokens = 0
        output_tokens = 0

    resp_headers = {"Content-Type": "application/json"}
    if anthropic_version:
        resp_headers["anthropic-version"] = anthropic_version

    return JSONResponse(
        content={
            "id": request_id,
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": anthropic_model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
        headers=resp_headers,
    )




def _chatcompletions_to_codex(body: dict[str, Any]) -> dict[str, Any]:
    """将 OpenAI Chat Completions 请求转换为 Codex Responses API 格式。"""
    model = body.get("model", "gpt-4o")
    codex_model = OAI_MODEL_MAP.get(model, "gpt-5.5")
    messages = body.get("messages", [])

    input_msgs: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
            text = " ".join(texts)
        else:
            text = str(content) if content else ""
        if text or role != "user":
            input_msgs.append({"role": role, "content": text})

    req: dict[str, Any] = {
        "model": codex_model,
        "input": input_msgs,
        "instructions": "You are a helpful assistant.",
        "stream": True,
        "store": False,
    }
    if (temp := body.get("temperature")) is not None:
        req["temperature"] = temp
    return req


async def _codex_sse_to_chat_completions(
    request_id: str,
    model: str,
    codex_response: httpx.Response,
) -> AsyncIterator[str]:
    """将 Codex Responses API SSE 流转换为 OpenAI Chat Completions SSE 流。"""
    current_event = ""
    has_content = False

    async for raw_line in codex_response.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event: "):
            current_event = line[7:]
            continue
        if line.startswith("data: ") and current_event:
            json_str = line[6:]
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            if current_event == "response.created":
                chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"

            elif current_event == "response.output_text.delta":
                delta_text = data.get("delta", "")
                if delta_text:
                    has_content = True
                    chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

            elif current_event == "response.completed":
                resp = data.get("response", data)
                usage = resp.get("usage", {}) or {}
                status = resp.get("status", "completed")
                finish_reason_map = {"completed": "stop", "incomplete": "length"}
                finish_reason = finish_reason_map.get(status, status)
                chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    "usage": {
                        "prompt_tokens": usage.get("input_tokens", 0),
                        "completion_tokens": usage.get("output_tokens", 0),
                        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    },
                }
                if not has_content:
                    chunk["choices"][0]["delta"]["content"] = ""
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"

            elif current_event == "response.failed":
                err = data.get("error", {})
                log.error("Chat Completions 流式错误: %s", err)
                err_chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
                yield f"data: {json.dumps(err_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            current_event = ""


@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """接收 OpenAI Chat Completions 格式请求，转发给 ChatGPT Backend，返回 Chat Completions 格式。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    stream = body.get("stream", False)
    try:
        token = await auth.ensure_token()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    codex_body = _chatcompletions_to_codex(body)
    codex_model = codex_body["model"]
    oai_model = body.get("model", codex_model)

    log.info(
        "→ [chat] %s → %s  stream=%s  input=%d msgs",
        oai_model, codex_model, stream, len(codex_body.get("input", [])),
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ChatGPT-Account-ID": auth.account_id,
        "Origin": "https://chatgpt.com",
        "User-Agent": "Codex-CLI/1.0",
    }

    if http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client not initialized")

    codex_resp = await http_client.post(
        f"{CODEX_BASE_URL}/responses",
        json=codex_body,
        headers=headers,
    )

    if codex_resp.status_code >= 400:
        error_detail = await codex_resp.aread()
        err_text = error_detail.decode(errors="replace")
        log.error("Codex backend error: HTTP %s %s", codex_resp.status_code, err_text)
        if codex_resp.status_code == 401:
            try:
                auth.refresh()
                token = auth.access_token
                headers["Authorization"] = f"Bearer {token}"
                codex_resp = await http_client.post(
                    f"{CODEX_BASE_URL}/responses",
                    json=codex_body,
                    headers=headers,
                )
                if codex_resp.status_code >= 400:
                    err2 = await codex_resp.aread()
                    raise HTTPException(status_code=codex_resp.status_code, detail=err2.decode(errors="replace"))
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc))
        else:
            raise HTTPException(status_code=codex_resp.status_code, detail=err_text)

    if stream:
        request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        return StreamingResponse(
            _codex_sse_to_chat_completions(request_id, oai_model, codex_resp),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # 非流式: 缓冲完整 SSE，组装 Chat Completions 响应
    request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    current_event = ""
    full_text = ""
    final_response_data: dict[str, Any] | None = None

    async for raw_line in codex_resp.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event: "):
            current_event = line[7:]
            continue
        if line.startswith("data: ") and current_event:
            json_str = line[6:]
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                continue
            if current_event == "response.output_text.delta":
                full_text += data.get("delta", "")
            elif current_event == "response.completed":
                final_response_data = data.get("response", data)
            elif current_event == "response.failed":
                error_msg = data.get("error", {}).get("message", str(data))
                raise HTTPException(status_code=502, detail=error_msg)
            current_event = ""

    if final_response_data:
        usage = final_response_data.get("usage", {}) or {}
        status = final_response_data.get("status", "completed")
        finish_reason_map = {"completed": "stop", "incomplete": "length"}
        finish_reason = finish_reason_map.get(status, status)
    else:
        usage = {}
        finish_reason = "stop"

    return JSONResponse(content={
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": oai_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": full_text},
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    })

# ── OpenAI Responses API 兼容 ──────────────────────────────────────────────
RESPONSES_REQUEST_ID_PREFIX = "resp_"


async def _responses_stream_to_openai(
    request_id: str,
    model: str,
    codex_response: httpx.Response,
) -> AsyncIterator[str]:
    """Pass through Codex SSE events, mapping model names in data payloads."""
    current_event = ""
    async for raw_line in codex_response.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event: "):
            current_event = line[7:]
            yield f"{line}\n"
            continue
        if line.startswith("data: ") and current_event:
            json_str = line[6:]
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                yield f"{line}\n"
                continue
            # Map model name in completed/failed events
            if current_event in ("response.completed", "response.failed"):
                resp = data.get("response", data)
                if isinstance(resp, dict):
                    resp["model"] = model
                data["model"] = model
            yield f"data: {json.dumps(data)}\n\n"
            current_event = ""


@app.post("/v1/responses")
async def proxy_responses(request: Request):
    """Receive OpenAI Responses API request, forward to ChatGPT Backend, return OpenAI format."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client not initialized")

    stream = body.get("stream", False)
    model = body.get("model", "gpt-4o")
    codex_model = RESPONSES_MODEL_MAP.get(model, DEFAULT_CODEX_MODEL)

    log.info(
        "-> [resp] %s -> %s  stream=%s", model, codex_model, stream,
    )

    try:
        token = await auth.ensure_token()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ChatGPT-Account-ID": auth.account_id,
        "Origin": "https://chatgpt.com",
        "User-Agent": "Codex-CLI/1.0",
    }
    # Build the backend body: normalize input, map model, pass through known fields
    raw_input = body.get("input", "")
    if isinstance(raw_input, str):
        codex_input: list[dict[str, Any]] = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        codex_input = raw_input
    else:
        codex_input = []

    codex_body: dict[str, Any] = {
        "model": codex_model,
        "input": codex_input,
        "instructions": body.get("instructions", "You are a helpful assistant."),
        "stream": True,
        "store": body.get("store", False),
    }
    for key in ("temperature", "top_p", "tools", "tool_choice", "stop"):
        if key in body:
            codex_body[key] = body[key]

    codex_resp = await http_client.post(
        f"{CODEX_BASE_URL}/responses",
        json=codex_body,
        headers=headers,
    )

    if codex_resp.status_code >= 400:
        error_detail = await codex_resp.aread()
        err_text = error_detail.decode(errors="replace")
        log.error("Codex backend error: HTTP %s %s", codex_resp.status_code, err_text)
        if codex_resp.status_code == 401:
            try:
                auth.refresh()
                token = auth.access_token
                headers["Authorization"] = f"Bearer {token}"
                codex_resp = await http_client.post(
                    f"{CODEX_BASE_URL}/responses",
                    json=codex_body,
                    headers=headers,
                )
                if codex_resp.status_code >= 400:
                    err2 = await codex_resp.aread()
                    raise HTTPException(
                        status_code=codex_resp.status_code, detail=err2.decode(errors="replace")
                    )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=502, detail=str(exc))
        else:
            raise HTTPException(status_code=codex_resp.status_code, detail=err_text)

    if stream:
        request_id = f"{RESPONSES_REQUEST_ID_PREFIX}{uuid.uuid4().hex[:24]}"
        return StreamingResponse(
            _responses_stream_to_openai(request_id, model, codex_resp),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Non-streaming: buffer SSE events, assemble Responses API response
    request_id = f"{RESPONSES_REQUEST_ID_PREFIX}{uuid.uuid4().hex[:24]}"
    current_event = ""
    full_text = ""
    output_items: list[dict[str, Any]] = []
    final_resp: dict[str, Any] | None = None

    async for raw_line in codex_resp.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event: "):
            current_event = line[7:]
            continue
        if line.startswith("data: ") and current_event:
            json_str = line[6:]
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            if current_event == "response.output_text.delta":
                full_text += data.get("delta", "")
            elif current_event == "response.output_item.added":
                item = data.get("item", data)
                if isinstance(item, dict):
                    output_items.append(item)
            elif current_event == "response.content_part.added":
                part = data.get("part", data)
                if isinstance(part, dict):
                    if output_items:
                        output_items[-1].setdefault("content", []).append(part)
            elif current_event == "response.completed":
                final_resp = data.get("response", data)
            elif current_event == "response.failed":
                error_msg = data.get("error", {}).get("message", str(data))
                raise HTTPException(status_code=502, detail=error_msg)

            current_event = ""

    # Merge accumulated delta text into the last output item's content
    if full_text and output_items:
        last = output_items[-1]
        if last.get("type") in ("message",) and "content" in last:
            for c in last["content"]:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text", "") == "":
                    c["text"] = full_text

    if not output_items and full_text:
        output_items.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full_text}],
        })

    usage = (final_resp or {}).get("usage", {}) or {}

    return JSONResponse(content={
        "id": request_id,
        "object": "response",
        "created": int(time.time()),
        "model": model,
        "output": output_items,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    })
# ── 入口 ──────────────────────────────────────────────────────────────────
def main():
    import uvicorn

    host = os.environ.get("PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("PROXY_PORT", "8080"))

    from importlib.metadata import version as _pkg_version

    try:
        ver = _pkg_version("codex-proxy")
    except Exception:
        ver = "0.3.0"

    log.info("=" * 60)
    log.info("Codex Proxy v%s 启动", ver)
    log.info("  监听地址:  http://%s:%s", host, port)
    log.info("  auth.json: %s", auth.auth_path.absolute())
    log.info("  Codex API: %s/responses", CODEX_BASE_URL)
    if PROXY_URL:
        log.info("  出站代理:  %s", PROXY_URL)
    log.info("=" * 60)
    log.info("")
    log.info("在 Claude Code 中设置:")
    log.info("  export ANTHROPIC_BASE_URL=http://%s:%s", host, port)
    log.info("  export ANTHROPIC_API_KEY=sk-any-value")
    log.info("")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
