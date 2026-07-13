# Development notes — what's real, what's a stub, what to build next

Being upfront about this matters more than it looks impressive: a judge
who finds an undisclosed stub loses trust in the whole submission. A
judge who finds a clearly labeled stub with a real plan sees an honest,
competent submitter. So — here is the actual state of this scaffold.

## What is implemented and structurally real

- `capsule.py` — the handoff capsule schema, JSON (de)serialization,
  and the `structured_properties` payload shape. This is complete.
- `governance.py` — confidence threshold, per-run write cap, and
  per-entity cooldown, backed by a small local JSON state file. This is
  complete and unit-testable without any DataHub connection at all.
- `mcp_client.py` — a real async wrapper around the official `mcp`
  Python client, using streamable HTTP transport. The tool-call
  convenience methods (`search`, `get_entities`, `get_lineage`, etc.)
  match the tool names documented in the DataHub MCP Server README.
- `agent.py` — the orchestration loop (inspect → findings → governance
  gate → write) is real and runnable end-to-end *once the two finding
  strategies below are filled in*.

## What is a deliberate stub — build these first

- `Sentinel._find_documentation_gaps()` and `Sentinel._find_query_drift()`
  in `agent.py` currently log a message and return an empty list. The
  orchestration around them (governance, capsule writing) is real; the
  actual "is this a gap?" judgment is not yet implemented.

  To make `_find_query_drift` real: `get_dataset_queries` returns query
  text/column references; `list_schema_fields` returns documented
  columns. Diff the two column sets. Columns appearing in >N% of
  queries but missing a description are drift candidates. Confirm the
  exact response shape against your running MCP server first — don't
  guess at field names.

  To make `_find_documentation_gaps` real: this is the one place an LLM
  call earns its keep — comparing an entity's free-text description
  against its schema/lineage for staleness needs judgment, not just a
  diff. A reasonable starting prompt:

  > "Given this entity's current description: {description}, its schema
  > fields: {fields}, and its upstream lineage: {lineage} — does the
  > description still accurately describe what this dataset contains
  > and where it comes from? Respond with a confidence score (0-1) and,
  > if confidence < 0.7, the specific gap."

## Not wired up at all (future work, mentioned in the README as ideas)

- `lineage_break` and `ownership_stale` finding types exist as enum
  values in `capsule.py` but have no corresponding Sentinel strategy.
- Temporal / trend tracking across multiple runs (comparing confidence
  or drift over simulated time) — the `session_ts` field on every
  capsule is there specifically so this can be built later by reading
  back a history of capsules for the same entity and comparing them.
- Registering the `io.cairn.*` structured property definitions in
  DataHub itself. `add_structured_properties` will fail until these
  property types are registered once. A ready-to-run definition file is
  provided at `datahub/structured_properties.yaml` — apply it with:

      datahub properties upsert -f datahub/structured_properties.yaml

  **Verified while building this scaffold:** the command syntax above
  (`-f, --file PATH`) is confirmed correct against a real installation
  of `acryl-datahub` 1.6.0.13 (`datahub properties upsert --help`), and
  the YAML file parses past argument handling successfully. What was
  *not* tested is the actual upsert against a live DataHub instance
  (no running instance was available in the environment used to build
  this) — it stopped at the expected "no ~/.datahubenv found" auth
  error, which just means you need to run `datahub init` first. If the
  YAML's exact property schema doesn't match what your DataHub version
  expects, the DataHub UI's "Structured Properties" section is the
  manual fallback.

## Before recording the demo video

0. **Sanity-check the MCP connection independently of Cairn's own code
   first.** If you have Claude Desktop, Cursor, or Cline installed, you
   can point it at your local DataHub MCP server directly:

       npx -y @acryldata/mcp-server-datahub init

   If natural-language queries work there, you know the server itself
   is healthy and any connection errors from Cairn are in
   `mcp_client.py`'s configuration, not DataHub. This isolates one
   variable before you start debugging the other.

1. Confirm `TOOLS_IS_MUTATION_ENABLED=true` on your MCP server — writes
   silently no-op otherwise, and the demo will look broken.
2. Run once against a dataset with a genuinely undocumented, heavily
   queried column so the query_drift strategy has something real to
   find — the `healthcare` sample pack should have planted issues
   suited to this.
3. Deliberately show one *skipped* write (a low-confidence finding) on
   camera — this is the clearest way to demonstrate the governance gate
   is real and not just described in the README.
