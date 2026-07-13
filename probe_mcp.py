"""
probe_mcp.py — One-off diagnostic script.

Purpose: verify that Cairn's own mcp_client.py can connect to the DataHub
MCP Server over HTTP transport, and print the RAW response shape for
list_schema_fields and get_dataset_queries against one real dataset. This
lets _find_query_drift be written against the actual field names instead
of guessing.

Usage (from the project root, with .venv activated):

    python probe_mcp.py "urn:li:dataset:(urn:li:dataPlatform:hive,SampleHiveDataset,PROD)"

If no URN is given as an argument, the script first runs a search call to
list a few candidate datasets.

Uses ONLY read-only methods (search, list_schema_fields,
get_dataset_queries) — does not touch the write path.
"""

from __future__ import annotations

import asyncio
import json
import sys

from dotenv import load_dotenv

from cairn.mcp_client import DataHubMCPClient, DataHubMCPError


def _dump(label: str, obj) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    try:
        # mcp_tool_result objects aren't always directly JSON-serializable,
        # so fall back to repr() if json.dumps chokes on it.
        print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))
    except Exception:
        print(repr(obj))


async def main() -> None:
    load_dotenv()

    urn_arg = sys.argv[1] if len(sys.argv) > 1 else None

    print("Connecting to DataHub MCP server...")
    try:
        async with DataHubMCPClient() as client:
            print(f"  -> Connected OK: {client.mcp_url}")

            urn = urn_arg
            if not urn:
                print("\nNo URN given — fetching a candidate via search()...")
                search_result = await client.search("dataset")
                _dump("search('dataset') -- raw response", search_result)
                print(
                    "\nCopy one URN from the response above and re-run the "
                    "script with it as an argument:\n"
                    "  python probe_mcp.py \"urn:li:dataset:(...)\""
                )
                return

            print(f"\nUsing URN: {urn}")

            entity = await client.get_entities([urn])
            _dump("get_entities([urn]) -- raw response", entity)

            schema_fields = await client.list_schema_fields(urn)
            _dump("list_schema_fields(urn) -- raw response", schema_fields)

            queries = await client.get_dataset_queries(urn)
            _dump("get_dataset_queries(urn) -- raw response", queries)

            print(
                "\n\nDone. Paste the output above to Claude so "
                "_find_query_drift can be written against these exact field names."
            )

    except DataHubMCPError as exc:
        print(f"\nERROR (DataHubMCPError): {exc}")
        print(
            "\nCheck:\n"
            "  - is the DataHub quickstart still running (docker ps)?\n"
            "  - does DATAHUB_MCP_URL in .env match the server's port?\n"
            "  - if GMS requires auth, is DATAHUB_PERSONAL_ACCESS_TOKEN set?"
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())