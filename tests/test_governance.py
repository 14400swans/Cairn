"""
Unit tests for the governance gate and capsule — the two modules that
are fully implemented and don't require a live DataHub connection.
Run with: pytest tests/
"""

import pytest

from cairn.capsule import Capsule, FindingType
from cairn.governance import GovernanceConfig, GovernanceGate


def make_capsule(confidence=0.8, urn="urn:li:dataset:(test,a,PROD)") -> Capsule:
    return Capsule(
        agent_id="test-agent",
        entity_urn=urn,
        finding_type=FindingType.QUERY_DRIFT,
        confidence=confidence,
        summary="test finding",
    )


@pytest.fixture
def gate(tmp_path):
    state_file = tmp_path / "state.json"
    config = GovernanceConfig(
        min_confidence_to_write=0.6, cooldown_hours=24, max_writes_per_run=2
    )
    return GovernanceGate(config=config, state_file=state_file)


def test_low_confidence_is_blocked(gate):
    capsule = make_capsule(confidence=0.3)
    decision = gate.evaluate(capsule)
    assert decision.allowed is False
    assert "confidence" in decision.reason


def test_high_confidence_is_allowed(gate):
    capsule = make_capsule(confidence=0.9)
    decision = gate.evaluate(capsule)
    assert decision.allowed is True


def test_cooldown_blocks_repeat_write(gate):
    capsule = make_capsule(confidence=0.9, urn="urn:li:dataset:(test,repeat,PROD)")
    assert gate.evaluate(capsule).allowed is True
    gate.record(capsule)

    capsule_again = make_capsule(confidence=0.9, urn="urn:li:dataset:(test,repeat,PROD)")
    decision = gate.evaluate(capsule_again)
    assert decision.allowed is False
    assert "cooldown" in decision.reason


def test_max_writes_per_run_is_enforced(gate):
    for i in range(2):
        c = make_capsule(confidence=0.9, urn=f"urn:li:dataset:(test,n{i},PROD)")
        assert gate.evaluate(c).allowed is True
        gate.record(c)

    one_too_many = make_capsule(confidence=0.9, urn="urn:li:dataset:(test,overflow,PROD)")
    decision = gate.evaluate(one_too_many)
    assert decision.allowed is False
    assert "limit" in decision.reason


def test_capsule_roundtrips_through_json():
    original = make_capsule()
    restored = Capsule.from_json(original.to_json())
    assert restored.entity_urn == original.entity_urn
    assert restored.finding_type == original.finding_type
    assert restored.confidence == original.confidence


def test_capsule_structured_properties_shape():
    """
    Verified 2026-07-13 against mcp-server-datahub==0.6.0's real
    add_structured_properties tool source: property_values is keyed by
    FULL structured property URNs, each value list-wrapped, and
    entity_urns is a separate list. This replaces an earlier version of
    this test that pinned down a {"urn": ..., "structured_properties":
    {...}} shape which looked reasonable but didn't match what the live
    MCP server actually accepts.
    """
    capsule = make_capsule()
    payload = capsule.to_structured_properties()

    assert payload["entity_urns"] == [capsule.entity_urn]

    props = payload["property_values"]
    assert props["urn:li:structuredProperty:io.cairn.confidence"] == [capsule.confidence]
    assert props["urn:li:structuredProperty:io.cairn.findingType"] == [
        capsule.finding_type.value
    ]
    assert props["urn:li:structuredProperty:io.cairn.agentId"] == [capsule.agent_id]
    assert props["urn:li:structuredProperty:io.cairn.summary"] == [capsule.summary]


def test_capsule_session_timestamp_is_date_only():
    """
    Pins down a real bug found 2026-07-13 via a live write against
    DataHub: io.cairn.sessionTimestamp is registered as a `date` type
    structured property, which DataHub validates strictly as
    YYYY-MM-DD. Sending the full ISO 8601 datetime that Capsule.session_ts
    carries (with time and timezone, used elsewhere for cooldown
    precision) was rejected server-side with "should be a date with
    format YYYY-MM-DD". This test confirms the value sent for THIS
    property is truncated to just the date portion, while
    capsule.session_ts itself stays a full datetime.
    """
    capsule = Capsule(
        agent_id="test-agent",
        entity_urn="urn:li:dataset:(test,date,PROD)",
        finding_type=FindingType.QUERY_DRIFT,
        confidence=0.8,
        summary="test finding",
        session_ts="2026-07-13T17:44:59.604804+00:00",
    )
    payload = capsule.to_structured_properties()
    props = payload["property_values"]

    assert props["urn:li:structuredProperty:io.cairn.sessionTimestamp"] == ["2026-07-13"]
    # The dataclass field itself must remain the full datetime — other
    # code (GovernanceGate cooldown math, previous_capsule_for ordering)
    # depends on the time-of-day precision.
    assert capsule.session_ts == "2026-07-13T17:44:59.604804+00:00"