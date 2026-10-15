from __future__ import annotations

from contextlib import contextmanager

import pytest

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.mongodb import MongoDBDialect
from deep_db_agents.exceptions import QueryNotAllowedError
from deep_db_agents.guardrails import GuardrailConfig

CONN = ConnectionConfig(
    scheme="mongodb", host="localhost", port=27017, credential={"database": "shop"}
)


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def count_documents(self, query, maxTimeMS=None):  # noqa: ARG002, N803 - firma driver
        return len(self.docs)

    def find(self, query=None, projection=None, skip=0, limit=0):  # noqa: ARG002
        result = self.docs[skip:]
        return result[:limit] if limit else result

    def aggregate(self, pipeline, maxTimeMS=None):  # noqa: ARG002, N803 - firma driver
        return list(self.docs)


class FakeDB:
    def __init__(self, collections):
        self._collections = collections

    def list_collection_names(self):
        return list(self._collections)

    def __getitem__(self, name):
        return self._collections[name]


def _make(monkeypatch, docs, guardrails=None):
    dialect = MongoDBDialect()
    db = FakeDB({"ordini": FakeCollection(docs)})

    @contextmanager
    def fake_db(conn, guardrails=None):  # noqa: ARG001
        yield db

    monkeypatch.setattr(dialect, "_db", fake_db)
    built = dialect.build_tools(CONN, guardrails or GuardrailConfig(), materialize_enable=True)
    return {t.name: t for t in built}


def test_find_forces_limit_and_counts(monkeypatch):
    docs = [{"_id": i, "regione": "Toscana"} for i in range(500)]
    tools = _make(monkeypatch, docs, GuardrailConfig(default_rows=10))
    out = tools["find"].invoke({"collection": "ordini"})
    assert "10 documents" in out


def test_find_blocked_over_estimate(monkeypatch):
    docs = [{"_id": i} for i in range(50)]
    tools = _make(monkeypatch, docs, GuardrailConfig(explain_row_threshold=10))
    # The estimate block is reflected back as corrective feedback, not raised.
    out = tools["find"].invoke({"collection": "ordini"})
    assert "was not executed" in out.lower()
    assert "aggregate" in out.lower()


def test_aggregate_rejects_write_stage(monkeypatch):
    tools = _make(monkeypatch, [{"_id": 1}])
    out = tools["aggregate"].invoke({"collection": "ordini", "pipeline": '[{"$out": "altra"}]'})
    assert "was not executed by the database" in out.lower()


def test_aggregate_invalid_json(monkeypatch):
    tools = _make(monkeypatch, [{"_id": 1}])
    out = tools["aggregate"].invoke({"collection": "ordini", "pipeline": "{non-json"})
    assert "was not executed by the database" in out.lower()


def test_find_rejects_where_js_operator(monkeypatch):
    # $where esegue JavaScript lato server: va bloccato prima del driver, con feedback.
    tools = _make(monkeypatch, [{"_id": 1}])
    out = tools["find"].invoke({"collection": "ordini", "filter": '{"$where": "this.x == 1"}'})
    assert "was not executed by the database" in out.lower()
    assert "$where" in out


def test_count_rejects_function_js_operator(monkeypatch):
    tools = _make(monkeypatch, [{"_id": 1}])
    js = '{"$expr": {"$function": {"body": "x", "args": [], "lang": "js"}}}'
    out = tools["count_documents"].invoke({"collection": "ordini", "filter": js})
    assert "was not executed by the database" in out.lower()
    assert "$function" in out


def test_aggregate_rejects_nested_write_stage(monkeypatch):
    # $out annidato in un $facet: la scansione ricorsiva deve comunque rifiutarlo.
    tools = _make(monkeypatch, [{"_id": 1}])
    out = tools["aggregate"].invoke(
        {"collection": "ordini", "pipeline": '[{"$facet": {"a": [{"$out": "altra"}]}}]'}
    )
    assert "was not executed by the database" in out.lower()
    assert "$out" in out


def test_describe_collection_infers_schema(monkeypatch):
    docs = [{"_id": 1, "importo": 10}, {"_id": 2, "importo": 20, "nota": "x"}]
    tools = _make(monkeypatch, docs)
    out = tools["describe_collection"].invoke({"collection": "ordini"})
    assert "importo" in out and "nota" in out


class BoomCollection:
    """Collezione il cui driver solleva un errore su find/aggregate (query non valida)."""

    def __init__(self, message="unknown operator: $boom"):
        self.message = message

    def count_documents(self, query, maxTimeMS=None):  # noqa: ARG002, N803 - firma driver
        raise RuntimeError(self.message)

    def find(self, query=None, projection=None, skip=0, limit=0):  # noqa: ARG002
        raise RuntimeError(self.message)

    def aggregate(self, pipeline, maxTimeMS=None):  # noqa: ARG002, N803 - firma driver
        raise RuntimeError(self.message)


def _make_boom(monkeypatch, message="unknown operator: $boom"):
    dialect = MongoDBDialect()
    db = FakeDB({"ordini": BoomCollection(message)})

    @contextmanager
    def fake_db(conn, guardrails=None):  # noqa: ARG001
        yield db

    monkeypatch.setattr(dialect, "_db", fake_db)
    built = dialect.build_tools(CONN, GuardrailConfig(), materialize_enable=True)
    return {t.name: t for t in built}


def test_find_returns_feedback_on_driver_error(monkeypatch):
    tools = _make_boom(monkeypatch)
    out = tools["find"].invoke({"collection": "ordini", "filter": '{"x": {"$boom": 1}}'})
    assert "was not executed by the database" in out.lower()
    assert "unknown operator: $boom" in out


def test_aggregate_returns_feedback_on_driver_error(monkeypatch):
    tools = _make_boom(monkeypatch)
    out = tools["aggregate"].invoke(
        {"collection": "ordini", "pipeline": '[{"$group": {"_id": "$x"}}]'}
    )
    assert "unknown operator: $boom" in out
    assert "pipeline" in out.lower()


def test_materialize_aggregate_bounded_by_bytes(monkeypatch):
    from types import SimpleNamespace

    from deepagents.backends.protocol import BackendProtocol, WriteResult
    from langgraph.types import Command

    class RecordingBackend(BackendProtocol):
        def __init__(self):
            self.written: dict[str, str] = {}

        def write(self, path, content):
            self.written[path] = content
            return WriteResult(path=path)

    dialect = MongoDBDialect()
    docs = [{"_id": i, "payload": "x" * 20} for i in range(2000)]
    db = FakeDB({"ordini": FakeCollection(docs)})

    @contextmanager
    def fake_db(conn, guardrails=None):  # noqa: ARG001
        yield db

    monkeypatch.setattr(dialect, "_db", fake_db)
    backend = RecordingBackend()
    built = dialect.build_tools(
        CONN,
        GuardrailConfig(max_materialized_bytes=300),
        materialize_enable=True,
        backend=backend,
    )
    tools = {t.name: t for t in built}
    out = tools["materialize_aggregate"].func(
        runtime=SimpleNamespace(tool_call_id="call-1"),
        collection="ordini",
        pipeline="[]",
        filename="out.csv",
    )
    assert isinstance(out, Command)
    message = out.update["messages"][0].content
    assert "INCOMPLETE" in message
    assert len(backend.written["/out.csv"].encode("utf-8")) <= 300


def test_missing_database_raises(monkeypatch):
    dialect = MongoDBDialect()
    conn = ConnectionConfig(scheme="mongodb", host="localhost", port=27017, credential={})
    built = dialect.build_tools(conn, GuardrailConfig(), materialize_enable=True)
    tools = {t.name: t for t in built}
    # Nessun patch su _db: il controllo sul database mancante deve scattare.
    with pytest.raises(QueryNotAllowedError):
        tools["list_collections"].invoke({})


class RaisingCollection:
    """Collection whose driver raises the given exception on every operation."""

    def __init__(self, exc):
        self.exc = exc

    def count_documents(self, query, maxTimeMS=None):  # noqa: ARG002, N803 - driver signature
        raise self.exc

    def find(self, query=None, projection=None, skip=0, limit=0):  # noqa: ARG002
        raise self.exc

    def aggregate(self, pipeline, maxTimeMS=None):  # noqa: ARG002, N803 - driver signature
        raise self.exc


class RaisingDB(FakeDB):
    def __init__(self, exc):
        super().__init__({"ordini": RaisingCollection(exc)})
        self.exc = exc

    def list_collection_names(self):
        raise self.exc


def _make_raising(monkeypatch, exc):
    dialect = MongoDBDialect()
    db = RaisingDB(exc)

    @contextmanager
    def fake_db(conn, guardrails=None):  # noqa: ARG001
        yield db

    monkeypatch.setattr(dialect, "_db", fake_db)
    built = dialect.build_tools(CONN, GuardrailConfig(), materialize_enable=True)
    return {t.name: t for t in built}


_TOOL_CALLS = [
    ("list_collections", {}),
    ("describe_collection", {"collection": "ordini"}),
    ("count_documents", {"collection": "ordini"}),
    ("sample_documents", {"collection": "ordini"}),
    ("find", {"collection": "ordini"}),
    ("aggregate", {"collection": "ordini", "pipeline": "[]"}),
]


def _connection_errors():
    errors = pytest.importorskip("pymongo.errors")
    return [
        errors.ServerSelectionTimeoutError(
            "db:27017: [Errno 61] Connection refused (mongodb://admin:s3cr3t@db:27017)"
        ),
        errors.AutoReconnect("connection closed"),
        errors.ConfigurationError("Unknown option foo"),
        errors.OperationFailure("Authentication failed.", code=18),
        errors.OperationFailure("not authorized on shop to execute command", code=13),
    ]


@pytest.mark.parametrize("tool_name, args", _TOOL_CALLS)
def test_connection_failure_is_reported_as_setup_error(monkeypatch, tool_name, args):
    # The MongoClient connects lazily: connection/auth errors surface on the first operation
    # and must tell the agent to stop, not to rewrite a correct query in a loop.
    for exc in _connection_errors():
        out = _make_raising(monkeypatch, exc)[tool_name].invoke(args)
        assert "Do not modify or retry" in out, (type(exc).__name__, out)
        assert "Fix the" not in out
        assert f"Error type: {type(exc).__name__}" in out
        assert "s3cr3t" not in out


def test_query_errors_and_timeouts_keep_fix_the_query_feedback(monkeypatch):
    errors = pytest.importorskip("pymongo.errors")
    for exc in (
        errors.NetworkTimeout("db:27017: timed out"),  # slow operation: the query is the issue
        errors.ExecutionTimeout("operation exceeded time limit", code=50),
        errors.OperationFailure("unknown operator: $boom", code=2),
    ):
        out = _make_raising(monkeypatch, exc)["find"].invoke({"collection": "ordini"})
        assert "Fix the find" in out, (type(exc).__name__, out)
        assert "Do not modify or retry" not in out
