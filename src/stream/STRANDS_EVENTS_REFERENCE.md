# Strands SDK — Event Reference for FastAPI Consumers

A developer reference covering every event emitted by the Strands agentic loop and tool executors,
when each event fires, what data it carries, and how to consume it from a FastAPI SSE endpoint
via `agent.stream_async()`.

---

## Table of Contents

1. [How Events Flow to FastAPI](#1-how-events-flow-to-fastapi)
2. [Event Visibility: `is_callback_event`](#2-event-visibility-is_callback_event)
3. [Model Events](#3-model-events)
4. [Tool Events](#4-tool-events)
5. [Lifecycle Events](#5-lifecycle-events)
6. [Multi-Agent Events](#6-multi-agent-events)
7. [Complete Event Quick-Reference Table](#7-complete-event-quick-reference-table)
8. [FastAPI Consumption Patterns](#8-fastapi-consumption-patterns)
9. [Event Ordering in a Single Turn](#9-event-ordering-in-a-single-turn)
10. [Key Pitfalls](#10-key-pitfalls)

---

## 1. How Events Flow to FastAPI

```
agent.stream_async(prompt)
    └─ _run_loop()
         └─ event_loop_cycle()          ← yields TypedEvent objects
              ├─ _handle_model_execution()
              │    └─ process_stream()   ← one event per raw model chunk
              └─ _handle_tool_execution()
                   └─ tool_executor._execute()
                        └─ per-tool _stream()

Each TypedEvent:
  event.is_callback_event == True  →  yielded to FastAPI as event.as_dict()
  event.is_callback_event == False →  consumed internally, NEVER reaches FastAPI
```

`stream_async` loops over all events and yields only those where
`event.is_callback_event is True` (see `agent.py:833–836`).

---

## 2. Event Visibility: `is_callback_event`

| Value | Meaning | FastAPI sees it? |
|---|---|---|
| `True` (default) | Normal observable event | Yes — yielded by `stream_async` |
| `False` (override) | Internal signal, not for consumers | No |

Events that **override to `False`**:
- `ModelStopReason` — internal stop signal, absorbed by `event_loop_cycle`
- `EventLoopStopEvent` — loop termination signal
- `ToolResultEvent` — per-tool raw result, consumed by executor before reaching FastAPI

---

## 3. Model Events

These events are produced during **LLM inference** — between when the model is called and
when it finishes its response. They carry LLM-generated content and tool-call parameters,
**never tool execution results**.

### 3.1 `ModelStreamChunkEvent`

> One event per raw chunk from the model provider.

- **Source**: `streaming.py` → `process_stream()` — emitted for every single raw chunk
- **`is_callback_event`**: `True`
- **Payload key**: `"event"` → raw `StreamEvent` dict

**Raw chunk types inside `event`:**

| Chunk type (key in `event`) | Content |
|---|---|
| `messageStart` | Starts a new message; sets `role: "assistant"` |
| `contentBlockStart` | Starts a content block; if `toolUse` present, contains `toolUseId` and `name` |
| `contentBlockDelta` | Incremental data — either `text` (LLM reasoning) or `toolUse.input` (JSON params being sent **to** a tool) |
| `contentBlockStop` | Finalises the content block |
| `messageStop` | End of message with `stopReason` |
| `metadata` | Token counts and latency metrics |

> **Important**: `contentBlockDelta` with `toolUse.input` carries the JSON **input parameters**
> sent to a tool — this is NOT the tool's response. Tool responses come via `ToolResultEvent` /
> `ToolResultMessageEvent`.

**FastAPI access:**
```python
if "event" in event_dict:
    raw_chunk = event_dict["event"]
    if "contentBlockDelta" in raw_chunk:
        delta = raw_chunk["contentBlockDelta"]["delta"]
        if "text" in delta:
            text_chunk = delta["text"]          # LLM text being generated
        elif "toolUse" in delta:
            input_fragment = delta["toolUse"]["input"]  # tool input params (partial JSON)
```

---

### 3.2 `TextStreamEvent`

> LLM text content, one event per text delta.

- **Source**: `streaming.py:221` — emitted when `contentBlockDelta` contains a `text` field
- **`is_callback_event`**: `True` (when non-empty)
- **Payload keys**:
  - `"data"` → the incremental text string
  - `"delta"` → the raw delta dict

**FastAPI access:**
```python
if "data" in event_dict and "delta" in event_dict:
    text_chunk = event_dict["data"]   # LLM-generated text fragment
```

---

### 3.3 `ToolUseStreamEvent`

> LLM is streaming JSON input parameters for a tool call.

- **Source**: `streaming.py:217` — emitted when `contentBlockDelta` contains a `toolUse` field
- **`is_callback_event`**: `True` (when non-empty)
- **Payload keys**:
  - `"type"` → `"tool_use_stream"`
  - `"delta"` → the raw delta
  - `"current_tool_use"` → `{"toolUseId": "...", "name": "...", "input": "<partial JSON string>"}`

> This tells you **which tool is about to be called** and what parameters the LLM is
> constructing — useful for UI progress indicators.

**FastAPI access:**
```python
if event_dict.get("type") == "tool_use_stream":
    tool_info = event_dict["current_tool_use"]
    tool_name = tool_info["name"]
    partial_input = tool_info["input"]   # partial JSON string, not yet parsed
```

---

### 3.4 `ReasoningTextStreamEvent`

> Extended thinking / chain-of-thought text from the model (Claude models with extended thinking).

- **Source**: `streaming.py:236`
- **`is_callback_event`**: `True`
- **Payload keys**: `"reasoningText"`, `"delta"`, `"reasoning": True`

---

### 3.5 `ReasoningSignatureStreamEvent` / `ReasoningRedactedContentStreamEvent`

> Reasoning signature (verification) or redacted reasoning content.

- **`is_callback_event`**: `True`
- Payload keys: `"reasoning_signature"` / `"reasoningRedactedContent"`, `"delta"`, `"reasoning": True`

---

### 3.6 `CitationStreamEvent`

> Citation metadata when the model cites source documents.

- **Source**: `streaming.py:228`
- **`is_callback_event`**: `True`
- **Payload keys**: `"citation"`, `"delta"`

---

### 3.7 `ModelMessageEvent`

> The **complete assembled assistant message** after the model finishes its response.

- **Source**: `event_loop.py:161` — emitted once per model inference cycle, after `ModelStopReason`
  is absorbed
- **`is_callback_event`**: `True`
- **Payload key**: `"message"` → full `Message` dict with `role: "assistant"`

**Content blocks inside `message.content`:**
- `{"text": "..."}` — LLM response text
- `{"toolUse": {"toolUseId": "...", "name": "...", "input": {...}}}` — tool call requested by LLM

**FastAPI access — extracting tool call IDs:**
```python
msg = event_dict.get("message", {})
if msg.get("role") == "assistant":
    for block in msg.get("content", []):
        if "toolUse" in block:
            tool_use_id = block["toolUse"]["toolUseId"]
            tool_name   = block["toolUse"]["name"]
            tool_input  = block["toolUse"]["input"]
```

> Use this event to map `toolUseId` → `tool_name` **before** tool execution begins.
> `ToolResultMessageEvent` only carries `toolUseId`, not the tool name.

---

### 3.8 `ModelStopReason` *(internal — not visible in FastAPI)*

- **`is_callback_event`**: `False`
- Internal signal absorbed by `event_loop_cycle` to extract `stop_reason`, `message`, `usage`,
  `metrics`. Never reaches `stream_async`.

---

## 4. Tool Events

These events are produced during **tool execution** — after `ModelMessageEvent` and before the
next model inference cycle (or end of loop).

### 4.1 `ToolStreamEvent`

> Intermediate data chunks yielded by a tool **during** its execution.

- **Source**: `_executor.py:246–248` — emitted when a tool's `stream()` method yields any value
  that is not a `ToolResultEvent`
- **`is_callback_event`**: `True`
- **Payload keys**:
  - `"type"` → `"tool_stream"`
  - `"tool_stream_event"` → `{"tool_use": {"toolUseId": "...", "name": "..."}, "data": <any>}`

**When does a tool produce `ToolStreamEvent`s?**

| Tool type | Produces `ToolStreamEvent`? |
|---|---|
| Regular `async def` returning `dict` (e.g. original `a2a_send_message`) | No — single `ToolResultEvent` only |
| `PythonAgentTool` with overridden `stream()` yielding intermediate values | Yes |
| `@tool` decorated `async def` generator (`async def foo() -> AsyncGenerator`) | Yes |
| Agent-as-tool (`agent.as_tool()`) | Yes — via `AgentAsToolStreamEvent` |

**FastAPI access:**
```python
if event_dict.get("type") == "tool_stream":
    stream_data = event_dict["tool_stream_event"]
    tool_use_id = stream_data["tool_use"]["toolUseId"]
    tool_name   = stream_data["tool_use"]["name"]
    chunk       = stream_data["data"]   # str for text, dict for structured data
```

---

### 4.2 `AgentAsToolStreamEvent`

> Subtype of `ToolStreamEvent` for when the tool is a nested agent (`agent.as_tool()`).

- **Source**: `_agent_as_tool.py`
- **`is_callback_event`**: `True`
- Same payload as `ToolStreamEvent` plus `_agent_as_tool` reference accessible via
  `event.agent_as_tool` (Python property, not in `as_dict()`)

---

### 4.3 `ToolResultEvent` *(internal — not visible in FastAPI)*

> Final result of a single tool execution.

- **Source**: `_executor.py:261` — emitted once per tool after `AfterToolCallEvent` hook runs
- **`is_callback_event`**: `False`
- Consumed inside the executor to build `tool_results` list. **Never reaches FastAPI.**
- Can be intercepted and mutated only via the `AfterToolCallEvent` hook.

---

### 4.4 `ToolResultMessageEvent`

> All tool results formatted as a single conversation message, after **all** parallel tools finish.

- **Source**: `event_loop.py:568` — emitted once per tool execution batch
- **`is_callback_event`**: `True`
- **Payload key**: `"message"` → `Message` with `role: "user"`

**Content structure:**
```python
{
    "role": "user",
    "content": [
        {
            "toolResult": {
                "toolUseId": "...",
                "status": "success" | "error",
                "content": [{"text": "..."}]   # the actual tool response
            }
        },
        # one entry per tool that ran in this batch
    ]
}
```

**FastAPI access — extracting tool results:**
```python
msg = event_dict.get("message", {})
if msg.get("role") == "user":
    for block in msg.get("content", []):
        if "toolResult" in block:
            tool_result = block["toolResult"]
            tool_use_id = tool_result["toolUseId"]
            status      = tool_result.get("status")   # "success" or "error"
            for item in tool_result.get("content", []):
                text = item.get("text")               # tool response text
```

> **Deduplication note**: If you already yielded chunks via `ToolStreamEvent` for a given
> `toolUseId`, skip its content in `ToolResultMessageEvent` to avoid duplicates.
> See Section 8 for the pattern.

---

### 4.5 `ToolCancelEvent`

> A tool call was cancelled by a `BeforeToolCallEvent` hook returning a cancel signal.

- **`is_callback_event`**: `True`
- **Payload key**: `"tool_cancel_event"` → `{"tool_use": {...}, "message": "..."}`

---

### 4.6 `ToolInterruptEvent`

> A tool raised an interrupt (human-in-the-loop pause).

- **`is_callback_event`**: `True`
- **Payload key**: `"tool_interrupt_event"` → `{"tool_use": {...}, "interrupts": [...]}`

---

## 5. Lifecycle Events

### 5.1 `InitEventLoopEvent`

- **When**: Very first event, before any processing
- **`is_callback_event`**: `True`
- **Payload**: merged `invocation_state` dict
- **Use**: Logging, tracing initialisation

---

### 5.2 `StartEvent` *(deprecated)*

- **When**: Start of each event loop cycle
- **`is_callback_event`**: `True`
- **Payload**: `{"start": True}`
- Use `StartEventLoopEvent` instead.

---

### 5.3 `StartEventLoopEvent`

- **When**: Beginning of core processing in each cycle (after `StartEvent`)
- **`is_callback_event`**: `True`
- **Payload**: `{"start_event_loop": True}`

---

### 5.4 `EventLoopThrottleEvent`

- **When**: Rate limiting — exponential backoff between retries
- **`is_callback_event`**: `True`
- **Payload**: `{"event_loop_throttled_delay": <seconds>}`

---

### 5.5 `StructuredOutputEvent`

- **When**: Structured output tool result successfully extracted (when `structured_output_model`
  passed to `stream_async`)
- **`is_callback_event`**: `True`
- **Payload**: `{"structured_output": <Pydantic BaseModel instance>}`

---

### 5.6 `ForceStopEvent`

- **When**: Unrecoverable exception or a tool explicitly triggers a force stop
- **`is_callback_event`**: `True`
- **Payload**: `{"force_stop": True, "force_stop_reason": "..."}`

---

### 5.7 `AgentResultEvent`

- **When**: Final event — agent invocation fully complete
- **`is_callback_event`**: `True`
- **Payload**: `{"result": AgentResult}` where `AgentResult` has:
  - `stop_reason` — final stop reason (`"end_turn"`, `"tool_use"`, `"cancelled"`, etc.)
  - `message` — last model message
  - `metrics` — `EventLoopMetrics`
  - `state` — final `request_state`
  - `structured_output` — Pydantic model if structured output was requested

---

### 5.8 `EventLoopStopEvent` *(internal — not visible in FastAPI)*

- **`is_callback_event`**: `False`
- Internal loop termination signal. Consumed by `stream_async` to build `AgentResultEvent`.
  Never reaches FastAPI.

---

## 6. Multi-Agent Events

Relevant when using `GraphOrchestrator`, `Swarm`, or when agents call other agents as tools.

| Event | `is_callback_event` | When emitted |
|---|---|---|
| `MultiAgentNodeStartEvent` | `True` | Node begins execution |
| `MultiAgentNodeStopEvent` | `True` | Node completes execution |
| `MultiAgentHandoffEvent` | `True` | Transition between nodes (Swarm or Graph) |
| `MultiAgentNodeStreamEvent` | `True` | Forwards inner agent events with `node_id` context |
| `MultiAgentResultEvent` | `True` | Entire multi-agent run complete |
| `MultiAgentNodeCancelEvent` | `True` | Node cancelled by `BeforeNodeCallEvent` hook |
| `MultiAgentNodeInterruptEvent` | `True` | Node interrupted |

---

## 7. Complete Event Quick-Reference Table

| Event class | Source | `is_callback_event` | FastAPI key | Phase |
|---|---|---|---|---|
| `InitEventLoopEvent` | `event_loop.py` | `True` | `init_event_loop` | Init |
| `StartEvent` *(deprecated)* | `event_loop.py` | `True` | `start` | Cycle start |
| `StartEventLoopEvent` | `event_loop.py` | `True` | `start_event_loop` | Cycle start |
| `ModelStreamChunkEvent` | `streaming.py` | `True` | `event` | Model inference |
| `TextStreamEvent` | `streaming.py` | `True` | `data`, `delta` | Model inference |
| `ToolUseStreamEvent` | `streaming.py` | `True` | `type:"tool_use_stream"`, `current_tool_use` | Model inference |
| `ReasoningTextStreamEvent` | `streaming.py` | `True` | `reasoningText`, `reasoning` | Model inference |
| `ReasoningSignatureStreamEvent` | `streaming.py` | `True` | `reasoning_signature`, `reasoning` | Model inference |
| `ReasoningRedactedContentStreamEvent` | `streaming.py` | `True` | `reasoningRedactedContent`, `reasoning` | Model inference |
| `CitationStreamEvent` | `streaming.py` | `True` | `citation`, `delta` | Model inference |
| `ModelStopReason` | `streaming.py` | **`False`** | *(internal)* | Model inference |
| `ModelMessageEvent` | `event_loop.py` | `True` | `message` (role: assistant) | Post-inference |
| `ToolStreamEvent` | `_executor.py` | `True` | `type:"tool_stream"`, `tool_stream_event` | Tool execution |
| `AgentAsToolStreamEvent` | `_agent_as_tool.py` | `True` | `type:"tool_stream"`, `tool_stream_event` | Tool execution |
| `ToolResultEvent` | `_executor.py` | **`False`** | *(internal)* | Tool execution |
| `ToolCancelEvent` | `_executor.py` | `True` | `tool_cancel_event` | Tool execution |
| `ToolInterruptEvent` | `_executor.py` | `True` | `tool_interrupt_event` | Tool execution |
| `ToolResultMessageEvent` | `event_loop.py` | `True` | `message` (role: user) | Post-tool |
| `EventLoopThrottleEvent` | `event_loop.py` | `True` | `event_loop_throttled_delay` | Retry |
| `StructuredOutputEvent` | `event_loop.py` | `True` | `structured_output` | Post-tool |
| `ForceStopEvent` | `event_loop.py` | `True` | `force_stop`, `force_stop_reason` | Error |
| `EventLoopStopEvent` | `event_loop.py` | **`False`** | *(internal)* | Loop end |
| `AgentResultEvent` | `agent.py` | `True` | `result` | Final |

---

## 8. FastAPI Consumption Patterns

### 8.1 Basic SSE skeleton

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import json

app = FastAPI()

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    async def generate():
        async for event_dict in agent.stream_async(request.message):
            # event_dict is already a plain dict — all is_callback_event=True events
            yield _sse(event_dict)

    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

### 8.2 Extracting LLM text tokens

```python
async for event_dict in agent.stream_async(prompt):
    if "data" in event_dict and "delta" in event_dict:
        # TextStreamEvent — one LLM text token/fragment
        yield _sse({"type": "text", "text": event_dict["data"]})
```

---

### 8.3 Tracking which tools the LLM called (before execution)

```python
a2a_tool_use_ids: set[str] = set()

async for event_dict in agent.stream_async(prompt):
    msg = event_dict.get("message", {})

    # ModelMessageEvent — assistant role, carries toolUse blocks
    if msg.get("role") == "assistant":
        for block in msg.get("content", []):
            if "toolUse" in block and block["toolUse"]["name"] == "a2a_send_message":
                a2a_tool_use_ids.add(block["toolUse"]["toolUseId"])
```

---

### 8.4 Streaming tool chunks (streaming agents) + full result (non-streaming agents) — no duplicates

```python
a2a_tool_use_ids: set[str] = set()
a2a_streamed_ids: set[str] = set()   # guard against duplicate content

async for event_dict in agent.stream_async(prompt):
    msg = event_dict.get("message", {})

    # Step 1 — record which toolUseIds belong to a2a_send_message
    if msg.get("role") == "assistant":
        for block in msg.get("content", []):
            if "toolUse" in block and block["toolUse"]["name"] == "a2a_send_message":
                a2a_tool_use_ids.add(block["toolUse"]["toolUseId"])

    # Step 2 — forward streaming chunks immediately (streaming agents)
    if event_dict.get("type") == "tool_stream":
        stream_data = event_dict["tool_stream_event"]
        tool_use_id = stream_data["tool_use"]["toolUseId"]
        if tool_use_id in a2a_tool_use_ids:
            chunk = stream_data["data"]
            if isinstance(chunk, str) and chunk:
                a2a_streamed_ids.add(tool_use_id)
                yield _sse({"type": "chunk", "tool_use_id": tool_use_id, "text": chunk})

    # Step 3 — for non-streaming agents: send full result once (skip if already streamed)
    if msg.get("role") == "user":
        for block in msg.get("content", []):
            if "toolResult" in block:
                tool_use_id = block["toolResult"]["toolUseId"]
                if tool_use_id in a2a_tool_use_ids and tool_use_id not in a2a_streamed_ids:
                    for item in block["toolResult"].get("content", []):
                        if isinstance(item.get("text"), str) and item["text"]:
                            yield _sse({"type": "chunk", "tool_use_id": tool_use_id, "text": item["text"]})

yield _sse({"type": "done"})
```

---

### 8.5 Detecting the LLM tool-input streaming phase

```python
# Shows which tool the LLM is currently building input for
if event_dict.get("type") == "tool_use_stream":
    tool_name = event_dict["current_tool_use"]["name"]
    yield _sse({"type": "tool_thinking", "tool": tool_name})
```

---

### 8.6 Accessing final agent result

```python
async for event_dict in agent.stream_async(prompt):
    if "result" in event_dict:
        result = event_dict["result"]   # AgentResult
        stop_reason = result.stop_reason
        final_message = result.message
```

---

## 9. Event Ordering in a Single Turn

For a request that triggers one LLM call followed by two parallel tool calls:

```
InitEventLoopEvent          ← once per agent invocation (first cycle only)
StartEvent                  ← (deprecated) start of cycle
StartEventLoopEvent         ← start of cycle

  ┌─ Model inference phase ─────────────────────────────────────────────────────
  │  ModelStreamChunkEvent  [messageStart]
  │  ModelStreamChunkEvent  [contentBlockStart: toolUse A]
  │  ToolUseStreamEvent     [partial input for tool A]  ×N
  │  ModelStreamChunkEvent  [contentBlockStop]
  │  ModelStreamChunkEvent  [contentBlockStart: toolUse B]
  │  ToolUseStreamEvent     [partial input for tool B]  ×N
  │  ModelStreamChunkEvent  [contentBlockStop]
  │  ModelStreamChunkEvent  [messageStop]
  └─────────────────────────────────────────────────────────────────────────────
ModelMessageEvent            ← complete assistant message with both toolUse blocks

  ┌─ Tool execution phase (ConcurrentToolExecutor — tools A & B run in parallel)
  │  ToolStreamEvent        [chunk from tool A]  ×N  (interleaved with B)
  │  ToolStreamEvent        [chunk from tool B]  ×N  (interleaved with A)
  └─────────────────────────────────────────────────────────────────────────────
ToolResultMessageEvent       ← one message containing BOTH tool results

  [if stop_event_loop=True in request_state]
EventLoopStopEvent (internal)
AgentResultEvent             ← final

  [else — recurse for next LLM cycle]
StartEvent
StartEventLoopEvent
ModelStreamChunkEvent        ← LLM processes tool results ...
  ...
```

---

## 10. Key Pitfalls

### `contentBlockDelta.toolUse.input` ≠ tool response
`contentBlockDelta` with a `toolUse` field contains the **input JSON the LLM is sending to the
tool** — not the tool's output. The tool has not yet been called at this point.
Tool responses only arrive in `ToolResultEvent` (internal) and `ToolResultMessageEvent` (FastAPI).

### `ToolResultEvent` is invisible to FastAPI
`ToolResultEvent.is_callback_event = False`. You cannot see individual per-tool results in
`stream_async`. Use `ToolResultMessageEvent` instead, which batches all results from a parallel
tool batch into one message.

### `ToolResultMessageEvent` has no `tool_name`
The `toolResult` blocks in `ToolResultMessageEvent` only have `toolUseId`. To know which
tool produced which result, build a `toolUseId → tool_name` map from `ModelMessageEvent` first.

### `stop_event_loop` and `structured_output_model` conflict
Setting `request_state["stop_event_loop"] = True` in a hook prevents the recursive LLM call
**and** the structured output cycle. Remove `structured_output_model` from `stream_async` when
using this pattern.

### Parallel tools — one `ToolResultMessageEvent` for all
`ConcurrentToolExecutor` (default) runs all tools in parallel. There is **one**
`ToolResultMessageEvent` after all complete, containing all results. Do not expect one event
per tool.

### Deduplication when mixing `ToolStreamEvent` + `ToolResultMessageEvent`
Streaming agents emit content via `ToolStreamEvent` chunks AND the same content again as the
full result in `ToolResultMessageEvent`. Track which `toolUseId`s have already been streamed
and skip their content in `ToolResultMessageEvent`.
