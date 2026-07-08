from __future__ import annotations

from types import SimpleNamespace

import pytest

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.mysql import MySQLDialect
from deep_db_agents.exceptions import QueryNotAllowedError
from deep_db_agents.guardrails import GuardrailConfig

CONN = ConnectionConfig(scheme="mysql", host="localhost", port=3306, credential={"user": "u"})


def _handler(estimate=5, data_rows=None):
    data_rows = data_rows if data_rows is not None else [(1, "a"), (2, "b")]

    def handler(sql, params):
        s = sql.strip().upper()
        if s.startswith("SET"):
            return [], []
        if s.startswith("EXPLAIN"):
            return [("rows",)], [(estimate,)]
        if s.startswith("SELECT COUNT(*)"):
            return [("c",)], [(42,)]
        # query dati
        return [("id",), ("name",)], data_rows

    return handler


def _tools(make_dialect, handler, guardrails=None):
    dialect, cursor = make_dialect(MySQLDialect, handler)
    tools = dialect.build_tools(CONN, guardrails or GuardrailConfig())
    return {t.name: t for t in tools}, cursor


def test_run_query_forces_limit(make_dialect):
    tools, cursor = _tools(make_dialect, _handler())
    out = tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    assert "2 rows" in out
    paginated = [sql for sql, _ in cursor.executed if "_q" in sql][0]
    assert "LIMIT 100" in paginated  # default_rows applicato


def test_run_query_rejects_non_select(make_dialect):
    tools, _ = _tools(make_dialect, _handler())
    out = tools["run_query"].invoke({"sql": "DELETE FROM ordini"})
    assert "was not executed" in out.lower()
    assert "DELETE FROM ordini" in out


def test_run_query_blocked_over_explain_threshold(make_dialect):
    tools, cursor = _tools(
        make_dialect,
        _handler(estimate=10_000_000),
        guardrails=GuardrailConfig(explain_row_threshold=1000),
    )
    out = tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    # The estimate block is reflected back as corrective feedback, not raised.
    assert "was not executed" in out.lower()
    assert "aggregate" in out.lower()
    # The data query was never run: only SET + EXPLAIN reached the driver.
    assert not any("_q" in sql for sql, _ in cursor.executed)


def test_count_rows(make_dialect):
    tools, _ = _tools(make_dialect, _handler())
    out = tools["count_rows"].invoke({"table": "ordini"})
    assert "42" in out


def test_describe_table_quotes_identifier(make_dialect):
    tools, _ = _tools(make_dialect, _handler())
    with pytest.raises(QueryNotAllowedError):
        tools["count_rows"].invoke({"table": "bad name; DROP"})


def _raising_handler(message, on="SELECT * FROM ("):
    """Handler che solleva un errore del driver quando esegue la query dati."""

    def handler(sql, params):
        s = sql.strip()
        if s.upper().startswith(("SET", "EXPLAIN")):
            return [], [] if s.upper().startswith("SET") else [(5,)]
        if on in sql:
            raise RuntimeError(message)
        return [("id",), ("name",)], [(1, "a")]

    return handler


def test_run_query_returns_feedback_on_driver_error(make_dialect):
    # Un errore del driver durante l'esecuzione non si propaga: diventa feedback per l'agente.
    tools, _ = _tools(make_dialect, _raising_handler('near "SELCT": syntax error'))
    out = tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    assert "was not executed" in out.lower()
    assert "RuntimeError" in out
    assert 'near "SELCT": syntax error' in out
    assert "SELECT * FROM ordini" in out  # query inviata riportata nel feedback


def test_count_rows_returns_feedback_on_bad_where(make_dialect):
    tools, _ = _tools(make_dialect, _raising_handler("unknown column 'nope'", on="SELECT COUNT(*)"))
    out = tools["count_rows"].invoke({"table": "ordini", "where": "nope = 1"})
    assert "unknown column 'nope'" in out
    assert "WHERE nope = 1" in out


def _materialize_tools(make_dialect, handler, backend, guardrails=None):
    dialect, cursor = make_dialect(MySQLDialect, handler)
    tools = dialect.build_tools(
        CONN, guardrails or GuardrailConfig(), materialize_enable=True, backend=backend
    )
    return {t.name: t for t in tools}, cursor


def test_materialize_query_without_backend_reports_missing_backend(make_dialect):
    # backend=None: il tool non può scrivere su file e lo comunica all'agente.
    tools, _ = _materialize_tools(make_dialect, _handler(), backend=None)
    out = tools["materialize_query"].func(
        runtime=SimpleNamespace(tool_call_id="call-1"), sql="SELECT * FROM ordini"
    )
    assert "cannot write to file" in out.lower()
    assert "no filesystem backend" in out.lower()


def test_materialize_query_with_backend_writes_and_returns_command(make_dialect):
    from deepagents.backends.protocol import BackendProtocol, WriteResult
    from langgraph.types import Command

    class RecordingBackend(BackendProtocol):
        def __init__(self):
            self.written: dict[str, str] = {}

        def write(self, path, content):
            self.written[path] = content
            return WriteResult(path=path)

    backend = RecordingBackend()
    tools, _ = _materialize_tools(make_dialect, _handler(), backend=backend)
    # ToolRuntime is injected by ToolNode; supply a stub with the tool_call_id the tool uses.
    out = tools["materialize_query"].func(
        runtime=SimpleNamespace(tool_call_id="call-1"),
        sql="SELECT * FROM ordini",
        filename="out.csv",
    )
    assert isinstance(out, Command)
    message = out.update["messages"][0].content
    assert "out.csv" in message
    assert "2 rows saved" in message
    # I dati completi sono su file, non nel contesto.
    assert backend.written["out.csv"].splitlines()[0] == "id,name"
