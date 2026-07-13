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
   what the data / query patterns actually show.
2. **Capsule writer** — instead of writing free-text guesses back into
   DataHub, Cairn writes a small **structured handoff capsule** using
   DataHub's `structured_properties` — machine-readable fields any other
   agent can read programmatically, not just a human.
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
it becomes a capsule.

## Governed write-back

Cairn will refuse to write if any of these hold:

- confidence is below `MIN_CONFIDENCE_TO_WRITE` (default `0.55`)
- the same entity was already written to within `COOLDOWN_HOURS`
  (default `24`)
- more than `MAX_WRITES_PER_RUN` writes have already happened this run
  (default `10`)

This is deliberate: a hackathon judge should be able to see, in the demo
video, that Cairn *chose not to write* on a low-confidence finding — that
restraint is the point, not a limitation.

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
                 │  (capsule.py)    │
                 └────────┬─────────┘
                          │
                          ▼
                 DataHub (via MCP Server)
                 add_structured_properties / update_description / save_document
```

## Status of this code

This is a **working scaffold**, written from scratch for this hackathon
submission period. It is *not* pre-tested against a live DataHub
instance — you will need to:

1. Run `datahub docker quickstart` and load the `healthcare` sample pack
2. Set `TOOLS_IS_MUTATION_ENABLED=true` on your MCP server (writes are
   disabled by default — see DataHub MCP Server docs)
3. Fill in `.env` from `.env.example`
4. Run `datahub properties upsert -f datahub/structured_properties.yaml`
   to register the `io.cairn.*` property types (one-time step)
5. Adjust `cairn/mcp_client.py` if your MCP transport (stdio vs. HTTP)
   differs from the default assumed here

See `DEVELOPMENT_NOTES.md` for known gaps and what to build out first.

## Quickstart

Requires **Python 3.10+** (see `.python-version`) and Docker (for the
local DataHub instance).

> **Verified note:** installing `acryl-datahub` (the DataHub CLI) prints
> `Python versions above 3.11 are not actively tested with yet. Please
> use Python 3.11 for now.` This was confirmed by actually installing
> it (version 1.6.0.13) while building this scaffold. Cairn's own code
> works fine on 3.10+, but if you hit strange `datahub` CLI behavior,
> try Python 3.11 specifically for the CLI steps below.

```bash
python -m venv .venv && source .venv/bin/activate     # macOS/Linux
python -m venv .venv && .venv\Scripts\activate         # Windows
pip install -r requirements.txt

# Install the DataHub CLI itself (separate from this project's own
# dependencies above -- this is what provides the `datahub` command
# used below and in DEVELOPMENT_NOTES.md).
pip install --upgrade acryl-datahub

cp .env.example .env   # fill in your DataHub MCP endpoint + token

# One-time setup: register the structured property types Cairn writes.
# Confirm the exact subcommand against your DataHub CLI version first
# (`datahub properties --help`) -- this surface has changed across releases.
datahub properties upsert -f datahub/structured_properties.yaml

python -m cairn.cli --dataset-urn "urn:li:dataset:(urn:li:dataPlatform:snowflake,healthcare.patient_records,PROD)"
```

### Running the tests

`capsule.py` and `governance.py` are fully implemented and covered by
unit tests that need **no DataHub connection at all** — a good first
check that your environment is set up correctly:

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
