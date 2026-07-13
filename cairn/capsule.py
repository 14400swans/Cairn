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
        Shape this capsule as a payload for DataHub's
        add_structured_properties MCP tool.

        NOTE: DataHub structured properties must be defined (their
        qualified names + types registered) before they can be set on
        an entity. See DEVELOPMENT_NOTES.md for the property definitions
        this scaffold assumes you will register once, up front, via
        `datahub-skills:datahub-enrich` or the DataHub UI.
        """
        return {
            "urn": self.entity_urn,
            "structured_properties": {
                "io.cairn.agentId": self.agent_id,
                "io.cairn.sessionTimestamp": self.session_ts,
                "io.cairn.confidence": round(self.confidence, 2),
                "io.cairn.findingType": self.finding_type.value,
                "io.cairn.summary": self.summary,
                "io.cairn.unresolvedQuestions": self.unresolved_questions,
                "io.cairn.assumptionsMade": self.assumptions_made,
                "io.cairn.requiresHumanReview": self.requires_human_review,
            },
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
