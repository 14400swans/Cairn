"""
probe_writeback.py — One-off diagnostic script.

Purpose: exercise the WRITE path (Sentinel.process_findings ->
GovernanceGate.evaluate -> add_structured_properties) end-to-end against
a real DataHub instance, using manually-constructed findings rather than
ones produced by inspect_dataset().

Why manual findings instead of a real drift/gap result: the healthcare/
quickstart sample pack doesn't contain a dataset with both undocumented
fields and heavy query traffic at the same time (verified 2026-07-13 —
see DEVELOPMENT_NOTES.md), and _find_documentation_gaps needs an
Anthropic API key that may not be configured yet. Rather than wait on
either of those, this tests the write mechanics directly and honestly:
does a real capsule actually land in DataHub via add_structured_properties,
and does the governance gate actually block a low-confidence one?

This WILL write to your DataHub instance if the high-confidence finding
clears the governance gate. Requirements before running:
  1. Structured property types registered:
       datahub properties upsert -f datahub/structured_properties.yaml
  2. MCP server restarted with mutations enabled:
       $env:TOOLS_IS_MUTATION_ENABLED="true"
       python -m uv tool run mcp-server-datahub@latest --transport http

Usage (from the project root, with .venv activated):

    python probe_writeback.py "urn:li:dataset:(urn:li:dataPlatform:hive,SampleHiveDataset,PROD)"
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from cairn.agent import Sentinel, RawFinding
from cairn.capsule import FindingType
from cairn.governance import GovernanceGate
from cairn.mcp_client import DataHubMCPClient, DataHubMCPError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main() -> None:
    load_dotenv()

    if len(sys.argv) < 2:
        print('Usage: python probe_writeback.py "urn:li:dataset:(...)"')
        sys.exit(1)

    urn = sys.argv[1]

    # A deliberately high-confidence finding — should clear the default
    # governance threshold (MIN_CONFIDENCE_TO_WRITE=0.55) and get written.
    high_confidence_finding = RawFinding(
        entity_urn=urn,
        finding_type=FindingType.QUERY_DRIFT,
        confidence=0.85,
        summary="[TEST] manually-constructed finding for write-path verification",
        unresolved_questions=[
            "This is a synthetic test finding, not a real drift result — "
            "used to verify Cairn's write mechanics against a live "
            "DataHub instance without depending on the sample data "
            "happening to contain a real drift scenario."
        ],
        assumptions_made=[
            "Constructed manually by probe_writeback.py, not produced by "
            "Sentinel.inspect_dataset()."
        ],
    )

    # A deliberately low-confidence finding — should be BLOCKED by the
    # governance gate. This is the "restraint is the point" moment
    # DEVELOPMENT_NOTES.md suggests showing on camera for the demo.
    low_confidence_finding = RawFinding(
        entity_urn=urn,
        finding_type=FindingType.DOCUMENTATION_GAP,
        confidence=0.2,
        summary="[TEST] low-confidence finding that should be skipped",
        unresolved_questions=["This should never appear in DataHub — the governance gate should block it."],
        assumptions_made=["Constructed manually to demonstrate the confidence threshold."],
    )

    print(f"Target dataset: {urn}\n")
    print("This will attempt TWO writes:")
    print(f"  1. High-confidence (0.85) finding — expected to WRITE")
    print(f"  2. Low-confidence (0.2) finding — expected to be SKIPPED\n")

    try:
        async with DataHubMCPClient() as client:
            gate = GovernanceGate()
            sentinel = Sentinel(client, gate)

            await sentinel.process_findings([high_confidence_finding, low_confidence_finding])

            print(f"\n{'=' * 70}")
            print("Done. Check the log lines above for WROTE / SKIPPED outcomes.")
            print(f"{'=' * 70}\n")
            print(
                "If you see a WROTE line, open this dataset's page in the "
                "DataHub UI (localhost:9002) and look for a "
                "'Properties' / 'Structured Properties' section — you "
                "should see io.cairn.* fields with the test values above."
            )

    except DataHubMCPError as exc:
        print(f"\nERROR (DataHubMCPError): {exc}")
        print(
            "\nCheck:\n"
            "  - is the MCP server running with TOOLS_IS_MUTATION_ENABLED=true?\n"
            "  - did you run `datahub properties upsert -f "
            "datahub/structured_properties.yaml` first? If not, "
            "add_structured_properties will fail because the io.cairn.* "
            "property types aren't registered yet.\n"
            "  - does DATAHUB_MCP_URL in .env match the running server's port?"
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())