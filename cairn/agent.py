"""
agent.py — The Sentinel: Cairn's core inspection loop.

Follows the read-before-write priority order documented by DataHub's own
Analytics Agent (search context -> discover datasets -> inspect schema
-> check lineage -> review query history -> only then write), applied to
a different goal: not answering a question, but noticing where context
is thin or contradictory and leaving a capsule about it.

Two finding strategies are implemented here:

  - documentation_gap  : an entity's existing description may be stale
                         relative to its current schema/lineage (uses
                         an LLM call — see _find_documentation_gaps)
  - query_drift        : columns that are heavily queried but undocumented

This is intentionally two strategies, not five — a hackathon judge can
verify both in the time they have. DEVELOPMENT_NOTES.md lists the
lineage_break and ownership_stale strategies as scaffolded-but-not-wired,
for you to extend if time allows.
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

# A field must appear in at least this fraction of a dataset's known
# queries before it's considered "heavily queried" for drift purposes.
# Verified against a live mcp-server-datahub v0.6.0 instance on
# 2026-07-13 (self-hosted, --transport http): see structured response
# shapes below. Not yet tuned against a large real-world query corpus —
# treat as a reasonable starting default, not a validated threshold.
QUERY_DRIFT_MIN_FRACTION = 0.3

# Below this finding_confidence, a documentation_gap candidate is
# dropped rather than surfaced — a low-confidence "maybe it's stale"
# isn't worth a capsule (the governance gate would likely block it
# anyway at the default 0.55 threshold, but filtering here avoids an
# unnecessary LLM-to-governance round trip being logged as a "finding").
DOC_GAP_MIN_CONFIDENCE = 0.3

# anthropic is an OPTIONAL dependency: only _find_documentation_gaps
# needs it, and that strategy already degrades gracefully (logs +
# returns no findings) when ANTHROPIC_API_KEY isn't set. Importing it
# at module level (rather than inside the function) means tests can
# monkeypatch `cairn.agent.anthropic` directly instead of having to
# patch sys.modules before import.
try:
    import anthropic
except ImportError:  # pragma: no cover — exercised via monkeypatch in tests
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
    """
    Normalize an mcp-server-datahub tool result down to a plain dict.

    Verified 2026-07-13 against a live self-hosted mcp-server-datahub
    v0.6.0 instance (--transport http): client.call_tool() returns an
    object exposing both `.content` (a list of TextContent blocks with
    the same payload as a JSON string) and `.structuredContent` (the
    same payload already parsed into a dict). This prefers
    structuredContent and falls back to parsing the first text block,
    so it degrades gracefully if a different mcp-server-datahub version
    ever stops populating structuredContent.
    """
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
        """
        Run both finding strategies against one dataset and return raw
        findings (before governance filtering).
        """
        findings: list[RawFinding] = []

        entity = await self.client.get_entities([urn])
        schema_fields = await self.client.list_schema_fields(urn)
        queries = await self.client.get_dataset_queries(urn)
        lineage = await self.client.get_lineage(urn)

        findings.extend(self._find_documentation_gaps(urn, entity, schema_fields, lineage))
        findings.extend(self._find_query_drift(urn, schema_fields, queries))

        return findings

    # --- Strategy 1: documentation gaps --------------------------------

    def _find_documentation_gaps(
        self, urn: str, entity, schema_fields, lineage=None
    ) -> list[RawFinding]:
        """
        Compares an entity's existing description against its current
        schema fields and lineage, using an LLM call to judge whether
        the description still accurately reflects what the dataset
        contains and where it comes from.

        This is deliberately the ONE place in Cairn that calls an LLM —
        this specific judgment (does free text still match the current
        technical reality?) needs judgment, not a diff, unlike
        _find_query_drift below.

        Degrades gracefully — logs and returns no findings — if:
          - ANTHROPIC_API_KEY isn't set (see .env.example)
          - the `anthropic` package isn't installed (optional dependency;
            `pip install anthropic` to enable this strategy)
          - the entity has no existing description at all (an
            undocumented dataset is a different, simpler signal than a
            *stale* one — out of scope for this strategy; see the
            docstring note further down)
          - the LLM call or its response parsing fails for any reason

        Response shape for get_entities confirmed 2026-07-13 against a
        live self-hosted mcp-server-datahub v0.6.0 instance: the
        structured result is {"result": [{"urn": ..., "name": ...,
        "description": "...", "platform": {...}, "ownership": {...},
        "schemaMetadata": {...}, ...}]}. The top-level "description" key
        (dataset-level, distinct from per-field descriptions in
        list_schema_fields) is what's used here.

        The exact shape of get_lineage's response was NOT independently
        verified against the running instance the way get_entities and
        list_schema_fields were — rather than guess at specific field
        names and risk silently mis-parsing it, the raw structured
        response is passed into the LLM prompt as JSON text. The model
        reads it as context; this code never parses lineage fields
        directly, so an unexpected shape there degrades the quality of
        the model's judgment rather than crashing this function.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.info(
                "documentation_gap: skipping %s (ANTHROPIC_API_KEY not set "
                "— see .env.example)",
                urn,
            )
            return []

        if anthropic is None:
            logger.warning(
                "documentation_gap: skipping %s (the 'anthropic' package "
                "isn't installed — run `pip install anthropic`)",
                urn,
            )
            return []

        entity_results = _structured(entity).get("result", [])
        if not entity_results:
            logger.info("documentation_gap: skipping %s (no entity data returned)", urn)
            return []

        description = (entity_results[0].get("description") or "").strip()
        if not description:
            logger.info(
                "documentation_gap: skipping %s (no existing description to "
                "check for staleness — an undocumented dataset is a "
                "different signal than a stale one)",
                urn,
            )
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

        # Model default reflects Anthropic's current lineup as of this
        # writing (2026-07). Verify against https://docs.claude.com
        # before a demo — Anthropic's model lineup changes over time and
        # this default can go stale. Override via ANTHROPIC_MODEL if
        # you'd rather use a cheaper/faster model for this classification-
        # style task.
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
            # Broad except is deliberate here: an LLM call can fail in
            # many ways (network, auth, rate limit, malformed JSON in
            # the response) and none of them should crash the whole
            # inspection run — just skip this strategy for this dataset.
            logger.warning("documentation_gap: LLM call/parsing failed for %s: %s", urn, exc)
            return []

        # We asked the model for its confidence the description IS
        # current. Cairn's finding confidence is about the finding
        # itself (i.e. confidence a gap EXISTS), which is the inverse.
        finding_confidence = round(1.0 - confidence_description_is_current, 2)

        if not gap or finding_confidence < DOC_GAP_MIN_CONFIDENCE:
            logger.info(
                "documentation_gap: %s — no significant gap found (model "
                "confidence description is current: %.2f)",
                urn,
                confidence_description_is_current,
            )
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

    # --- Strategy 2: query drift ----------------------------------------

    def _find_query_drift(self, urn: str, schema_fields, queries) -> list[RawFinding]:
        """
        Compares which columns actually appear in real queries
        (get_dataset_queries) against which columns are documented
        (list_schema_fields). Columns that are heavily queried but have
        no description are flagged as drift candidates.

        Response shapes verified 2026-07-13 against a live self-hosted
        mcp-server-datahub v0.6.0 instance (--transport http) using the
        DataHub healthcare/quickstart sample pack:

          list_schema_fields(urn) -> {
            "urn": "...",
            "fields": [
              {"fieldPath": "field_foo", "nativeDataType": "varchar(100)",
               "description": "Foo field description", "nullable": false,
               "isPartOfKey": true},
              ...
            ],
            "totalFields": 2, "returned": 2, ...
          }

          get_dataset_queries(urn) -> {
            "start": 0, "total": 1,
            "queries": [
              {"urn": "urn:li:query:...",
               "properties": {
                 "name": "TestQuery", "source": "MANUAL",
                 "statement": {"value": "SELECT * FROM db.T WHERE field_foo = 'x'",
                               "language": "SQL"},
                 "lastModified": {"actor": "urn:li:corpuser:..."}
               },
               "subjects": [{"dataset": {"urn": "..."}}]}
            ]
          }

        IMPORTANT: in this sample data, `subjects` only ever pointed at
        the dataset itself, not at individual columns — even though the
        MCP server docs mention column-level subjects are possible in
        general. Column usage isn't reliable from `subjects` alone here,
        so this matches column names against the raw SQL text in
        `statement.value` instead. That's a real limitation: it will
        miss column references hidden behind aliases or `SELECT *`, and
        can produce false positives if a field name collides with an
        unrelated substring elsewhere in the query (a comment, a string
        literal, another table's column with the same name). Treat the
        resulting confidence scores accordingly — this is a heuristic,
        not a parsed-SQL guarantee.
        """
        fields = _structured(schema_fields).get("fields", [])
        query_list = _structured(queries).get("queries", [])
        total_queries = len(query_list)

        if not fields or total_queries == 0:
            logger.info(
                "query_drift: skipping %s (no schema fields or no queries to compare)",
                urn,
            )
            return []

        # Count, for each field, how many queries reference it by name.
        field_hit_counts: dict[str, int] = {f["fieldPath"]: 0 for f in fields}
        for query in query_list:
            statement = (query.get("properties") or {}).get("statement") or {}
            sql_text = (statement.get("value") or "").lower()
            if not sql_text:
                continue
            for field in fields:
                field_path = field["fieldPath"]
                # Word-boundary match so "field_foo" doesn't also match
                # "field_foobar". Still a heuristic — see docstring above.
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
                continue  # already documented — not a drift candidate
            if hits < min_hits_for_drift:
                continue  # not queried often enough to flag

            hit_fraction = hits / total_queries
            # Confidence scales with how dominant the field is in the
            # query corpus, capped below 1.0 since this is a heuristic
            # text match, not a verified parse.
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

        logger.info(
            "query_drift: %s produced %d finding(s) from %d field(s) / %d quer(y/ies)",
            urn,
            len(findings),
            len(fields),
            total_queries,
        )
        return findings

    # --- Governed write-back ---------------------------------------------

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
                logger.info(
                    "SKIPPED write for %s — %s", capsule.entity_urn, decision.reason
                )
                continue

            payload = capsule.to_structured_properties()
            try:
                await self.client.add_structured_properties(
                    payload["property_values"], payload["entity_urns"]
                )
            except DataHubMCPError as exc:
                # A write that DataHub itself rejects (e.g. a value type
                # or format validation error) must NOT be recorded in
                # governance state. Added 2026-07-13 after a live bug:
                # mcp_client.call() used to return a server-side error
                # result as if it were a success, which caused this
                # branch's code to log "WROTE" and gate.record() to poison
                # the cooldown for a write that never actually landed.
                # Now that mcp_client.call() raises on isError, this
                # except block is what keeps a single bad write from
                # crashing the whole run (via run()'s outer handler) while
                # still making the failure loud and un-recorded, so the
                # entity is eligible to be retried on the next run instead
                # of sitting in a false 24h cooldown.
                logger.error(
                    "WRITE FAILED for %s — %s", capsule.entity_urn, exc
                )
                continue

            self.gate.record(capsule)
            logger.info(
                "WROTE capsule for %s (confidence=%.2f)",
                capsule.entity_urn,
                capsule.confidence,
            )

            # Also leave a human-readable reflection document alongside the
            # machine-readable structured properties. The structured
            # properties above are the capsule's authoritative record and
            # what governance/cooldown state is based on; this document is
            # a best-effort presentation layer so a person skimming the
            # dataset page sees Cairn's contribution too, not only an agent
            # parsing the Props tab. A failure here is logged but does not
            # undo or hide the structured property write already recorded.
            try:
                await self._write_reflection_document(capsule)
            except DataHubMCPError as exc:
                logger.warning(
                    "Structured property write for %s succeeded, but the "
                    "human-readable reflection document failed — %s",
                    capsule.entity_urn,
                    exc,
                )

    async def _write_reflection_document(self, capsule: Capsule) -> None:
        """
        Save a short markdown document via save_document, linked to the
        entity via related_assets so it surfaces on the dataset's own
        page in the DataHub UI -- not just findable through search or
        buried in the Props tab.

        Uses document_type="Context", which matches Cairn's own framing
        (a handoff marker for the next agent or person) among the tool's
        fixed set of allowed types.

        NOTE: save_document's own docstring says an interactive agent
        should confirm with the user before saving. Cairn is not
        interactive -- GovernanceGate.evaluate() (confidence threshold,
        cooldown, per-run cap) is Cairn's equivalent confirmation
        mechanism, already passed by the time this method is called.
        """
        # Datasets URNs look like "urn:li:dataset:(urn:li:dataPlatform:hive,
        # SampleHiveDataset,PROD)" -- the human-readable name is the
        # second-to-last comma-separated segment. Falls back to the full
        # URN if the format doesn't match (e.g. a non-dataset entity type).
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
        # Caught here (not just in mcp_client.py) so a demo run ends with
        # one clear, actionable line instead of a raw stack trace. Note
        # this now only fires for connection-level failures (or a write
        # error surfaced outside process_findings' own try/except) since
        # per-write failures are handled and logged inside
        # process_findings above without aborting the whole run.
        logger.error("Cairn stopped: %s", exc)
        raise SystemExit(1) from exc


def main(dataset_urn: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run(dataset_urn))