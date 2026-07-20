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

These thresholds are configurable via environment variables
(`CAIRN_MIN_CONFIDENCE_TO_WRITE`, `CAIRN_COOLDOWN_HOURS`,
`CAIRN_MAX_WRITES_PER_RUN`) — for example, to re-run Cairn against a
dataset it already wrote to within the cooldown window (useful when
testing, or re-recording a demo after a fix):

```bash
CAIRN_COOLDOWN_HOURS=0 python -m cairn.cli --dataset-urn "..."
```

Cooldown state persists across runs in `.cairn_write_state.json` in the
project root — delete it to reset all cooldown history, e.g. when
starting fresh against a newly rebuilt DataHub instance.

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
quickstart instance** (2026-07-14, with a follow-up rebuild-and-reverify
session on 2026-07-18) — not just written and assumed to work. Along the
way, five real integration bugs were found by reading the actual
`mcp-server-datahub` tool source and testing live writes, then fixed and
covered by regression tests:

- `add_structured_properties` originally sent the wrong payload shape
  (a flat `{urn, structured_properties}` object instead of the real
  tool's `{property_values, entity_urns}` signature).
- `io.cairn.sessionTimestamp` is registered as a `date`-type property,
  which DataHub validates strictly as `YYYY-MM-DD` — a full ISO 8601
  datetime was rejected server-side until this was fixed.
- `save_document` originally used an invented `parent_folder` parameter
  that doesn't exist on the real tool.
- `mcp_client.py`'s `__aenter__()` originally wrapped connection setup
  in `asyncio.wait_for()`, which runs its coroutine in a separate
  asyncio Task under the hood — breaking anyio's cancel-scope-to-Task
  binding and raising a `TypeError` on connect. The first fix attempt
  (`anyio.fail_after()`) was itself subtly wrong: `fail_after()` is a
  *synchronous* context manager, and cancel scopes must close in strict
  nested order within one block — it can't wrap a method that
  intentionally leaves resources open past the block (as `__aenter__()`
  does here, by design, until `__aexit__()` runs later). The actual fix
  was to drop scope-based timeouts from `__aenter__()` entirely and pass
  the timeout straight to `streamablehttp_client`'s own `timeout`
  parameter instead — both mistakes only surfaced by testing against a
  live MCP server, not from reading the code.
- `agent.py`'s `_find_documentation_gaps` originally read
  `entity["description"]` directly; the real `get_entities` MCP tool
  response nests it under `entity["properties"]["description"]`. This
  silently caused every dataset with a real, existing description to be
  skipped as if it had none at all — a bug that unit tests alone didn't
  catch, since they didn't exercise the real response shape.

`mcp_client.py`'s `call()` method also checks the MCP tool result's
`isError` field — a server-side write failure is reported as a normal
200-OK response with `isError=True`, not a transport-level exception, so
without this check a real write failure could have silently poisoned
governance's cooldown state for a write that never actually happened.
See `DEVELOPMENT_NOTES.md` for the full account of each bug and fix.

**Confirmed working live**, including fully independent (not
hand-seeded) findings from both strategies in the same run: against a
freshly rebuilt `datahub docker quickstart` instance (2026-07-18),
`Sentinel._find_query_drift` found an undocumented, heavily-queried
`browser` column on the `logging_events` dataset (confidence 0.90, based
on query history backfilled via DataHub's own `sql-queries` ingestion
source), while `Sentinel._find_documentation_gaps` independently flagged
the dataset's existing description as stale relative to its schema and
lineage (confidence 0.60, using `claude-sonnet-5`). Both findings passed
the governance gate and were written as separate structured-property
capsules and separate linked reflection documents on the same dataset,
with no finding hand-constructed by a person.

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
> `healthcare.*` exists. Running `datahub docker ingest-sample-data`
> loads a larger, more varied sample pack (Hive, Feast, Looker, Airflow,
> Kafka, HDFS platforms) if you want more datasets to explore.

> **Verified note:** `query_drift` needs actual query history to find
> anything — a freshly ingested dataset has none. DataHub's `sql-queries`
> ingestion source can backfill query history from a small NDJSON file
> for testing/demo purposes:
> ```yaml
> # queries_recipe.yml
> source:
>   type: sql-queries
>   config:
>     platform: "hive"
>     query_file: "./queries.json"
> sink:
>   type: "datahub-rest"
>   config:
>     server: "http://localhost:8080"
> ```
> ```bash
> pip install 'acryl-datahub[sql-queries]'
> datahub ingest -c queries_recipe.yml
> ```
> See DataHub's [SQL Queries source docs](https://docs.datahub.com/docs/generated/ingestion/sources/sql-queries)
> for the full NDJSON query-file format.

### 0. Start DataHub itself

If you don't already have a running DataHub instance, the fastest way to
get one locally is DataHub's own quickstart (needs Docker running):

```bash
pip install --upgrade acryl-datahub
datahub docker quickstart
# Loads a small built-in demo pack. For a larger, more varied one:
datahub docker ingest-sample-data
```

Wait for it to report `DataHub is now running`, then confirm the
frontend actually loads at <http://localhost:9002> before moving on —
this isolates "is DataHub itself healthy" from any later Cairn-specific
debugging.

### 1. Set up Cairn

```bash
python -m venv .venv && source .venv/bin/activate     # macOS/Linux
python -m venv .venv && .venv\Scripts\activate         # Windows
pip install -r requirements.txt
pip install -e .

cp .env.example .env   # then fill in the values below
```

`.env` needs:

| Variable | Required? | Notes |
| --- | --- | --- |
| `DATAHUB_MCP_URL` | Yes | The MCP server's own URL, e.g. `http://localhost:8000/mcp` — **check the MCP server's startup log for the actual port it bound to** (see step 3 below); it isn't always 8080. |
| `DATAHUB_PERSONAL_ACCESS_TOKEN` | Only if your DataHub instance has auth enabled | A default local `quickstart` instance has auth disabled, so this can usually be left blank. |
| `ANTHROPIC_API_KEY` | Only for `documentation_gap` | Without it, `documentation_gap` logs a message and skips cleanly — `query_drift` and the rest of Cairn work fine either way. |
| `ANTHROPIC_MODEL` | No | Defaults to `claude-sonnet-5` if unset. |

### 2. Register Cairn's structured property types

```bash
# Confirm the exact subcommand against your DataHub CLI version first
# (`datahub properties --help`) -- this surface has changed across
# releases. You may see a "Client-Server Incompatible" version warning
# here -- it's harmless and doesn't stop the properties from being
# created; check the command's own output for "Created structured
# property" lines to confirm success.
#
# Needs re-running any time you rebuild DataHub from scratch, since a
# fresh instance has no structured property definitions registered yet
# even though Cairn will happily try to write values for them --
# without this step, writes fail with "Unexpected null value found for
# ... Structured Property Definition".
datahub properties upsert -f datahub/structured_properties.yaml
```

### 3. Start the MCP server

```bash
# If `uv`/`uvx` isn't already available in your environment:
pip install uv

# Runs in its own terminal -- it stays running. Watch its startup log
# for which port it actually binds to (commonly 8000, not always the
# DataHub-Cloud-default 8080) and make sure DATAHUB_MCP_URL in .env
# matches it exactly, including the path (.../mcp).
TOOLS_IS_MUTATION_ENABLED=true uvx mcp-server-datahub --transport http
```

### 4. Run Cairn

```bash
# In a second terminal:
python -m cairn.cli --dataset-urn "urn:li:dataset:(urn:li:dataPlatform:hive,logging_events,PROD)"
```

A successful run logs a `WROTE capsule for ...` line for each finding
that passes the governance gate. To confirm it visually: open the
dataset's page at `http://localhost:9002/dataset/<urn>/Properties` and
look for an `io.cairn (8)` group, or search DataHub for `Cairn:` to find
the linked reflection document(s) it wrote alongside the properties.

Want to see the governance gate reject a low-confidence finding on
demand, without waiting for one to occur naturally?

```bash
python probe_writeback.py "urn:li:dataset:(urn:li:dataPlatform:hive,SampleHiveDataset,PROD)"
```

See `probe_writeback.py` and its usage notes in `DEVELOPMENT_NOTES.md`
for what this actually does and why `SampleHiveDataset` (rather than
whatever dataset you've been running `cairn.cli` against) is the safer
target — it writes a synthetic test finding, clearly labeled as such.

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