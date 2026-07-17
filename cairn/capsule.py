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
        {"urn": ..., "structured_properties": {...}} shape that didn't
        match the real tool schema at all and failed silently.

        SECOND ISSUE, ALSO VERIFIED 2026-07-13 via a live write attempt
        against a real DataHub instance: `io.cairn.sessionTimestamp` is
        registered in datahub/structured_properties.yaml as a `date`
        type property, and DataHub validates that strictly as
        YYYY-MM-DD -- a full ISO 8601 datetime (with time and timezone,
        which self.session_ts intentionally carries for cooldown
        precision and ordering elsewhere) is rejected server-side with
        "should be a date with format YYYY-MM-DD". self.session_ts
        itself is left untouched everywhere else (JSON round-trip,
        GovernanceGate cooldown math, previous_capsule_for ordering) --
        only the value sent to THIS specific structured property is
        truncated to its date portion.
        """
        prefix = "urn:li:structuredProperty:io.cairn."
        session_date = datetime.fromisoformat(self.session_ts).date().isoformat()

        property_values: dict = {
            f"{prefix}agentId": [self.agent_id],
            f"{prefix}sessionTimestamp": [session_date],
            f"{prefix}confidence": [round(self.confidence, 2)],
            f"{prefix}findingType": [self.finding_type.value],
            f"{prefix}summary": [self.summary],
            f"{prefix}requiresHumanReview": [str(self.requires_human_review).lower()],
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