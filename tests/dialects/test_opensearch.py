from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.opensearch import OpenSearchDialect
from deep_db_agents.guardrails import GuardrailConfig
from deep_db_agents.registry import resolve


def _invoke_aggregate(tool, **kwargs):
    """Invoca ``aggregate`` con un ``ToolRuntime`` fittizio (iniettato da ToolNode a runtime)."""
    runtime = SimpleNamespace(config={"configurable": {}})
    return tool.func(runtime=runtime, **kwargs)


class FakeCat:
    def __init__(self, stats):
        self.stats = stats

    def indices(self, index=None, format=None, request_timeout=None):  # noqa: ARG002
        return self.stats


class FakeIndices:
    def __init__(self, mapping):
        self.mapping = mapping

    def get_mapping(self, index=None, request_timeout=None):  # noqa: ARG002
        return self.mapping


class FakeClient:
    """Client OpenSearch finto: stessa API REST di Elasticsearch."""

    def __init__(self, docs, stats=None, mapping=None):
        self.docs = docs
        self.cat = FakeCat(stats or [{"index": "logs1", "docs.count": str(len(docs))}])
        self.indices = FakeIndices(
            mapping or {"logs1": {"mappings": {"properties": {"level": {"type": "keyword"}}}}}
        )

    def count(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
        return {"count": len(self.docs)}

    def search(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
        size = (body or {}).get("size", len(self.docs))
        offset = (body or {}).get("from", 0)
        hits = self.docs[offset : offset + size]
        res = {"hits": {"hits": hits}}
        if (body or {}).get("aggs"):
            res["aggregations"] = {
                name: {"buckets": [{"key": "x", "doc_count": len(self.docs)}]}
                for name in body["aggs"]
            }
        return res

    def close(self):
        pass


def _hit(i: int) -> dict:
    return {"_id": str(i), "_index": "logs1", "_source": {"level": "info", "n": i}}


def _make(monkeypatch, docs, guardrails=None, credential=None):
    dialect = OpenSearchDialect()
    client = FakeClient(docs)

    @contextmanager
    def fake_client(conn, guardrails=None):  # noqa: ARG001
        yield client

    monkeypatch.setattr(dialect, "_client", fake_client)
    conn = ConnectionConfig(
        scheme="opensearch", host="localhost", port=9200, credential=credential or {}
    )
    built = dialect.build_tools(conn, guardrails or GuardrailConfig())
    return {t.name: t for t in built}


def test_scheme_is_registered():
    assert resolve("opensearch") is OpenSearchDialect


def test_list_indices_reports_doc_counts(monkeypatch):
    tools = _make(monkeypatch, [_hit(i) for i in range(4)])
    out = tools["list_indices"].invoke({})
    assert "logs1" in out and "4 documents" in out


def test_describe_index_reports_fields(monkeypatch):
    tools = _make(monkeypatch, [])
    out = tools["describe_index"].invoke({})
    assert "level:keyword" in out


def test_run_query_forces_limit(monkeypatch):
    docs = [_hit(i) for i in range(100)]
    tools = _make(monkeypatch, docs, GuardrailConfig(default_rows=20))
    out = tools["run_query"].invoke({})
    assert "20 documents" in out


def test_run_query_blocked_over_estimate(monkeypatch):
    docs = [_hit(i) for i in range(50)]
    tools = _make(monkeypatch, docs, GuardrailConfig(explain_row_threshold=10))
    # The estimate block is reflected back as corrective feedback, not raised.
    out = tools["run_query"].invoke({})
    assert "was not executed" in out.lower()
    assert "aggregate" in out.lower()


def test_aggregate_returns_aggregation_results(monkeypatch):
    docs = [_hit(i) for i in range(3)]
    tools = _make(monkeypatch, docs)
    out = _invoke_aggregate(
        tools["aggregate"],
        index="logs1",
        aggs='{"by_level": {"terms": {"field": "level"}}}',
    )
    assert "Aggregations on logs1" in out
    assert "by_level" in out
    assert "buckets" in out


def test_index_outside_scope_is_rejected(monkeypatch):
    tools = _make(monkeypatch, [_hit(0)], credential={"index": "logs1,logs2"})
    out = tools["count_documents"].invoke({"index": "secrets"})
    assert "was not executed" in out.lower()
    assert "secrets" in out
