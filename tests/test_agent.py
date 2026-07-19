"""
Unit tests for Sentinel._find_query_drift() and
Sentinel._find_documentation_gaps() — the two pieces of agent.py that
involve actual reasoning logic rather than pure orchestration.

Why mock data instead of a live DataHub connection for query_drift: the
healthcare/quickstart sample pack turned out not to contain any dataset
with both undocumented fields AND query history at the same time
(verified manually against a running instance on 2026-07-13 — see
DEVELOPMENT_NOTES.md). Rather than depend on a live instance having the
right shape of data at demo time, this constructs mock MCP responses
matching the exact structure confirmed against a real
mcp-server-datahub v0.6.0 instance, and tests the diffing logic directly.

Why the anthropic client is monkeypatched for documentation_gap tests:
this strategy makes a real LLM call in production, but tests should
never depend on network access, a real API key, or non-deterministic
model output. `cairn.agent.anthropic` is monkeypatched to a fake module
exposing a fake `Anthropic` class, so the tests exercise Cairn's own
prompt-building, response-parsing, and confidence-inversion logic
without ever calling out to Anthropic's API.

Run with: pytest tests/test_agent.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairn.agent import Sentinel, _structured
from cairn.capsule import FindingType


def _mock_tool_result(structured_content: dict):
    """
    Build a stand-in for the object mcp-server-datahub's call_tool()
    returns. Only sets .structuredContent, since _structured() checks
    that first and it's sufficient for these tests — the .content
    fallback path is exercised separately below.
    """
    return SimpleNamespace(structuredContent=structured_content, content=[])


def make_sentinel() -> Sentinel:
    # Neither strategy under test here touches self.client or self.gate
    # directly, so both can be None — only the pure reasoning logic is
    # under test.
    return Sentinel(client=None, gate=None)  # type: ignore[arg-type]


# --- _structured() extraction --------------------------------------------


def test_structured_prefers_structured_content():
    result = _mock_tool_result({"fields": [{"fieldPath": "x"}]})
    assert _structured(result) == {"fields": [{"fieldPath": "x"}]}


def test_structured_falls_back_to_content_text():
    text_block = SimpleNamespace(text='{"fields": [{"fieldPath": "y"}]}')
    result = SimpleNamespace(structuredContent=None, content=[text_block])
    assert _structured(result) == {"fields": [{"fieldPath": "y"}]}


def test_structured_returns_empty_dict_on_garbage():
    result = SimpleNamespace(structuredContent=None, content=[])
    assert _structured(result) == {}


# --- _find_query_drift() --------------------------------------------------


def make_schema_fields(fields: list[dict]):
    return _mock_tool_result({"urn": "urn:li:dataset:(test,ds,PROD)", "fields": fields})


def make_queries(statements: list[str]):
    queries = [
        {
            "urn": f"urn:li:query:q{i}",
            "properties": {
                "name": f"Query{i}",
                "source": "MANUAL",
                "statement": {"value": stmt, "language": "SQL"},
            },
            "subjects": [{"dataset": {"urn": "urn:li:dataset:(test,ds,PROD)"}}],
        }
        for i, stmt in enumerate(statements)
    ]
    return _mock_tool_result(
        {"start": 0, "total": len(queries), "count": 10, "queries": queries}
    )


def test_flags_undocumented_heavily_queried_column():
    """
    Mirrors the real shape confirmed against a live mcp-server-datahub
    instance: a `flag_override`-style column referenced in most queries
    but with no description should be flagged as query_drift.
    """
    sentinel = make_sentinel()
    schema_fields = make_schema_fields(
        [
            {"fieldPath": "flag_override", "nativeDataType": "int", "description": None, "nullable": True},
            {"fieldPath": "created_at", "nativeDataType": "timestamp", "description": "Row creation time", "nullable": False},
        ]
    )
    queries = make_queries(
        [
            "SELECT * FROM t WHERE flag_override = 2",
            "SELECT flag_override, id FROM t",
            "SELECT id FROM t WHERE flag_override = 1",
            "SELECT id FROM t",  # doesn't mention flag_override
        ]
    )

    findings = sentinel._find_query_drift("urn:li:dataset:(test,ds,PROD)", schema_fields, queries)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_type == FindingType.QUERY_DRIFT
    assert "flag_override" in finding.summary
    assert 0.5 <= finding.confidence <= 0.9
    assert any("flag_override" in q for q in finding.unresolved_questions)
    assert any("word-boundary" in a for a in finding.assumptions_made)


def test_does_not_flag_documented_columns():
    sentinel = make_sentinel()
    schema_fields = make_schema_fields(
        [
            {"fieldPath": "field_foo", "nativeDataType": "varchar(100)", "description": "Foo field description", "nullable": False},
        ]
    )
    queries = make_queries(["SELECT field_foo FROM t"] * 5)

    findings = sentinel._find_query_drift("urn:li:dataset:(test,ds,PROD)", schema_fields, queries)

    assert findings == []


def test_does_not_flag_rarely_queried_undocumented_columns():
    """A column mentioned in only 1 of 10 queries shouldn't clear the
    QUERY_DRIFT_MIN_FRACTION (0.3) threshold."""
    sentinel = make_sentinel()
    schema_fields = make_schema_fields(
        [
            {"fieldPath": "rarely_used", "nativeDataType": "varchar(50)", "description": None, "nullable": True},
        ]
    )
    queries = make_queries(
        ["SELECT rarely_used FROM t"] + ["SELECT id FROM t"] * 9
    )

    findings = sentinel._find_query_drift("urn:li:dataset:(test,ds,PROD)", schema_fields, queries)

    assert findings == []


def test_handles_empty_schema_fields():
    sentinel = make_sentinel()
    schema_fields = make_schema_fields([])
    queries = make_queries(["SELECT 1"])

    findings = sentinel._find_query_drift("urn:li:dataset:(test,ds,PROD)", schema_fields, queries)

    assert findings == []


def test_handles_no_queries():
    sentinel = make_sentinel()
    schema_fields = make_schema_fields(
        [{"fieldPath": "x", "nativeDataType": "int", "description": None}]
    )
    queries = make_queries([])

    findings = sentinel._find_query_drift("urn:li:dataset:(test,ds,PROD)", schema_fields, queries)

    assert findings == []


def test_word_boundary_avoids_substring_false_positive():
    """`id` shouldn't match inside `valid` or `paid_at` — this is a
    known limitation documented in agent.py's docstring, and this test
    pins down that the word-boundary regex actually protects against
    the simplest case of it."""
    sentinel = make_sentinel()
    schema_fields = make_schema_fields(
        [{"fieldPath": "id", "nativeDataType": "int", "description": None, "nullable": False}]
    )
    queries = make_queries(
        ["SELECT valid, paid_at FROM t"] * 5  # mentions "id" only as a substring
    )

    findings = sentinel._find_query_drift("urn:li:dataset:(test,ds,PROD)", schema_fields, queries)

    assert findings == []


# --- _find_documentation_gaps() -------------------------------------------


def make_entity(description):
    return _mock_tool_result(
        {
            "result": [
                {
                    "urn": "urn:li:dataset:(test,ds,PROD)",
                    "name": "ds",
                    "properties": {
                        "name": "ds",
                        "description": description,
                    },
                }
            ]
        }
    )


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, response_text):
        self._response_text = response_text
        self.last_call_kwargs = None

    def create(self, **kwargs):
        self.last_call_kwargs = kwargs
        return _FakeMessage(self._response_text)


class _FakeAnthropicClient:
    def __init__(self, response_text, api_key=None):
        self.messages = _FakeMessages(response_text)


def _install_fake_anthropic(monkeypatch, response_text):
    """
    Monkeypatch cairn.agent.anthropic to a fake module whose Anthropic()
    constructor returns a client wired to return `response_text` from
    messages.create(). Returns a dict that will hold the constructed
    client so tests can inspect what prompt/model was actually sent.
    """
    captured = {}

    def fake_anthropic_constructor(api_key=None):
        client = _FakeAnthropicClient(response_text, api_key=api_key)
        captured["client"] = client
        return client

    fake_module = SimpleNamespace(Anthropic=fake_anthropic_constructor)
    monkeypatch.setattr("cairn.agent.anthropic", fake_module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return captured


def test_doc_gap_skips_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sentinel = make_sentinel()
    entity = make_entity("Some description")
    schema_fields = make_schema_fields([{"fieldPath": "x", "description": None}])

    findings = sentinel._find_documentation_gaps(
        "urn:li:dataset:(test,ds,PROD)", entity, schema_fields, None
    )

    assert findings == []


def test_doc_gap_skips_without_anthropic_package(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr("cairn.agent.anthropic", None)
    sentinel = make_sentinel()
    entity = make_entity("Some description")
    schema_fields = make_schema_fields([{"fieldPath": "x", "description": None}])

    findings = sentinel._find_documentation_gaps(
        "urn:li:dataset:(test,ds,PROD)", entity, schema_fields, None
    )

    assert findings == []


def test_doc_gap_skips_entity_with_no_description(monkeypatch):
    captured = _install_fake_anthropic(monkeypatch, '{"confidence": 0.2, "gap": "whatever"}')
    sentinel = make_sentinel()
    entity = make_entity(None)
    schema_fields = make_schema_fields([{"fieldPath": "x", "description": None}])

    findings = sentinel._find_documentation_gaps(
        "urn:li:dataset:(test,ds,PROD)", entity, schema_fields, None
    )

    assert findings == []
    # Should not even have called the LLM — no description to check.
    assert "client" not in captured


def test_doc_gap_flags_stale_description(monkeypatch):
    """
    The model reports low confidence the description is current — Cairn
    should invert that into a high finding confidence and surface the
    gap it identified.
    """
    _install_fake_anthropic(
        monkeypatch,
        '{"confidence": 0.1, "gap": "Description mentions daily batch loads but schema now shows real-time streaming fields."}',
    )
    sentinel = make_sentinel()
    entity = make_entity("Loaded once per day via nightly batch job.")
    schema_fields = make_schema_fields(
        [{"fieldPath": "event_ts", "nativeDataType": "timestamp", "description": "Streaming event timestamp"}]
    )

    findings = sentinel._find_documentation_gaps(
        "urn:li:dataset:(test,ds,PROD)", entity, schema_fields, None
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_type == FindingType.DOCUMENTATION_GAP
    assert finding.confidence == pytest.approx(0.9, abs=0.01)  # round(1 - 0.1, 2)
    assert "real-time streaming" in finding.unresolved_questions[0]
    assert any("sonnet" in a.lower() for a in finding.assumptions_made)


def test_doc_gap_does_not_flag_current_description(monkeypatch):
    _install_fake_anthropic(monkeypatch, '{"confidence": 0.95, "gap": ""}')
    sentinel = make_sentinel()
    entity = make_entity("Accurate, up-to-date description.")
    schema_fields = make_schema_fields([{"fieldPath": "x", "description": "documented"}])

    findings = sentinel._find_documentation_gaps(
        "urn:li:dataset:(test,ds,PROD)", entity, schema_fields, None
    )

    assert findings == []


def test_doc_gap_handles_malformed_llm_response(monkeypatch):
    """If the model doesn't return valid JSON, Cairn should log and
    skip rather than crash the whole inspection run."""
    _install_fake_anthropic(monkeypatch, "Sorry, I can't help with that in JSON form.")
    sentinel = make_sentinel()
    entity = make_entity("Some description.")
    schema_fields = make_schema_fields([{"fieldPath": "x", "description": None}])

    findings = sentinel._find_documentation_gaps(
        "urn:li:dataset:(test,ds,PROD)", entity, schema_fields, None
    )

    assert findings == []


def test_doc_gap_passes_lineage_as_context_without_parsing(monkeypatch):
    """
    Lineage's exact shape wasn't independently verified against a live
    instance (unlike get_entities / list_schema_fields), so it should
    be passed into the prompt as raw JSON rather than field-parsed —
    this test pins that contract down.
    """
    captured = _install_fake_anthropic(monkeypatch, '{"confidence": 0.8, "gap": ""}')
    sentinel = make_sentinel()
    entity = make_entity("Some description.")
    schema_fields = make_schema_fields([{"fieldPath": "x", "description": "documented"}])
    lineage = _mock_tool_result({"some_unverified_shape": {"upstreams": ["urn:li:dataset:(test,upstream,PROD)"]}})

    sentinel._find_documentation_gaps(
        "urn:li:dataset:(test,ds,PROD)", entity, schema_fields, lineage
    )

    sent_prompt = captured["client"].messages.last_call_kwargs["messages"][0]["content"]
    assert "some_unverified_shape" in sent_prompt
    assert "urn:li:dataset:(test,upstream,PROD)" in sent_prompt