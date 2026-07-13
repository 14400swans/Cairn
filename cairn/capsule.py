"""
capsule.py — The structured "handoff capsule" that Cairn leaves behind.

A capsule is deliberately small and machine-readable. It is not a
replacement for a human-facing description; it is a companion artifact
any downstream agent can parse without an LLM call, so context survives
the handoff even between two very different agents.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class FindingType(str, Enum):
    DOCUMENTATION_GAP = "documentation_gap"
    QUERY_DRIFT = "query_drift"
    LINEAGE_BREAK = "lineage_break"
    OWNERSHIP_STALE = "ownership_stale"


@dataclass
class Capsule:
    """A single structured handoff capsule for one entity (dataset/column)."""

    agent_id: str
    entity_urn: str
    finding_type: FindingType
    confidence: float  # 0.0 - 1.0
    summary: str  # short, human-skimmable — aim for a handful of words
    unresolved_questions: list[str] = field(default_factory=list)
    assumptions_made: list[str] = field(default_factory=list)
    requires_human_review: bool = True
    session_ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_structured_properties(self) -> dict:
        """
        Shape this capsule as a payload for DataHub's real
        add_structured_properties MCP tool.

        VERIFIED 2026-07-13 against mcp-server-datahub==0.6.0 source
        (tools/structured_properties.py): the tool's actual signature is

            add_structured_properties(
                property_values: Dict[str, List[Union[str, float, int]]],
                entity_urns: List[str],
            )

        property_values keys must be FULL structured property URNs
        (e.g. "urn:li:structuredProperty:io.cairn.confidence"), and
        every value — even a single one — must be wrapped in a list.

        An earlier version of this method produced a flat
        {"urn": ..., "structured_properties": {...}} shape that looked
        reasonable but was never checked against the real tool schema.
        It failed silently server-side with an "Invalid arguments"
        warning (missing `urn`, unexpected `structured_properties`)
        while Cairn's own code still logged "WROTE", because the old
        add_structured_properties() call didn't raise on that response.
        Confirmed empirically: a write with the old shape produced no
        visible property in the DataHub UI's Props tab; this fixed
        shape is what the live tool source actually expects.
        """
        prefix = "urn:li:structuredProperty:io.cairn."
        property_values: dict = {
            f"{prefix}agentId": [self.agent_id],
            f"{prefix}sessionTimestamp": [self.session_ts],
            f"{prefix}confidence": [round(self.confidence, 2)],
            f"{prefix}findingType": [self.finding_type.value],
            f"{prefix}summary": [self.summary],
            f"{prefix}requiresHumanReview": [str(self.requires_human_review)],
        }
        # Multi-valued, optional properties: omit rather than send an
        # empty list, since the property's value-type validation may
        # reject an empty values list.
        if self.unresolved_questions:
            property_values[f"{prefix}unresolvedQuestions"] = list(
                self.unresolved_questions
            )
        if self.assumptions_made:
            property_values[f"{prefix}assumptionsMade"] = list(self.assumptions_made)

        return {
            "entity_urns": [self.entity_urn],
            "property_values": property_values,
        }

    def to_json(self) -> str:
        payload = asdict(self)
        payload["finding_type"] = self.finding_type.value
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Capsule":
        data = json.loads(raw)
        data["finding_type"] = FindingType(data["finding_type"])
        return cls(**data)


def previous_capsule_for(entity_urn: str, capsules: list[Capsule]) -> Optional[Capsule]:
    """
    Given a list of capsules already read back from DataHub for this
    entity (e.g. via get_entities -> structured_properties), return the
    most recent one, or None. Used by the governance gate for cooldown
    checks and by the Sentinel to avoid re-raising a question it already
    asked.
    """
    matching = [c for c in capsules if c.entity_urn == entity_urn]
    if not matching:
        return None
    return max(matching, key=lambda c: c.session_ts)