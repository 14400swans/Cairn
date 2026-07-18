"""
inspect_mcp_shapes.py — one-off diagnostic to see the REAL shape of the
DataHub MCP tool responses that agent.py's strategies parse, instead of
guessing at field names.

Run from the project root (same place you run `python -m cairn.cli`):

    python inspect_mcp_shapes.py

Requires the DataHub MCP server to already be running (same as cairn.cli).
"""

from __future__ import annotations

import asyncio
import json

from dotenv import load_dotenv

from cairn.mcp_client import DataHubMCPClient

URN = "urn:li:dataset:(urn:li:dataPlatform:hive,logging_events,PROD)"


def _dump(label: str, result) -> None:
    print(f"\n{'=' * 20} {label} {'=' * 20}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        print("structuredContent:")
        print(json.dumps(structured, indent=2, default=str)[:3000])
        return
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            print("content[].text (attempting JSON parse):")
            try:
                print(json.dumps(json.loads(text), indent=2, default=str)[:3000])
            except (json.JSONDecodeError, TypeError):
                print(text[:3000])
            return
    print("NOTHING RECOGNIZABLE — raw repr:")
    print(repr(result)[:3000])


async def main() -> None:
    load_dotenv()
    async with DataHubMCPClient() as client:
        entity = await client.get_entities([URN])
        _dump("get_entities", entity)

        schema_fields = await client.list_schema_fields(URN)
        _dump("list_schema_fields", schema_fields)

        queries = await client.get_dataset_queries(URN)
        _dump("get_dataset_queries", queries)


if __name__ == "__main__":
    asyncio.run(main())