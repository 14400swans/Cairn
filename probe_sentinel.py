"""
probe_sentinel.py — One-off diagnostic script.

Purpose: run Sentinel.inspect_dataset() directly against a real dataset,
using the already-running self-hosted DataHub MCP server, to confirm
_find_query_drift produces real findings from real data.

Usage (from the project root, with .venv activated, MCP server already
running via `uv tool run mcp-server-datahub@latest --transport http`):

    python probe_sentinel.py "urn:li:dataset:(urn:li:dataPlatform:hive,SampleHiveDataset,PROD)"

Does NOT call process_findings() / write anything back to DataHub — this
only exercises the read + reasoning path (inspect_dataset), so it's safe
to run regardless of TOOLS_IS_MUTATION_ENABLED.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from cairn.agent import Sentinel
from cairn.governance import GovernanceGate
from cairn.mcp_client import DataHubMCPClient, DataHubMCPError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main() -> None:
    load_dotenv()

    if len(sys.argv) < 2:
        print('Usage: python probe_sentinel.py "urn:li:dataset:(...)"')
        sys.exit(1)

    urn = sys.argv[1]

    print(f"Inspecting dataset: {urn}\n")

    try:
        async with DataHubMCPClient() as client:
            gate = GovernanceGate()
            sentinel = Sentinel(client, gate)

            findings = await sentinel.inspect_dataset(urn)

            print(f"\n{'=' * 70}")
            print(f"Sentinel produced {len(findings)} raw finding(s)")
            print(f"{'=' * 70}\n")

            if not findings:
                print(
                    "No findings. This is expected if every field in this "
                    "dataset already has a description, or if none of the "
                    "fields are referenced often enough in the known queries "
                    "to clear the QUERY_DRIFT_MIN_FRACTION threshold."
                )
                return

            for i, finding in enumerate(findings, start=1):
                print(f"--- Finding {i} ---")
                print(f"  entity_urn:  {finding.entity_urn}")
                print(f"  type:        {finding.finding_type.value}")
                print(f"  confidence:  {finding.confidence}")
                print(f"  summary:     {finding.summary}")
                print(f"  questions:")
                for q in finding.unresolved_questions:
                    print(f"    - {q}")
                print(f"  assumptions:")
                for a in finding.assumptions_made:
                    print(f"    - {a}")
                print()

            print(
                "Note: this script does NOT call process_findings(), so "
                "nothing was written back to DataHub, regardless of the "
                "governance gate or TOOLS_IS_MUTATION_ENABLED."
            )

    except DataHubMCPError as exc:
        print(f"\nERROR (DataHubMCPError): {exc}")
        print(
            "\nCheck:\n"
            "  - is the MCP server still running (the `uv tool run "
            "mcp-server-datahub@latest --transport http` terminal)?\n"
            "  - does DATAHUB_MCP_URL in .env match that server's port?"
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())