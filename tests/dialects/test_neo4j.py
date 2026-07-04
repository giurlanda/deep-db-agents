from __future__ import annotations

from contextlib import contextmanager

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.neo4j import Neo4jDialect
from deep_db_agents.guardrails import GuardrailConfig

CONN = ConnectionConfig(scheme="neo4j", host="localhost", port=7687, credential={"user": "u"})


class FakeResult:
    def __init__(self, keys, records):
        self._keys = keys
        self._records = records

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._records)


class FakeRecord:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class FakeTx:
    """Transazione fake: ``run`` delega a un callback per simulare risultati o errori."""

    def __init__(self, on_run):
        self._on_run = on_run

    def run(self, cypher):
        # ``run_cypher`` può passare un ``neo4j.Query`` (per il timeout server-side): il fake
        # lo normalizza a stringa così i callback ``on_run`` restano semplici.
        return self._on_run(str(cypher))


class FakeSession:
    def __init__(self, tx):
        self._tx = tx

    def execute_read(self, work, *args):
        return work(self._tx, *args)


def _tools(monkeypatch, on_run, guardrails=None):
    dialect = Neo4jDialect()
    tx = FakeTx(on_run)

    @contextmanager
    def fake_session(conn, guardrails=None):  # noqa: ARG001
        yield FakeSession(tx)

    monkeypatch.setattr(dialect, "_session", fake_session)
    tools = dialect.build_tools(CONN, guardrails or GuardrailConfig())
    return {t.name: t for t in tools}


def test_run_cypher_collects_rows(monkeypatch):
    def on_run(cypher):
        if cypher.upper().startswith("EXPLAIN"):
            raise RuntimeError("no plan")  # stima best-effort: verrà ignorata
        return FakeResult(["n"], [FakeRecord({"n": 1}), FakeRecord({"n": 2})])

    tools = _tools(monkeypatch, on_run)
    out = tools["run_cypher"].invoke({"cypher": "MATCH (n) RETURN n"})
    assert "2 rows" in out


def test_run_cypher_returns_feedback_on_driver_error(monkeypatch):
    # Un errore del driver (es. sintassi Cypher) diventa feedback, non un'eccezione.
    def on_run(cypher):
        if cypher.upper().startswith("EXPLAIN"):
            raise RuntimeError("no plan")  # ignorato dalla stima best-effort
        raise RuntimeError("Invalid input 'RETUN': expected RETURN")

    tools = _tools(monkeypatch, on_run)
    out = tools["run_cypher"].invoke({"cypher": "MATCH (n) RETUN n"})
    assert "was not executed by the database" in out.lower()
    assert "Invalid input 'RETUN'" in out
    assert "MATCH (n) RETUN n" in out  # query inviata riportata nel feedback


def test_run_cypher_rejects_write_clause(monkeypatch):
    # Le violazioni di whitelist non vengono eseguite: sono riportate come feedback correttivo.
    tools = _tools(monkeypatch, lambda cypher: FakeResult([], []))
    out = tools["run_cypher"].invoke({"cypher": "CREATE (n:Person)"})
    assert "was not executed by the database" in out.lower()


def test_count_nodes_escapes_label(monkeypatch):
    # Una label con backtick non deve rompere il quoting: i backtick sono raddoppiati.
    seen = {}

    def on_run(cypher):
        seen["cypher"] = cypher
        return FakeResult(["c"], [FakeRecord({"c": 0})])

    tools = _tools(monkeypatch, on_run)
    tools["count_nodes"].invoke({"label": "Lab`el"})
    assert "`Lab``el`" in seen["cypher"]  # backtick raddoppiato dentro l'identificatore quotato


def test_materialize_cypher_gated_and_unique():
    # materialize_cypher è esposto solo con materialize_enable=True, senza duplicati:
    # regressione sul doppio append (una volta nella lista base, una nel branch).
    dialect = Neo4jDialect()
    without = dialect.build_tools(CONN, GuardrailConfig(), materialize_enable=False)
    with_mat = dialect.build_tools(CONN, GuardrailConfig(), materialize_enable=True)

    names_without = [t.name for t in without]
    names_with = [t.name for t in with_mat]
    assert "materialize_cypher" not in names_without
    assert names_with.count("materialize_cypher") == 1
    assert len(names_with) == len(set(names_with))  # nessun nome tool duplicato
