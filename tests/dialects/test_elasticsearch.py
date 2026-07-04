from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.elasticsearch import ElasticsearchDialect
from deep_db_agents.exceptions import GuardrailError
from deep_db_agents.guardrails import GuardrailConfig


def _invoke_aggregate(tool, *, be_uuid=None, **kwargs):
    """Invoca ``aggregate`` chiamando la funzione grezza con un ``ToolRuntime`` fittizio.

    ``ToolRuntime`` è iniettato da ToolNode a runtime e non è fornibile via ``.invoke`` nei
    test unitari: passiamo uno stub con il solo ``config`` che il tool interroga.
    """
    configurable = {"be_uuid": be_uuid} if be_uuid is not None else {}
    runtime = SimpleNamespace(config={"configurable": configurable})
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
    """Client Elasticsearch finto: risponde con gli ``hits`` configurati."""

    def __init__(self, docs, stats=None, mapping=None):
        self.docs = docs
        self.cat = FakeCat(stats or [{"index": "sakila1", "docs.count": str(len(docs))}])
        self.indices = FakeIndices(
            mapping or {"sakila1": {"mappings": {"properties": {"title": {"type": "text"}}}}}
        )

    def count(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
        return {"count": len(self.docs)}

    def search(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
        size = (body or {}).get("size", len(self.docs))
        offset = (body or {}).get("from", 0)
        hits = self.docs[offset : offset + size]
        res = {"hits": {"hits": hits}}
        if (body or {}).get("aggs"):
            # Eco delle aggregazioni richieste, con un bucket fittizio per verifica.
            res["aggregations"] = {
                name: {"buckets": [{"key": "x", "doc_count": len(self.docs)}]}
                for name in body["aggs"]
            }
        return res

    def close(self):
        pass


def _hit(i: int) -> dict:
    return {"_id": str(i), "_index": "sakila1", "_source": {"title": f"film {i}"}}


def _make(monkeypatch, docs, guardrails=None, credential=None, stats=None, mapping=None):
    dialect = ElasticsearchDialect()
    client = FakeClient(docs, stats=stats, mapping=mapping)

    @contextmanager
    def fake_client(conn, guardrails=None):  # noqa: ARG001
        yield client

    monkeypatch.setattr(dialect, "_client", fake_client)
    conn = ConnectionConfig(
        scheme="elasticsearch", host="localhost", port=9200, credential=credential or {}
    )
    built = dialect.build_tools(conn, guardrails or GuardrailConfig(), materialize_enable=True)
    return {t.name: t for t in built}


def test_list_indices_reports_doc_counts(monkeypatch):
    tools = _make(monkeypatch, [_hit(i) for i in range(3)])
    out = tools["list_indices"].invoke({})
    assert "sakila1" in out and "3 documents" in out


def test_describe_index_reports_fields(monkeypatch):
    tools = _make(monkeypatch, [])
    out = tools["describe_index"].invoke({})
    assert "title:text" in out


def test_count_documents(monkeypatch):
    tools = _make(monkeypatch, [_hit(i) for i in range(5)])
    out = tools["count_documents"].invoke({})
    assert "5 documents" in out


def test_run_query_forces_limit_and_paginates(monkeypatch):
    docs = [_hit(i) for i in range(500)]
    tools = _make(monkeypatch, docs, GuardrailConfig(default_rows=10))
    out = tools["run_query"].invoke({})
    assert "10 documents" in out
    assert "page=1" in out


def test_run_query_blocked_over_estimate(monkeypatch):
    docs = [_hit(i) for i in range(50)]
    tools = _make(monkeypatch, docs, GuardrailConfig(explain_row_threshold=10))
    with pytest.raises(GuardrailError):
        tools["run_query"].invoke({})


def test_search_query_uses_query_string(monkeypatch):
    docs = [_hit(i) for i in range(3)]
    tools = _make(monkeypatch, docs)
    out = tools["search_query"].invoke({"query_string": "title:film"})
    assert "3 documents" in out


def test_sample_documents(monkeypatch):
    docs = [_hit(i) for i in range(2)]
    tools = _make(monkeypatch, docs)
    out = tools["sample_documents"].invoke({})
    assert "2 documents" in out


def test_aggregate_returns_aggregation_results(monkeypatch):
    docs = [_hit(i) for i in range(5)]
    tools = _make(monkeypatch, docs)
    out = _invoke_aggregate(
        tools["aggregate"],
        index="sakila1",
        aggs='{"by_title": {"terms": {"field": "title"}}}',
    )
    assert "Aggregations on sakila1" in out
    assert "by_title" in out
    assert "buckets" in out


def test_aggregate_truncates_large_output_without_backend(monkeypatch):
    from deep_db_agents.dialects import search_base

    # Abbassa la soglia così l'output (anche piccolo) del fake supera il cap.
    monkeypatch.setattr(search_base, "MAX_AGG_INLINE_CHARS", 5)
    tools = _make(monkeypatch, [_hit(i) for i in range(3)])
    out = _invoke_aggregate(
        tools["aggregate"],
        index="sakila1",
        aggs='{"by_title": {"terms": {"field": "title"}}}',
    )
    assert "too large" in out.lower()
    assert "truncated to" in out.lower()
    assert "reduce the buckets' 'size'" in out.lower()


def test_aggregate_materializes_large_output_with_backend(monkeypatch):
    from deepagents.backends.protocol import BackendProtocol, WriteResult

    from deep_db_agents.backend_registry import BERegistry
    from deep_db_agents.dialects import search_base

    class RecordingBackend(BackendProtocol):
        def __init__(self):
            self.written: dict[str, str] = {}

        def write(self, path, content):
            self.written[path] = content
            return WriteResult(path=path)

    monkeypatch.setattr(search_base, "MAX_AGG_INLINE_CHARS", 5)
    backend = RecordingBackend()
    key = BERegistry().add(backend)
    try:
        tools = _make(monkeypatch, [_hit(i) for i in range(3)])
        out = _invoke_aggregate(
            tools["aggregate"],
            be_uuid=key,
            index="sakila1",
            aggs='{"by_title": {"terms": {"field": "title"}}}',
        )
    finally:
        BERegistry().remove(key)
    assert "saved to aggregations_" in out
    assert "preview" in out.lower()
    # Il payload completo è finito su file, non nel contesto.
    assert len(backend.written) == 1
    saved = next(iter(backend.written.values()))
    assert "by_title" in saved and "buckets" in saved


def test_aggregate_requires_non_empty_aggs(monkeypatch):
    tools = _make(monkeypatch, [])
    out = _invoke_aggregate(tools["aggregate"], aggs="")
    assert "was not executed" in out.lower()


def test_aggregate_invalid_json_returns_feedback(monkeypatch):
    tools = _make(monkeypatch, [])
    out = _invoke_aggregate(tools["aggregate"], aggs="{non-json")
    assert "was not executed" in out.lower()


def test_run_query_invalid_json(monkeypatch):
    tools = _make(monkeypatch, [])
    out = tools["run_query"].invoke({"query": "{non-json"})
    assert "was not executed" in out.lower()


def test_index_outside_scope_is_rejected(monkeypatch):
    tools = _make(monkeypatch, [_hit(0)], credential={"index": "sakila*"})
    out = tools["describe_index"].invoke({"index": "other_index"})
    assert "was not executed" in out.lower()
    assert "other_index" in out


def test_index_inside_scope_is_allowed(monkeypatch):
    tools = _make(monkeypatch, [_hit(0)], credential={"index": "sakila*"})
    out = tools["count_documents"].invoke({"index": "sakila1"})
    assert "documents" in out


def test_search_query_index_outside_scope_returns_feedback(monkeypatch):
    # search_query deve convertire la violazione di scope in feedback, come run_query,
    # invece di ri-sollevare QueryNotAllowedError al framework.
    tools = _make(monkeypatch, [_hit(0)], credential={"index": "sakila*"})
    out = tools["search_query"].invoke({"index": "other_index", "query_string": "title:film"})
    assert "was not executed" in out.lower()
    assert "other_index" in out


class BoomClient(FakeClient):
    """Client il cui driver solleva un errore su count/search (query non valida)."""

    def count(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
        raise RuntimeError("parsing_exception: unknown field [boom]")

    def search(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
        raise RuntimeError("parsing_exception: unknown field [boom]")


def test_run_query_returns_feedback_on_driver_error(monkeypatch):
    dialect = ElasticsearchDialect()
    client = BoomClient([])

    @contextmanager
    def fake_client(conn, guardrails=None):  # noqa: ARG001
        yield client

    monkeypatch.setattr(dialect, "_client", fake_client)
    conn = ConnectionConfig(scheme="elasticsearch", host="localhost", port=9200, credential={})
    tools = {t.name: t for t in dialect.build_tools(conn, GuardrailConfig())}
    out = tools["run_query"].invoke({"query": '{"term": {"boom": 1}}'})
    assert "was not executed" in out.lower()
    assert "unknown field [boom]" in out
