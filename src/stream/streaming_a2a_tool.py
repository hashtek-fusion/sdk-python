"""Custom streaming A2A tool — extends PythonAgentTool without modifying the SDK.

Overrides stream() to use streaming=True on the A2A client, yielding intermediate
text chunks as ToolStreamEvents before the final ToolResultEvent.
"""

import logging
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import AgentCard, Message, Part, Role, TextPart

from strands.tools.tools import PythonAgentTool
from strands.types._events import ToolResultEvent
from strands.types.tools import ToolGenerator, ToolResult, ToolSpec, ToolUse

logger = logging.getLogger(__name__)

# Matches the original a2a_send_message tool spec so the LLM calls it identically
A2A_SEND_MESSAGE_TOOL_SPEC: ToolSpec = {
    "name": "a2a_send_message",
    "description": (
        "Send a message to a specific A2A agent and return the response. "
        "IMPORTANT: If the user provides a specific URL, use it directly. "
        "If the user refers to an agent by name only, use a2a_list_discovered_agents "
        "first to get the correct URL. Never guess, generate, or hallucinate URLs."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "message_text": {
                    "type": "string",
                    "description": "The message content to send to the agent",
                },
                "target_agent_url": {
                    "type": "string",
                    "description": "The exact URL of the target A2A agent",
                },
                "message_id": {
                    "type": "string",
                    "description": "Optional message ID for tracking",
                },
            },
            "required": ["message_text", "target_agent_url"],
        }
    },
}


class StreamingA2ASendMessageTool(PythonAgentTool):
    """Extends PythonAgentTool to stream A2A responses as ToolStreamEvents.

    Overrides stream() to use streaming=True on the A2A client and yield
    intermediate text chunks before the final ToolResultEvent.
    Each yielded non-ToolResultEvent becomes a ToolStreamEvent in _executor.py:248.
    """

    def __init__(self, httpx_client_args: dict[str, Any]) -> None:
        # Pass a no-op tool_func — stream() is fully overridden, _tool_func never called
        super().__init__(
            tool_name="a2a_send_message",
            tool_spec=A2A_SEND_MESSAGE_TOOL_SPEC,
            tool_func=lambda *_, **__: {},
        )
        self._httpx_client_args = httpx_client_args

    async def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> ToolGenerator:
        """Stream A2A response chunks immediately, then yield final ToolResultEvent.

        Each string yield becomes a ToolStreamEvent in _executor.py:248.
        The final ToolResultEvent is the authoritative result added to conversation history.
        """
        input_data = tool_use.get("input", {})
        message_text: str = input_data.get("message_text", "")
        target_url: str = input_data.get("target_agent_url", "")
        message_id: str = input_data.get("message_id") or uuid4().hex
        tool_use_id = str(tool_use.get("toolUseId", ""))

        accumulated: list[str] = []

        try:
            # Discover agent card with a short-lived client
            async with httpx.AsyncClient(**self._httpx_client_args) as discovery_client:
                resolver = A2ACardResolver(
                    httpx_client=discovery_client, base_url=target_url
                )
                agent_card: AgentCard = await resolver.get_agent_card()

            # Use streaming only if the agent card advertises the capability
            supports_streaming: bool = bool(
                getattr(agent_card, "capabilities", None)
                and getattr(agent_card.capabilities, "streaming", False)
            )
            streaming_client = httpx.AsyncClient(**self._httpx_client_args)
            config = ClientConfig(httpx_client=streaming_client, streaming=supports_streaming)
            factory = ClientFactory(config)
            client = factory.create(agent_card)

            message = Message(
                kind="message",
                role=Role.user,
                parts=[Part(TextPart(kind="text", text=message_text))],
                message_id=message_id,
            )

            async with streaming_client:
                async for event in client.send_message(message):
                    chunk = _extract_text_chunk(event)
                    if chunk:
                        accumulated.append(chunk)
                        yield chunk  # becomes ToolStreamEvent in _executor.py:248

            result: ToolResult = {
                "toolUseId": tool_use_id,
                "status": "success",
                "content": [{"text": "".join(accumulated)}],
            }

        except Exception as exc:
            logger.exception(
                "tool_use_id=<%s>, target_url=<%s> | streaming A2A call failed",
                tool_use_id,
                target_url,
            )
            result = {
                "toolUseId": tool_use_id,
                "status": "error",
                "content": [{"text": str(exc)}],
            }

        yield ToolResultEvent(result)  # executor breaks loop here, adds to conversation


def _extract_text_chunk(event: Any) -> str | None:
    """Extract incremental text from an A2A streaming event.

    With streaming=True, send_message yields (Task, UpdateEvent) tuples where
    Task artifacts accumulate text parts incrementally.

    Adjust this based on the actual a2a-python SDK streaming event shape
    if the A2A agent uses a different response structure.
    """
    if isinstance(event, tuple) and len(event) == 2:
        task, _ = event
        if hasattr(task, "artifacts") and task.artifacts:
            latest = task.artifacts[-1]
            if hasattr(latest, "parts") and latest.parts:
                part = latest.parts[-1]
                root = getattr(part, "root", part)
                return getattr(root, "text", None)

    # Direct Message response (non-streaming fallback)
    if hasattr(event, "parts") and event.parts:
        part = event.parts[-1]
        root = getattr(part, "root", part)
        return getattr(root, "text", None)

    return None
