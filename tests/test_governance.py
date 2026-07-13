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
    capsule = make_capsule()
    payload = capsule.to_structured_properties()
    assert payload["urn"] == capsule.entity_urn
    props = payload["structured_properties"]
    assert props["io.cairn.confidence"] == capsule.confidence
    assert props["io.cairn.findingType"] == capsule.finding_type.value
