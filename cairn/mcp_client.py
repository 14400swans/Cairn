"""
mcp_client.py — Thin wrapper around the DataHub MCP Server.

This wraps the official `mcp` Python client so the rest of Cairn's code
never has to think about transport details (stdio vs. streamable HTTP)
or raw MCP tool-call plumbing.

HONESTY NOTE: MCP transport setup varies by how you've deployed the
DataHub MCP Server (local quickstart vs. DataHub Cloud vs. self-hosted
behind auth). The HTTP path below is the common case for the quickstart
and is a reasonable default, but confirm against the DataHub MCP Server
Guide for your specific setup before relying on this for a demo.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger("cairn.mcp_client")


def _redact(text: str, secret: str) -> str:
    """Replace any occurrence of `secret` in `text` before it is logged.

    Used so a connection error that happens to echo back the request
    (including the Authorization header) never puts a live token into
    logs, a terminal recording, or a demo video.
    """
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


class DataHubMCPError(RuntimeError):
    """Raised when a DataHub MCP tool call fails or times out.

    Deliberately a distinct exception type (rather than letting raw
    connection errors propagate) so calling code -- and anyone reading a
    stack trace during a live demo -- sees a clear, non-secret-leaking
    message instead of a raw transport exception.
    """


class DataHubMCPClient:
    """
    Async context manager wrapping a DataHub MCP Server connection.

    Usage:
        async with DataHubMCPClient() as client:
            results = await client.call("search", {"query": "healthcare"})
    """

    def __init__(
        self,
        mcp_url: Optional[str] = None,
        token: Optional[str] = None,
        connect_timeout_seconds: float = 15.0,
        call_timeout_seconds: float = 30.0,
    ):
        self.mcp_url: str = mcp_url if mcp_url else os.getenv(
            "DATAHUB_MCP_URL", "http://localhost:8080/mcp"
        )
        self.token: str = token if token else os.getenv(
            "DATAHUB_PERSONAL_ACCESS_TOKEN", ""
        )
        self.connect_timeout_seconds = connect_timeout_seconds
        self.call_timeout_seconds = call_timeout_seconds
        self._session: Optional[ClientSession] = None
        self._streams_ctx: Optional[Any] = None

    async def __aenter__(self) -> "DataHubMCPClient":
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self._streams_ctx = streamablehttp_client(self.mcp_url, headers=headers)
        try:
            read_stream, write_stream, _ = await asyncio.wait_for(
                self._streams_ctx.__aenter__(), timeout=self.connect_timeout_seconds
            )
            self._session = ClientSession(read_stream, write_stream)
            await self._session.__aenter__()
            await asyncio.wait_for(
                self._session.initialize(), timeout=self.connect_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise DataHubMCPError(
                f"Timed out connecting to DataHub MCP server at {self.mcp_url} "
                f"after {self.connect_timeout_seconds}s. Is `datahub docker "
                f"quickstart` running, and is TOOLS_IS_MUTATION_ENABLED set if "
                f"you need writes?"
            ) from exc
        except Exception as exc:
            safe_message = _redact(str(exc), self.token)
            logger.error("Failed to connect to DataHub MCP server: %s", safe_message)
            raise DataHubMCPError(
                f"Could not connect to DataHub MCP server at {self.mcp_url}: "
                f"{safe_message}"
            ) from exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.__aexit__(exc_type, exc, tb)
        if self._streams_ctx is not None:
            await self._streams_ctx.__aexit__(exc_type, exc, tb)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call one DataHub MCP tool and return its parsed result.

        Raises DataHubMCPError (never a raw transport exception) on
        timeout or failure, with any token value stripped from the
        message first.
        """
        if self._session is None:
            raise DataHubMCPError(
                "DataHubMCPClient used outside of `async with` -- no active session"
            )
        try:
            return await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=self.call_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise DataHubMCPError(
                f"Tool call `{tool_name}` timed out after "
                f"{self.call_timeout_seconds}s"
            ) from exc
        except Exception as exc:
            safe_message = _redact(str(exc), self.token)
            logger.error("Tool call `%s` failed: %s", tool_name, safe_message)
            raise DataHubMCPError(
                f"Tool call `{tool_name}` failed: {safe_message}"
            ) from exc

    # --- Convenience wrappers around the read-only tools Sentinel needs ---

    async def search(self, query: str, **kwargs) -> Any:
        return await self.call("search", {"query": query, **kwargs})

    async def get_entities(self, urns: list[str]) -> Any:
        return await self.call("get_entities", {"urns": urns})

    async def list_schema_fields(self, urn: str) -> Any:
        return await self.call("list_schema_fields", {"urn": urn})

    async def get_lineage(self, urn: str, **kwargs) -> Any:
        return await self.call("get_lineage", {"urn": urn, **kwargs})

    async def get_lineage_paths_between(self, source_urn: str, target_urn: str) -> Any:
        return await self.call(
            "get_lineage_paths_between",
            {"source_urn": source_urn, "target_urn": target_urn},
        )

    async def get_dataset_queries(self, urn: str) -> Any:
        return await self.call("get_dataset_queries", {"urn": urn})

    # --- Write tools — only ever called through governance.GovernanceGate ---

    async def add_structured_properties(
        self, property_values: dict, entity_urns: list[str]
    ) -> Any:
        """
        Matches the real mcp-server-datahub add_structured_properties
        tool signature exactly (verified 2026-07-13 against
        mcp-server-datahub==0.6.0 source):

            add_structured_properties(
                property_values: Dict[str, List[Union[str, float, int]]],
                entity_urns: List[str],
            )

        property_values must be keyed by FULL structured property URNs
        with each value list-wrapped, and entity_urns is always a list
        even for a single entity. Callers should pass
        capsule.to_structured_properties()'s two dict keys straight
        through — see Sentinel.process_findings in agent.py.
        """
        return await self.call(
            "add_structured_properties",
            {"property_values": property_values, "entity_urns": entity_urns},
        )

    async def update_description(self, urn: str, description: str) -> Any:
        return await self.call(
            "update_description", {"urn": urn, "description": description}
        )

    async def save_document(self, title: str, content: str, parent_folder: str) -> Any:
        return await self.call(
            "save_document",
            {"title": title, "content": content, "parent_folder": parent_folder},
        )