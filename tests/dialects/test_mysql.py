from __future__ import annotations

import pytest

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.mysql import MySQLDialect
from deep_db_agents.exceptions import GuardrailError, QueryNotAllowedError
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
    tools, _ = _tools(
        make_dialect,
        _handler(estimate=10_000_000),
        guardrails=GuardrailConfig(explain_row_threshold=1000),
    )
    with pytest.raises(GuardrailError):
        tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})


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
