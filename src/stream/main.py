"""FastAPI orchestration layer — streams A2A tool responses to the caller.

Architecture:
    FastAPI SSE endpoint
      └─ agent.stream_async()
           └─ ConcurrentToolExecutor
                └─ StreamingA2ASendMessageTool.stream()
                     ├─ yield text_chunk  → ToolStreamEvent  → SSE immediately
                     └─ yield ToolResultEvent → ToolResultMessageEvent (final)

Hooks:
    a2a_validate_and_sanitize — validates Pydantic model, sanitises errors,
                                 sets stop_event_loop to prevent LLM reprocessing.
"""

import json
import logging
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from strands import Agent
from strands.hooks import AfterToolCallEvent
from strands.models.bedrock import BedrockModel
from strands.types.tools import ToolResult

from .streaming_a2a_provider import StreamingA2AClientToolProvider

logger = logging.getLogger(__name__)

app = FastAPI(title="Financial Orchestration Layer")


# ── Pydantic model for A2A agent response validation ─────────────────────────

class A2AAgentResponse(BaseModel):
    """Expected structure of the JSON text returned by A2A agents.

    Adjust fields to match your actual downstream agent response schema.
    """
    transaction_id: str
    status: str
    risk_score: float | None = None
    message: str


# ── Generic error messages — never expose internal detail to callers ──────────

_GENERIC_ERROR = "Request could not be completed. Please contact support."
_GENERIC_VALIDATION_ERROR = "Received an unexpected response format from the service."


# ── Helper ────────────────────────────────────────────────────────────────────

def _error_result(tool_use_id: str, message: str) -> ToolResult:
    return {
        "toolUseId": tool_use_id,
        "status": "error",
        "content": [{"text": message}],
    }


# ── Validation + sanitisation hook ───────────────────────────────────────────

def a2a_validate_and_sanitize(event: AfterToolCallEvent) -> None:
    """Validate A2A tool result and sanitise errors before they reach the LLM.

    Runs after every tool call; acts only on a2a_send_message results.

    Behaviour:
        - JSONRPC / transport error  → replace with generic error message
        - Missing text content       → replace with generic validation error
        - JSON parse failure         → replace with generic validation error
        - Pydantic ValidationError   → replace with generic validation error
        - Valid response             → re-serialise validated Pydantic model

    In all cases, sets stop_event_loop=True to prevent LLM from reprocessing
    the tool result.
    """
    if event.tool_use.get("name") != "a2a_send_message":
        return

    result = event.result
    tool_use_id: str = result.get("toolUseId", "")

    # Case 1: Tool already errored (JSONRPC / transport / exception)
    if result.get("status") == "error":
        logger.warning(
            "tool_use_id=<%s> | a2a tool returned error status, sanitising",
            tool_use_id,
        )
        event.result = _error_result(tool_use_id, _GENERIC_ERROR)
        event.invocation_state["request_state"]["stop_event_loop"] = True
        return

    # Case 2: Extract text content from result
    content = result.get("content", [])
    text: str | None = next(
        (item["text"] for item in content if "text" in item), None
    )

    if not text:
        logger.warning(
            "tool_use_id=<%s> | a2a result has no text content",
            tool_use_id,
        )
        event.result = _error_result(tool_use_id, _GENERIC_VALIDATION_ERROR)
        event.invocation_state["request_state"]["stop_event_loop"] = True
        return

    # Case 3: Pydantic validation
    try:
        raw: Any = json.loads(text)
        validated = A2AAgentResponse.model_validate(raw)

        event.result = {
            "toolUseId": tool_use_id,
            "status": "success",
            "content": [{"text": validated.model_dump_json()}],
        }
        logger.debug(
            "tool_use_id=<%s> | a2a response validated successfully",
            tool_use_id,
        )

    except json.JSONDecodeError:
        logger.warning(
            "tool_use_id=<%s> | a2a response is not valid JSON",
            tool_use_id,
        )
        event.result = _error_result(tool_use_id, _GENERIC_VALIDATION_ERROR)

    except ValidationError as exc:
        logger.warning(
            "tool_use_id=<%s>, errors=<%d> | a2a response failed Pydantic validation",
            tool_use_id,
            exc.error_count(),
        )
        event.result = _error_result(tool_use_id, _GENERIC_VALIDATION_ERROR)

    # Always stop the event loop — no LLM reprocessing of A2A results
    event.invocation_state["request_state"]["stop_event_loop"] = True


# ── Agent setup ───────────────────────────────────────────────────────────────

provider = StreamingA2AClientToolProvider(
    known_agent_urls=[
        "http://trade-compliance-agent:8080",
        "http://risk-assessment-agent:8080",
    ],
    timeout=300,
)

agent = Agent(
    model=BedrockModel(model_id="us.amazon.nova-pro-v1:0"),
    tools=[provider],
    hooks=[a2a_validate_and_sanitize],
    system_prompt=(
        "You are a financial-services orchestration layer. "
        "Route requests to the appropriate A2A agents based on user intent "
        "using their agent card details. "
        "Do not modify, summarise, or paraphrase agent responses."
    ),
)


# ── Request model ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


# ── SSE helper ────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Streaming endpoint ────────────────────────────────────────────────────────

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream A2A agent responses back to the caller via SSE.

    Event types emitted to the client:
        {"type": "chunk",  "tool_use_id": "...", "text": "..."}  — incremental chunk
        {"type": "done"}                                           — stream complete
    """
    async def generate() -> AsyncGenerator[str, None]:
        # Track toolUseIds belonging to a2a_send_message calls
        # Populated from ModelMessageEvent (assistant role, toolUse blocks)
        a2a_tool_use_ids: set[str] = set()

        # Track toolUseIds that have already had at least one ToolStreamEvent chunk
        # yielded — used to suppress duplicate content from ToolResultMessageEvent
        # for streaming agents while still forwarding the full result for non-streaming ones
        a2a_streamed_ids: set[str] = set()

        async for event_dict in agent.stream_async(
            request.message,
            invocation_state={"request_state": {}},
        ):
            msg = event_dict.get("message", {})

            # Track a2a_send_message toolUseIds from the assistant's tool call message
            if msg.get("role") == "assistant":
                for block in msg.get("content", []):
                    if (
                        "toolUse" in block
                        and block["toolUse"].get("name") == "a2a_send_message"
                    ):
                        a2a_tool_use_ids.add(block["toolUse"]["toolUseId"])

            # Forward streaming chunks immediately as they arrive (streaming agents only)
            if event_dict.get("type") == "tool_stream":
                tool_stream = event_dict["tool_stream_event"]
                tool_use_id = tool_stream["tool_use"]["toolUseId"]
                if tool_use_id in a2a_tool_use_ids:
                    chunk = tool_stream["data"]
                    if isinstance(chunk, str) and chunk:
                        a2a_streamed_ids.add(tool_use_id)
                        yield _sse({
                            "type": "chunk",
                            "tool_use_id": tool_use_id,
                            "text": chunk,
                        })

            # ToolResultMessageEvent — authoritative final result for compliance logging.
            # For non-streaming agents (no prior chunks): yield the full content now.
            # For streaming agents (chunks already sent): skip to avoid duplicates.
            if msg.get("role") == "user":
                for block in msg.get("content", []):
                    if (
                        "toolResult" in block
                        and block["toolResult"]["toolUseId"] in a2a_tool_use_ids
                    ):
                        tool_use_id = block["toolResult"]["toolUseId"]
                        status = block["toolResult"].get("status")
                        logger.info(
                            "tool_use_id=<%s>, status=<%s>, streamed=<%s> | a2a complete result received",
                            tool_use_id,
                            status,
                            tool_use_id in a2a_streamed_ids,
                        )
                        if tool_use_id not in a2a_streamed_ids:
                            # Non-streaming agent — send the full result as a single chunk
                            for item in block["toolResult"].get("content", []):
                                if isinstance(item.get("text"), str) and item["text"]:
                                    yield _sse({
                                        "type": "chunk",
                                        "tool_use_id": tool_use_id,
                                        "text": item["text"],
                                    })

        yield _sse({"type": "done"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
