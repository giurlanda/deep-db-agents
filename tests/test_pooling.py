from __future__ import annotations

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.guardrails import GuardrailConfig
from deep_db_agents.pooling import LazyClient


def test_lazy_client_builds_once_and_reuses():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return object()

    cache = LazyClient()
    a = cache.get(factory)
    b = cache.get(factory)
    assert a is b
    assert calls["n"] == 1


def test_lazy_client_close_rebuilds():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1

        class C:
            closed = False

            def close(self):
                type(self).closed = True

        return C()

    cache = LazyClient()
    first = cache.get(factory)
    cache.close()
    assert first.closed
    cache.get(factory)
    assert calls["n"] == 2


def test_search_dialect_reuses_client(monkeypatch):
    # Il client ES/OS viene costruito una sola volta e riusato su più tool call.
    from deep_db_agents.dialects.elasticsearch import ElasticsearchDialect

    calls = {"n": 0}

    class FakeClient:
        cat = None

        def count(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
            return {"count": 0}

        def search(self, index=None, body=None, request_timeout=None):  # noqa: ARG002
            return {"hits": {"hits": []}}

        def close(self):
            pass

    def fake_connect(conn, guardrails=None):  # noqa: ARG001
        calls["n"] += 1
        return FakeClient()

    dialect = ElasticsearchDialect()
    monkeypatch.setattr(dialect, "_connect", fake_connect)
    conn = ConnectionConfig(scheme="elasticsearch", host="localhost", port=9200, credential={})
    tools = {t.name: t for t in dialect.build_tools(conn, GuardrailConfig())}

    tools["count_documents"].invoke({})
    tools["sample_documents"].invoke({})
    tools["run_query"].invoke({})
    assert calls["n"] == 1  # un solo client costruito, riusato su 3 tool call


def test_mongo_reuses_client(monkeypatch):
    from deep_db_agents.dialects.mongodb import MongoDBDialect

    calls = {"n": 0}

    class FakeCollection:
        def count_documents(self, query, maxTimeMS=None):  # noqa: ARG002, N803
            return 0

        def find(self, query=None, projection=None, skip=0, limit=0):  # noqa: ARG002
            return []

    class FakeClient:
        def __getitem__(self, name):  # noqa: ARG002
            return {"ordini": FakeCollection()}

        def close(self):
            pass

    def fake_connect(conn, socket_timeout_ms=None):  # noqa: ARG001
        calls["n"] += 1
        return FakeClient()

    dialect = MongoDBDialect()
    monkeypatch.setattr("deep_db_agents.dialects.mongodb.tools.connect", fake_connect)
    conn = ConnectionConfig(
        scheme="mongodb", host="localhost", port=27017, credential={"database": "shop"}
    )
    tools = {t.name: t for t in dialect.build_tools(conn, GuardrailConfig())}
    tools["count_documents"].invoke({"collection": "ordini"})
    tools["sample_documents"].invoke({"collection": "ordini"})
    assert calls["n"] == 1  # un solo MongoClient costruito, riusato
