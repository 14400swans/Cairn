"""
Unit tests for Sentinel.process_findings' reflection-document write path
-- the human-readable save_document call added alongside the
machine-readable structured properties write, so a person skimming the
dataset page in the DataHub UI sees Cairn's contribution too, not only
an agent parsing the Props tab.

Run with: pytest tests/test_write_reflection.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cairn.agent import RawFinding, Sentinel
from cairn.capsule import Capsule, FindingType
from cairn.governance import GovernanceConfig, GovernanceGate
from cairn.mcp_client import DataHubMCPError

ENTITY_URN = "urn:li:dataset:(urn:li:dataPlatform:hive,SampleHiveDataset,PROD)"


def make_finding(confidence: float = 0.85) -> RawFinding:
    return RawFinding(
        entity_urn=ENTITY_URN,
        finding_type=FindingType.QUERY_DRIFT,
        confidence=confidence,
        summary="heavily queried, undocumented column: flag_override",
        unresolved_questions=["What does flag_override mean?"],
        assumptions_made=["Detected via word-boundary text match."],
    )


@pytest.fixture
def gate(tmp_path):
    config = GovernanceConfig(
        min_confidence_to_write=0.55, cooldown_hours=24, max_writes_per_run=10
    )
    return GovernanceGate(config=config, state_file=tmp_path / "state.json")


@pytest.mark.asyncio
async def test_high_confidence_finding_writes_both_structured_properties_and_document(gate):
    client = AsyncMock()
    sentinel = Sentinel(client=client, gate=gate)

    await sentinel.process_findings([make_finding()])

    client.add_structured_properties.assert_awaited_once()
    client.save_document.assert_awaited_once()

    _, kwargs = client.save_document.await_args
    assert kwargs["document_type"] == "Context"
    assert "SampleHiveDataset" in kwargs["title"]
    assert kwargs["related_assets"] == [ENTITY_URN]
    assert "cairn" in kwargs["topics"]
    assert "flag_override" in kwargs["content"]


@pytest.mark.asyncio
async def test_reflection_document_failure_does_not_undo_structured_property_write(gate):
    """
    save_document is a best-effort presentation layer. If it fails, the
    structured property write that already succeeded (and was already
    recorded in governance state) must not be rolled back or hidden --
    confirmed here by checking the cooldown now blocks a repeat write.
    """
    client = AsyncMock()
    client.save_document.side_effect = DataHubMCPError("boom")
    sentinel = Sentinel(client=client, gate=gate)

    await sentinel.process_findings([make_finding()])

    client.add_structured_properties.assert_awaited_once()

    repeat = Capsule(
        agent_id="cairn-sentinel-v1",
        entity_urn=ENTITY_URN,
        finding_type=FindingType.QUERY_DRIFT,
        confidence=0.85,
        summary="test",
    )
    decision = gate.evaluate(repeat)
    assert decision.allowed is False
    assert "cooldown" in decision.reason


@pytest.mark.asyncio
async def test_low_confidence_finding_never_reaches_save_document(gate):
    client = AsyncMock()
    sentinel = Sentinel(client=client, gate=gate)

    await sentinel.process_findings([make_finding(confidence=0.2)])

    client.add_structured_properties.assert_not_awaited()
    client.save_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_property_write_failure_skips_document_entirely(gate):
    """
    If the authoritative structured property write itself fails, Cairn
    must not proceed to save a reflection document about a finding it
    never actually recorded -- that would produce a visible document
    with no matching machine-readable backing.
    """
    client = AsyncMock()
    client.add_structured_properties.side_effect = DataHubMCPError("rejected")
    sentinel = Sentinel(client=client, gate=gate)

    await sentinel.process_findings([make_finding()])

    client.save_document.assert_not_awaited()