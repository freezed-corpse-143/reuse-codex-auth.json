# Tasks: Codex Proxy Translation Rewrite

## Phase 1: 基础修复 (P0)

- [ ] **P0** `anthropic_to_codex()`: 转发 `max_tokens` → `max_output_tokens`
      ```python
      if anth_req.max_tokens is not None:
          body["max_output_tokens"] = anth_req.max_tokens
      ```
- [ ] **P0** `config.json`: 补充 Claude v2 模型 ID 映射
      - `claude-sonnet-4-6` → `gpt-5.5`
      - `claude-opus-4-8` → `gpt-5.5`
      - `claude-sonnet-4-20250609` → `gpt-5.5` (如有)

## Phase 2: 输入消息结构化 (P0)

- [ ] **P0** 重写 `_extract_text()` → 新的消息转换函数 `convert_message()` / `convert_messages()`
      - 保留 content block 类型
      - `tool_use` → Codex `tool_calls` array
      - `tool_result` → Codex `tool` role
      - `thinking` / `redacted_thinking` → 忽略
- [ ] **P0** `anthropic_to_codex()`: 使用新的消息转换替代 `_extract_text()`
      - 构建结构化 `input` items 数组
      - 验证多轮对话（user + assistant + tool + ...）的顺序

## Phase 3: 输出工具调用 (P0)

- [ ] **P0** `CodexStreamTranslator._on_item_added()`: 处理 `type=function_call`
      - 发出 `content_block_start` (type: `tool_use`)
      - 包含 `id`, `name`, `input` (解析 arguments JSON)
      - 立即发出 `content_block_stop`（function_call 是原子输出）
- [ ] **P0** `CodexStreamTranslator`: 维护 `content_index` 计数器
      - text block 和 tool_use block 各自递增 index
- [ ] **P0** `CodexStreamTranslator._on_completed()`: 在 `stop_reason` 中处理 `tool_use`
      - Codex `status=completed` with tool calls → Anthropic `stop_reason: "tool_use"`
      - 仅在没有任何 text output 时设为 `tool_use`；如果既有 text 又有 tool_use，保持 `end_turn`

## Phase 4: 思考/推理 (P1)

- [ ] **P1** `AnthropicRequest`: 添加 `thinking: dict | None = None` 字段防止 Pydantic 静默丢弃
      - 仅为了不报错，不需要处理其中的参数
- [ ] **P1** `CodexStreamTranslator._on_part_added()`: 如果 Codex 返回 `reasoning` 类型
      - 转为 Anthropic thinking 或忽略（取决于 Codex 是否支持）
- [ ] **P1** `_extract_text()` (或替代函数): 忽略 `thinking` 类型 content block

## Phase 5: 测试与验证 (P0)

- [ ] **P0** 编写单元测试：`anthropic_to_codex()` 消息格式转换
      - 纯文本 → 单 item
      - tool_use → tool_calls
      - tool_result → tool role
      - 混合内容块
      - thinking 块忽略
- [ ] **P0** 编写单元测试：`CodexStreamTranslator` SSE 事件转换
      - function_call → tool_use content block
      - 多个 output_item（text + function_call 交错）
      - 无 function_call 时的回退
- [ ] **P0** 端到端测试：交互模式 tool 调用
      - 用 tmux 启动 claude，发送触发 tool 的问题
      - 验证 claude 收到 tool_use 并执行
- [ ] **P0** 端到端测试：`--bare --print` 非流式响应
- [ ] **P1** 端到端测试：多轮对话保持

## Phase 6: 文档与发布 (P0)

- [ ] **P0** 更新 `README.md`：新增工具调用支持说明
- [ ] **P0** 更新 `CHANGELOG`
- [ ] **P0** 提交 PR，推送

## 优先级说明

- **P0** = 必须做（否则代理基本不可用）
- **P1** = 应该做（但不是阻塞）
