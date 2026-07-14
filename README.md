# Cairn — A Context Relay Agent for DataHub

> *A cairn is a stack of stones travelers leave along a trail — not a full map,
> just enough signal for the next traveler to find their way.*
> Cairn does the same for AI agents and data teams working in DataHub: it
> leaves small, deliberate, structured markers so the next person or agent
> inherits real context instead of starting from zero.

Built for **Build with DataHub: The Agent Hackathon** (Open / Wildcard category).

---

## The problem

Agents (and humans) that touch a dataset in DataHub often learn something
useful — "this column looks derived, not authoritative," "this description
no longer matches how the table is actually queried," "I'm not fully sure
why this value diverges upstream." That knowledge normally evaporates the
moment the session ends. The next agent or teammate starts from zero, or
worse, overwrites a partial understanding with an equally partial one.

Cairn treats that evaporating knowledge as the actual problem to solve —
not a side effect of some other task.

## What Cairn does

Cairn is a DataHub agent with three cooperating parts:

1. **Sentinel** — inspects a dataset (schema, lineage, description, query
   history) and produces *findings*: gaps between what is documented and
   what the data / query patterns actually show. Two strategies are
   implemented: `query_drift` (heavily queried but undocumented columns)
   and `documentation_gap` (an existing description that may be stale
   relative to current schema/lineage — this one uses a single LLM call
   and degrades gracefully, producing no findings rather than erroring,
   if `ANTHROPIC_API_KEY` isn't set).
2. **Capsule writer** — instead of writing free-text guesses back into
   DataHub, Cairn writes a small **structured handoff capsule** using
   DataHub's `structured_properties` — machine-readable fields any other
   agent can read programmatically, not just a human. Alongside that,
   Cairn also saves a short, human-readable **reflection document**
   (via `save_document`) linked to the dataset, so a person skimming the
   dataset page in the DataHub UI sees Cairn's contribution too, not
   only an agent parsing the Props tab.
3. **Governance gate** — Cairn does **not** write back every finding it
   makes. Writes are rate-limited, confidence-gated, and cooled down per
   entity, so Cairn behaves like a careful colleague, not a bot that
   floods your catalog with descriptions.

None of this replaces DataHub's own Analytics Agent (which answers
business questions in plain English). Cairn does not answer questions —
it watches for *context that is about to be lost* and leaves a marker
before it disappears.

## The handoff capsule

Every capsule Cairn writes has the same shape, so it's cheap for *any*
downstream agent to parse:

```json
{
  "agent_id": "cairn-sentinel-v1",
  "session_ts": "2026-07-20T14:32:00Z",
  "confidence": 0.72,
  "finding_type": "documentation_gap | query_drift | lineage_break",
  "summary": "Three-word style summary, human-skimmable",
  "unresolved_questions": [
    "Why does this column diverge from the upstream source after 2026-06-01?"
  ],
  "assumptions_made": [
    "Treated NULL as missing, not zero"
  ],
  "requires_human_review": true
}
```

See `examples/sample_capsule.json` for a full example, and
`examples/sample_finding.json` for what a raw finding looks like before
it becomes a capsule. **Both examples are captured from a real run**
against a live, self-hosted DataHub instance (2026-07-14) — not
hand-constructed illustrations.

## Governed write-back

Cairn will refuse to write if any of these hold:

- confidence is below `MIN_CONFIDENCE_TO_WRITE` (default `0.55`)
- the same entity was already written to within `COOLDOWN_HOURS`
  (default `24`)
- more than `MAX_WRITES_PER_RUN` writes have already happened this run
  (default `10`)

This is deliberate: a hackathon judge should be able to see, in the demo
video, that Cairn *chose not to write* on a low-confidence finding — that
restraint is the point, not a limitation. This has been confirmed
working end-to-end against a live instance: a high-confidence finding is
written (both as structured properties and a reflection document) while
a low-confidence finding in the same run is skipped with a logged
reason.

## Architecture

```
                 ┌─────────────────┐
   dataset URN → │  Sentinel        │  finds gaps: docs vs. schema,
                 │  (agent.py)      │  docs vs. lineage, docs vs. queries
                 └────────┬─────────┘
                          │ raw findings
                          ▼
                 ┌─────────────────┐
                 │  Governance gate │  confidence / cooldown / rate limit
                 │  (governance.py) │
                 └────────┬─────────┘
                          │ approved findings only
                          ▼
                 ┌─────────────────┐
                 │  Capsule writer  │  builds structured_properties payload
                 │  (capsule.py)    │  + a short human-readable reflection doc
                 └────────┬─────────┘
                          │
                          ▼
                 DataHub (via MCP Server)
                 add_structured_properties  → io.cairn.* fields (Props tab)
                 save_document               → linked Context doc (Documentation tab)
```

## Status of this code

This has been **built and verified against a live, self-hosted DataHub
quickstart instance** (2026-07-14) — not just written and assumed to
work. Along the way, three real integration bugs were found by reading
the actual `mcp-server-datahub` tool source and testing live writes,
then fixed and covered by regression tests:

- `add_structured_properties` originally sent the wrong payload shape
  (a flat `{urn, structured_properties}` object instead of the real
  tool's `{property_values, entity_urns}` signature).
- `io.cairn.sessionTimestamp` is registered as a `date`-type property,
  which DataHub validates strictly as `YYYY-MM-DD` — a full ISO 8601
  datetime was rejected server-side until this was fixed.
- `save_document` originally used an invented `parent_folder` parameter
  that doesn't exist on the real tool.

`mcp_client.py`'s `call()` method also now checks the MCP tool result's
`isError` field — earlier, a server-side write failure was silently
treated as success, which could have poisoned governance's cooldown
state for a write that never actually happened. See
`DEVELOPMENT_NOTES.md` for the full account of each bug and fix.

**Confirmed working live**, including a fully independent (not
hand-seeded) finding: `Sentinel._find_query_drift` found an undocumented,
heavily-queried `browser` column on a `logging_events` dataset in a
default `datahub docker quickstart` instance, and Cairn wrote both a
structured-property capsule and a linked reflection document for it
without any human constructing the finding by hand.

`documentation_gap` (the LLM-based strategy) is implemented and unit
tested, but has not yet been run against live data, since that requires
an `ANTHROPIC_API_KEY`.

## Quickstart

Requires **Python 3.10+** (see `.python-version`) and Docker (for the
local DataHub instance).

> **Verified note:** installing `acryl-datahub` (the DataHub CLI) prints
> `Python versions above 3.11 are not actively tested with yet. Please
> use Python 3.11 for now.` This was confirmed by actually installing
> it (version 1.6.0.13) while building this scaffold. Cairn's own code
> works fine on 3.10+, but if you hit strange `datahub` CLI behavior,
> try Python 3.11 specifically for the CLI steps below.

> **Verified note:** a default `datahub docker quickstart` instance does
> **not** come with a `healthcare` sample pack — only the generic
> `Sample*Dataset` entities (e.g. `SampleHiveDataset`) plus a handful of
> unrelated demo assets (`fct_users_created`, `logging_events`, etc.).
> Search your own instance's UI (`localhost:9002`) for a dataset with an
> undocumented column to try `query_drift` against, rather than assuming
> `healthcare.*` exists.

```bash
python -m venv .venv && source .venv/bin/activate     # macOS/Linux
python -m venv .venv && .venv\Scripts\activate         # Windows
pip install -r requirements.txt
pip install -e .

# Install the DataHub CLI itself (separate from this project's own
# dependencies above -- this is what provides the `datahub` command
# used below and in DEVELOPMENT_NOTES.md).
pip install --upgrade acryl-datahub

cp .env.example .env   # fill in your DataHub MCP endpoint + token

# One-time setup: register the structured property types Cairn writes.
# Confirm the exact subcommand against your DataHub CLI version first
# (`datahub properties --help`) -- this surface has changed across releases.
datahub properties upsert -f datahub/structured_properties.yaml

# Start the MCP server with writes enabled (in its own terminal --
# it stays running). Watch its startup log for which port it actually
# binds to (commonly 8000, not always the DataHub-Cloud-default 8080)
# and make sure DATAHUB_MCP_URL in .env matches it.
TOOLS_IS_MUTATION_ENABLED=true uvx mcp-server-datahub --transport http

# In a second terminal:
python -m cairn.cli --dataset-urn "urn:li:dataset:(urn:li:dataPlatform:hive,logging_events,PROD)"
```

### Running the tests

`capsule.py`, `governance.py`, and `agent.py` are fully covered by unit
tests that need **no DataHub connection at all** — a good first check
that your environment is set up correctly:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

These also run automatically on every push via GitHub Actions
(`.github/workflows/tests.yml`).

## License

Apache License 2.0 — see `LICENSE`.

## Disclosure (per hackathon rules, section 4: "New Projects Only")

This project's code was written during the hackathon submission period
(July 6 – August 10, 2026). It draws on *conceptual* patterns explored in
prior personal projects (an unrelated wellness app) and public DataHub
documentation, but contains no code carried over from those sources.