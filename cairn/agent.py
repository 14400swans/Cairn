"""
agent.py — The Sentinel: Cairn's core inspection loop.
(uploaded version, for testing purposes)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from .capsule import Capsule, FindingType
from .governance import GovernanceGate
from .mcp_client import DataHubMCPClient, DataHubMCPError

logger = logging.getLogger("cairn.agent")

AGENT_ID = "cairn-sentinel-v1"

QUERY_DRIFT_MIN_FRACTION = 0.3
DOC_GAP_MIN_CONFIDENCE = 0.3

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]


@dataclass
class RawFinding:
    entity_urn: str
    finding_type: FindingType
    confidence: float
    summary: str
    unresolved_questions: list[str]
    assumptions_made: list[str]


def _structured(result: Any) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue

    logger.warning("Could not extract structured content from MCP result: %r", result)
    return {}


class Sentinel:
    def __init__(self, client: DataHubMCPClient, gate: GovernanceGate):
        self.client = client
        self.gate = gate

    async def inspect_dataset(self, urn: str) -> list[RawFinding]:
        findings: list[RawFinding] = []

        entity = await self.client.get_entities([urn])
        schema_fields = await self.client.list_schema_fields(urn)
        queries = await self.client.get_dataset_queries(urn)
        lineage = await self.client.get_lineage(urn)

        findings.extend(self._find_documentation_gaps(urn, entity, schema_fields, lineage))
        findings.extend(self._find_query_drift(urn, schema_fields, queries))

        return findings

    def _find_documentation_gaps(
        self, urn: str, entity, schema_fields, lineage=None
    ) -> list[RawFinding]:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.info("documentation_gap: skipping %s (ANTHROPIC_API_KEY not set)", urn)
            return []

        if anthropic is None:
            logger.warning("documentation_gap: skipping %s (anthropic not installed)", urn)
            return []

        entity_results = _structured(entity).get("result", [])
        if not entity_results:
            logger.info("documentation_gap: skipping %s (no entity data returned)", urn)
            return []

        description = (entity_results[0].get("description") or "").strip()
        if not description:
            logger.info("documentation_gap: skipping %s (no existing description)", urn)
            return []

        fields = _structured(schema_fields).get("fields", [])
        field_summary = ", ".join(f.get("fieldPath", "?") for f in fields) or "(no fields returned)"

        lineage_data = _structured(lineage) if lineage is not None else {}
        lineage_summary = (
            json.dumps(lineage_data, default=str)[:2000]
            if lineage_data
            else "(no lineage data available)"
        )

        prompt = (
            "You are reviewing metadata documentation for a dataset in a "
            "data catalog.\n\n"
            f"Current description: {description!r}\n\n"
            f"Current schema fields: {field_summary}\n\n"
            f"Upstream/downstream lineage (raw JSON, may be partial or "
            f"empty): {lineage_summary}\n\n"
            "Does the description still accurately describe what this "
            "dataset contains and where it comes from, based on the "
            "schema and lineage above? Respond with ONLY a JSON object, "
            "no other text, in exactly this shape:\n"
            '{"confidence": <float 0.0-1.0, your confidence the '
            'description is CURRENT and accurate>, "gap": <string, the '
            "specific mismatch you found, or empty string if none>}"
        )

        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = "".join(
                block.text
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            parsed = json.loads(raw_text)
            confidence_description_is_current = float(parsed.get("confidence", 1.0))
            gap = (parsed.get("gap") or "").strip()
        except Exception as exc:
            logger.warning("documentation_gap: LLM call/parsing failed for %s: %s", urn, exc)
            return []

        finding_confidence = round(1.0 - confidence_description_is_current, 2)

        if not gap or finding_confidence < DOC_GAP_MIN_CONFIDENCE:
            logger.info("documentation_gap: %s — no significant gap found", urn)
            return []

        return [
            RawFinding(
                entity_urn=urn,
                finding_type=FindingType.DOCUMENTATION_GAP,
                confidence=finding_confidence,
                summary="description may be stale relative to schema/lineage",
                unresolved_questions=[gap],
                assumptions_made=[
                    f"Used {model} to compare the existing description "
                    f"against current schema fields and lineage; did not "
                    f"verify against the underlying source system's own "
                    f"documentation."
                ],
            )
        ]

    def _find_query_drift(self, urn: str, schema_fields, queries) -> list[RawFinding]:
        fields = _structured(schema_fields).get("fields", [])
        query_list = _structured(queries).get("queries", [])
        total_queries = len(query_list)

        if not fields or total_queries == 0:
            logger.info("query_drift: skipping %s (no schema fields or no queries)", urn)
            return []

        field_hit_counts: dict[str, int] = {f["fieldPath"]: 0 for f in fields}
        for query in query_list:
            statement = (query.get("properties") or {}).get("statement") or {}
            sql_text = (statement.get("value") or "").lower()
            if not sql_text:
                continue
            for field in fields:
                field_path = field["fieldPath"]
                pattern = r"\b" + re.escape(field_path.lower()) + r"\b"
                if re.search(pattern, sql_text):
                    field_hit_counts[field_path] += 1

        min_hits_for_drift = max(1, round(total_queries * QUERY_DRIFT_MIN_FRACTION))

        findings: list[RawFinding] = []
        for field in fields:
            field_path = field["fieldPath"]
            description = (field.get("description") or "").strip()
            hits = field_hit_counts[field_path]

            if description:
                continue
            if hits < min_hits_for_drift:
                continue

            hit_fraction = hits / total_queries
            confidence = round(min(0.9, 0.5 + hit_fraction * 0.4), 2)

            findings.append(
                RawFinding(
                    entity_urn=urn,
                    finding_type=FindingType.QUERY_DRIFT,
                    confidence=confidence,
                    summary=f"heavily queried, undocumented column: {field_path}",
                    unresolved_questions=[
                        f"Column `{field_path}` appears in {hits} of "
                        f"{total_queries} known queries against this dataset "
                        f"but has no description. What does it represent, "
                        f"and who should own documenting it?"
                    ],
                    assumptions_made=[
                        f"Detected usage of `{field_path}` via a word-boundary "
                        f"text match against raw SQL statement text, not a "
                        f"parsed AST — aliased references or SELECT * usage "
                        f"may be undercounted, and unrelated substring "
                        f"matches may be overcounted."
                    ],
                )
            )

        return findings

    async def process_findings(self, findings: list[RawFinding]) -> None:
        for f in findings:
            capsule = Capsule(
                agent_id=AGENT_ID,
                entity_urn=f.entity_urn,
                finding_type=f.finding_type,
                confidence=f.confidence,
                summary=f.summary,
                unresolved_questions=f.unresolved_questions,
                assumptions_made=f.assumptions_made,
            )

            decision = self.gate.evaluate(capsule)
            if not decision.allowed:
                logger.info("SKIPPED write for %s — %s", capsule.entity_urn, decision.reason)
                continue

            payload = capsule.to_structured_properties()
            try:
                await self.client.add_structured_properties(
                    payload["urn"], payload["structured_properties"]
                )
            except DataHubMCPError as exc:
                logger.error("WRITE FAILED for %s — %s", capsule.entity_urn, exc)
                continue

            self.gate.record(capsule)
            logger.info("WROTE capsule for %s (confidence=%.2f)", capsule.entity_urn, capsule.confidence)

            try:
                await self._write_reflection_document(capsule)
            except DataHubMCPError as exc:
                logger.warning(
                    "Structured property write for %s succeeded, but the "
                    "reflection document failed — %s", capsule.entity_urn, exc,
                )

    async def _write_reflection_document(self, capsule: Capsule) -> None:
        parts = capsule.entity_urn.split(",")
        entity_name = parts[-2] if len(parts) >= 2 else capsule.entity_urn

        title = f"Cairn: {capsule.finding_type.value.replace('_', ' ')} — {entity_name}"

        lines = [
            f"**Finding type:** {capsule.finding_type.value}",
            f"**Confidence:** {capsule.confidence:.2f}",
            f"**Summary:** {capsule.summary}",
            "",
            "### Unresolved questions",
        ]
        lines += [f"- {q}" for q in capsule.unresolved_questions] or ["- (none recorded)"]
        lines += ["", "### Assumptions made"]
        lines += [f"- {a}" for a in capsule.assumptions_made] or ["- (none recorded)"]
        lines += [
            "",
            f"_Written by {capsule.agent_id} on {capsule.session_ts}. A "
            f"machine-readable copy of this finding is also stored on "
            f"this dataset's structured properties (io.cairn.*)._",
        ]
        content = "\n".join(lines)

        await self.client.save_document(
            document_type="Context",
            title=title,
            content=content,
            topics=["cairn", capsule.finding_type.value],
            related_assets=[capsule.entity_urn],
        )


async def run(dataset_urn: str) -> None:
    gate = GovernanceGate()
    try:
        async with DataHubMCPClient() as client:
            sentinel = Sentinel(client, gate)
            findings = await sentinel.inspect_dataset(dataset_urn)
            logger.info("Sentinel produced %d raw finding(s)", len(findings))
            await sentinel.process_findings(findings)
    except DataHubMCPError as exc:
        logger.error("Cairn stopped: %s", exc)
        raise SystemExit(1) from exc


def main(dataset_urn: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run(dataset_urn))