from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.elasticsearch import ElasticsearchDialect
from deep_db_agents.guardrails import GuardrailConfig


def _invoke_aggregate(tool, **kwargs):
    """Invoca ``aggregate`` chiamando la funzione grezza con un ``ToolRuntime`` fittizio.

    ``ToolRuntime`` è iniettato da ToolNode a runtime e non è fornibile via ``.invoke`` nei
    test unitari: passiamo uno stub con il solo ``tool_call_id`` che il tool usa quando
    materializza l'output su file tramite ``write_command``.
    """
    runtime = SimpleNamespace(tool_call_id="call-1")
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


def _make(
    monkeypatch, docs, guardrails=None, credential=None, stats=None, mapping=None, backend=None
):
    dialect = ElasticsearchDialect()
    client = FakeClient(docs, stats=stats, mapping=mapping)

    @contextmanager
    def fake_client(conn, guardrails=None):  # noqa: ARG001
        yield client

    monkeypatch.setattr(dialect, "_client", fake_client)
    conn = ConnectionConfig(
        scheme="elasticsearch", host="localhost", port=9200, credential=credential or {}
    )
    built = dialect.build_tools(
        conn, guardrails or GuardrailConfig(), materialize_enable=True, backend=backend
    )
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
    # The estimate block is reflected back as corrective feedback, not raised.
    out = tools["run_query"].invoke({})
    assert "was not executed" in out.lower()
    assert "aggregate" in out.lower()


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

    from deep_db_agents.dialects import search_base

    class RecordingBackend(BackendProtocol):
        def __init__(self):
            self.written: dict[str, str] = {}

        def write(self, path, content):
            self.written[path] = content
            return WriteResult(path=path)

    monkeypatch.setattr(search_base, "MAX_AGG_INLINE_CHARS", 5)
    backend = RecordingBackend()
    # Backend injected directly into the tool closure (no more BERegistry indirection).
    tools = _make(monkeypatch, [_hit(i) for i in range(3)], backend=backend)
    out = _invoke_aggregate(
        tools["aggregate"],
        index="sakila1",
        aggs='{"by_title": {"terms": {"field": "title"}}}',
    )
    # The tool now returns a Command carrying the tool message with the preview.
    message = out.update["messages"][0].content
    assert "saved to aggregations_" in message
    assert "preview" in message.lower()
    # Il payload completo è finito su file, non nel contesto.
    assert len(backend.written) == 1
    saved = next(iter(backend.written.values()))
    assert "by_title" in saved and "buckets" in saved


def test_materialize_query_bounded_by_bytes(monkeypatch):
    from types import SimpleNamespace

    from deepagents.backends.protocol import BackendProtocol, WriteResult
    from langgraph.types import Command

    class RecordingBackend(BackendProtocol):
        def __init__(self):
            self.written: dict[str, str] = {}

        def write(self, path, content):
            self.written[path] = content
            return WriteResult(path=path)

    hits = [
        {"_id": str(i), "_index": "sakila1", "_source": {"title": "x" * 20}} for i in range(1000)
    ]
    backend = RecordingBackend()
    tools = _make(
        monkeypatch, hits, guardrails=GuardrailConfig(max_materialized_bytes=300), backend=backend
    )
    out = tools["materialize_query"].func(
        runtime=SimpleNamespace(tool_call_id="call-1"), index="sakila1", filename="out.csv"
    )
    assert isinstance(out, Command)
    message = out.update["messages"][0].content
    assert "INCOMPLETE" in message
    assert len(backend.written["/out.csv"].encode("utf-8")) <= 300


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
