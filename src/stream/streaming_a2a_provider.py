"""ToolProvider that wires StreamingA2ASendMessageTool into a Strands agent.

Wraps A2AClientToolProvider — replaces a2a_send_message with the streaming
version while keeping all other tools (a2a_discover_agent,
a2a_list_discovered_agents) from the original provider unchanged.
"""

import logging
from collections.abc import Sequence
from typing import Any

from strands_tools.a2a_client import A2AClientToolProvider
from strands.tools.tool_provider import ToolProvider
from strands.types.tools import AgentTool

from .streaming_a2a_tool import StreamingA2ASendMessageTool

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300


class StreamingA2AClientToolProvider(ToolProvider):
    """Wraps A2AClientToolProvider, replacing a2a_send_message with a streaming version.

    All other tools (a2a_discover_agent, a2a_list_discovered_agents) are taken
    directly from the original provider without modification.
    """

    def __init__(
        self,
        known_agent_urls: list[str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        httpx_client_args: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the streaming provider.

        Args:
            known_agent_urls: List of A2A agent base URLs to pre-discover.
            timeout: HTTP timeout in seconds applied to all A2A calls.
            httpx_client_args: Extra kwargs forwarded to httpx.AsyncClient
                (e.g. auth headers, proxies). Timeout is added automatically
                if not already present.
        """
        self._base_provider = A2AClientToolProvider(
            known_agent_urls=known_agent_urls,
            timeout=timeout,
            httpx_client_args=httpx_client_args,
        )
        self._httpx_client_args: dict[str, Any] = {
            "timeout": timeout,
            **(httpx_client_args or {}),
        }
        self._consumers: set[Any] = set()

    async def load_tools(self, **kwargs: Any) -> Sequence[AgentTool]:
        """Load tools: non-send tools from base provider + streaming send tool.

        Returns:
            Sequence of AgentTool instances ready for registration.
        """
        # Keep all original tools except a2a_send_message
        original_tools = [
            t for t in self._base_provider.tools
            if t.tool_spec["name"] != "a2a_send_message"
        ]

        streaming_send = StreamingA2ASendMessageTool(
            httpx_client_args=self._httpx_client_args,
        )

        logger.debug(
            "loaded_tools=<%s> | streaming A2A provider ready",
            [t.tool_spec["name"] for t in [*original_tools, streaming_send]],
        )

        return [*original_tools, streaming_send]

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        """Register a consumer (agent) using this provider.

        Args:
            consumer_id: Unique identifier for the consuming agent.
        """
        self._consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        """Deregister a consumer. Idempotent — safe to call multiple times.

        Args:
            consumer_id: Unique identifier for the consuming agent.
        """
        self._consumers.discard(consumer_id)
