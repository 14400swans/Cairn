"""
Unit tests for Sentinel._find_query_drift() — the one piece of agent.py
that involves actual reasoning logic rather than pure orchestration.

Why mock data instead of a live DataHub connection: the healthcare/
quickstart sample pack turned out not to contain any dataset with both
undocumented fields AND query history at the same time (verified
manually against a running instance on 2026-07-13 — see
DEVELOPMENT_NOTES.md). Rather than depend on a live instance having the
right shape of data at demo time, this constructs mock MCP responses
matching the exact structure confirmed against a real
mcp-server-datahub v0.6.0 instance, and tests the diffing logic directly.

Run with: pytest tests/test_agent.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

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


def make_sentinel() -> Sentinel:
    # _find_query_drift doesn't touch self.client or self.gate, so both
    # can be None for this unit test — only the pure diffing logic is
    # under test here.
    return Sentinel(client=None, gate=None)  # type: ignore[arg-type]


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