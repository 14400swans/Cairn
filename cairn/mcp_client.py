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

import logging
import os
from typing import Any, Optional

import anyio
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


def _extract_result_text(result: Any) -> str:
    """
    Pull whatever human-readable text an MCP tool result carries, for
    use in an error message. Mirrors agent.py's own _structured()
    fallback logic (content blocks with a .text attribute), but doesn't
    try to parse it as JSON here — this is purely for logging/error
    text, not for further processing.
    """
    content = getattr(result, "content", None) or []
    texts = [getattr(block, "text", None) for block in content]
    texts = [t for t in texts if t]
    return "; ".join(texts) if texts else repr(result)


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
        """
        NOTE, added after two real bugs found running against a live
        DataHub instance:

        1. This used to wrap self._streams_ctx.__aenter__() and
           self._session.initialize() in asyncio.wait_for(). That looks
           harmless but isn't -- streamablehttp_client is anyio-based,
           and anyio's cancel scopes are tied to the specific asyncio
           Task they were opened in. asyncio.wait_for() runs its
           coroutine in a new, separate Task under the hood, so the
           cancel scope opened inside __aenter__() belonged to that
           short-lived wait_for task -- not to the outer task this
           object actually lives in.

        2. The fix for (1) was to swap in anyio.fail_after() -- but
           fail_after() has its own trap for exactly this method: it
           returns a CancelScope, and cancel scopes must close in
           strict last-opened-first-closed order within one task. Here,
           self._streams_ctx and self._session are deliberately opened
           but NOT closed inside __aenter__() -- they stay open for the
           client's whole lifetime and only close later, in
           __aexit__(). Wrapping their __aenter__() calls in a
           fail_after() scope means that scope tries to close (at the
           end of the `with` block) while the task group opened inside
           streamablehttp_client is still open -- anyio correctly
           refuses with "Attempted to exit a cancel scope that isn't
           the current task's current cancel scope". Scope-based
           timeouts (fail_after / move_on_after) only work for code
           that opens AND closes everything within the same block --
           not for a method that intentionally leaves resources open
           past it.

        The actual fix: don't wrap connection setup in any scope-based
        timeout here at all. streamablehttp_client already accepts its
        own `timeout` parameter, which it threads straight through to
        httpx's connect/read timeout -- that's the right layer for this,
        since it doesn't require anything to close before this method
        returns.
        """
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self._streams_ctx = streamablehttp_client(
            self.mcp_url, headers=headers, timeout=self.connect_timeout_seconds
        )
        try:
            read_stream, write_stream, _ = await self._streams_ctx.__aenter__()
            self._session = ClientSession(read_stream, write_stream)
            await self._session.__aenter__()
            await self._session.initialize()
        except Exception as exc:
            safe_message = _redact(str(exc), self.token)
            logger.error("Failed to connect to DataHub MCP server: %s", safe_message)
            raise DataHubMCPError(
                f"Could not connect to DataHub MCP server at {self.mcp_url}: "
                f"{safe_message}. Is `datahub docker quickstart` running, and "
                f"is TOOLS_IS_MUTATION_ENABLED set if you need writes?"
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
        timeout, transport failure, OR a tool-level error -- with any
        token value stripped from the message first.

        IMPORTANT: the MCP protocol reports a tool-side failure (e.g. a
        server-side validation error) as a normal CallToolResult with
        isError=True and an HTTP 200 -- it does NOT raise at the
        transport layer. Without checking isError here, a real write
        failure (DataHub rejected the payload) would look like success
        to callers: agent.py would log "WROTE" and governance.py's
        cooldown state would get recorded for a write that never
        actually happened. Checking isError here is what makes that log
        line trustworthy.

        NOTE: this used to wrap self._session.call_tool() in
        asyncio.wait_for(), same as __aenter__() originally did -- and
        for the same reason it's wrong here too. call_tool() awaits on
        the session's underlying anyio memory-object streams, which
        belong to the cancel scope opened back in __aenter__() on THIS
        task. Running the await inside a asyncio.wait_for()-spawned Task
        risks the exact same "different task" cancel-scope mismatch,
        just triggered by a tool call instead of connection teardown.
        anyio.fail_after() keeps everything on the one task this client
        object lives on, from connect through every call through
        __aexit__.
        """
        if self._session is None:
            raise DataHubMCPError(
                "DataHubMCPClient used outside of `async with` -- no active session"
            )
        try:
            with anyio.fail_after(self.call_timeout_seconds):
                result = await self._session.call_tool(tool_name, arguments)
        except TimeoutError as exc:
            # anyio.fail_after() raises the built-in TimeoutError (not
            # asyncio.TimeoutError specifically) on expiry.
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

        if getattr(result, "isError", False):
            error_text = _redact(_extract_result_text(result), self.token)
            logger.error("Tool call `%s` returned an error: %s", tool_name, error_text)
            raise DataHubMCPError(
                f"Tool call `{tool_name}` returned an error: {error_text}"
            )

        return result

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
        self, property_values: dict, entity_urns: list
    ) -> Any:
        """
        Thin pass-through: capsule.py's to_structured_properties() already
        builds the exact payload the real mcp-server-datahub
        add_structured_properties tool expects -- property_values keyed
        by full structured property URNs (urn:li:structuredProperty:...)
        with each value list-wrapped, and entity_urns as a list. This
        method used to re-derive that translation itself from a
        capsule.py that returned short qualified names, but capsule.py
        was updated to do the translation directly -- keeping it there
        means there is exactly one place in the codebase that knows the
        real tool's argument shape, instead of two that have to agree.
        """
        return await self.call(
            "add_structured_properties",
            {"property_values": property_values, "entity_urns": entity_urns},
        )

    async def update_description(self, urn: str, description: str) -> Any:
        return await self.call(
            "update_description", {"urn": urn, "description": description}
        )

    async def save_document(
        self,
        document_type: str,
        title: str,
        content: str,
        urn: Optional[str] = None,
        topics: Optional[list[str]] = None,
        related_documents: Optional[list[str]] = None,
        related_assets: Optional[list[str]] = None,
    ) -> Any:
        """
        NOTE: parameter names for the underlying `save_document` MCP tool
        (document_type / topics / related_assets / related_documents)
        match what agent.py's _write_reflection_document sends and what
        test_write_reflection.py pins down. There is deliberately no
        `parent_folder` parameter here -- an earlier draft of this
        method invented one; the real tool is understood to place saved
        documents under an automatically-managed parent folder
        server-side. As with add_structured_properties above, confirm
        this against your own running instance's tool schema (e.g. via
        your MCP client's list_tools()) before a demo.

        `related_assets` is what's expected to make a saved document
        appear on a dataset's own page in the DataHub UI (not just
        findable via search) -- pass the entity URN there to link the
        document back to the asset it describes.
        """
        arguments: dict[str, Any] = {
            "document_type": document_type,
            "title": title,
            "content": content,
        }
        if urn is not None:
            arguments["urn"] = urn
        if topics is not None:
            arguments["topics"] = topics
        if related_documents is not None:
            arguments["related_documents"] = related_documents
        if related_assets is not None:
            arguments["related_assets"] = related_assets
        return await self.call("save_document", arguments)