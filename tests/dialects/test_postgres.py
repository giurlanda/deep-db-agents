from __future__ import annotations

import pytest

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.postgres import PostgresDialect
from deep_db_agents.guardrails import GuardrailConfig

CONN = ConnectionConfig(scheme="postgres", host="localhost", port=5432, credential={"user": "u"})


def _handler(estimate=5):
    def handler(sql, params):
        s = sql.strip().upper()
        if "SET_CONFIG" in s:
            return [("set_config",)], [(str(params[0]),)]
        if s.startswith("EXPLAIN"):
            # psycopg restituisce il piano JSON in una singola cella.
            return [("QUERY PLAN",)], [([{"Plan": {"Plan Rows": estimate}}],)]
        return [("id",), ("name",)], [(1, "a")]

    return handler


def _tools(make_dialect, handler, guardrails=None):
    dialect, cursor = make_dialect(PostgresDialect, handler)
    tools = dialect.build_tools(CONN, guardrails or GuardrailConfig())
    return {t.name: t for t in tools}, cursor


def test_run_query_uses_json_explain_estimate(make_dialect):
    tools, cursor = _tools(make_dialect, _handler(estimate=3))
    out = tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    assert "1 rows" in out


def test_run_query_blocked_over_threshold(make_dialect):
    tools, cursor = _tools(
        make_dialect,
        _handler(estimate=5_000_000),
        guardrails=GuardrailConfig(explain_row_threshold=1000),
    )
    out = tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    # The estimate block is reflected back as corrective feedback, not raised.
    assert "was not executed" in out.lower()
    assert "aggregate" in out.lower()
    # The data query was never run: only set_config + EXPLAIN reached the driver.
    assert not any("_q" in sql for sql, _ in cursor.executed)


def test_quote_ident_double_quotes(make_dialect):
    dialect, _ = make_dialect(PostgresDialect, _handler())
    assert dialect._quote_ident("ordini") == '"ordini"'


_SET_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, false)"


def test_apply_timeout_uses_set_config(make_dialect):
    # Regression for #17: psycopg 3 binds parameters server-side (`$1`), which PostgreSQL
    # rejects in `SET statement_timeout = $1`; set_config accepts a bound parameter.
    tools, cursor = _tools(make_dialect, _handler())
    assert "1 rows" in tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    assert cursor.executed[0] == (_SET_TIMEOUT_SQL, ("30000",))
    assert not any(sql.strip().upper().startswith("SET ") for sql, _ in cursor.executed)


def test_timeout_zero_disables_statement_timeout(make_dialect):
    tools, cursor = _tools(make_dialect, _handler(), guardrails=GuardrailConfig(query_timeout_s=0))
    tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    assert cursor.executed[0] == (_SET_TIMEOUT_SQL, ("0",))


def test_timeout_setup_failure_tells_agent_not_to_retry(make_dialect):
    def handler(sql, params):
        if "set_config" in sql:
            raise RuntimeError('syntax error at or near "$1"')
        return _handler()(sql, params)

    tools, cursor = _tools(make_dialect, handler)
    out = tools["run_query"].invoke({"sql": "SELECT 1 AS ok"})
    assert "session could not be prepared" in out
    assert "NOT caused by your query" in out
    assert "Do not modify or retry" in out
    assert "Fix the query" not in out
    # Only the timeout setup reached the driver: the agent's query never ran.
    assert [sql for sql, _ in cursor.executed] == [_SET_TIMEOUT_SQL]


def _tools_failing_connect(make_dialect, monkeypatch, exc):
    dialect, _ = make_dialect(PostgresDialect, _handler())

    def failing_connect(conn, guardrails=None):  # noqa: ARG001
        raise exc

    monkeypatch.setattr(dialect, "_connect", failing_connect)
    return {t.name: t for t in dialect.build_tools(CONN, GuardrailConfig())}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_tables", {}),
        ("describe_table", {"table": "ordini"}),
        ("count_rows", {"table": "ordini"}),
        ("sample_rows", {"table": "ordini"}),
        ("run_query", {"sql": "SELECT 1"}),
    ],
)
def test_connection_failure_is_setup_feedback(make_dialect, monkeypatch, tool, args):
    exc = RuntimeError("connection refused: postgres://admin:s3cr3t@db:5432/shop")
    tools = _tools_failing_connect(make_dialect, monkeypatch, exc)
    out = tools[tool].invoke(args)
    assert "session could not be prepared" in out
    assert "Do not modify or retry" in out
    assert "s3cr3t" not in out


def test_missing_driver_still_raises(make_dialect, monkeypatch):
    exc = ImportError("The Postgres dialect requires the 'postgres' extra.")
    tools = _tools_failing_connect(make_dialect, monkeypatch, exc)
    with pytest.raises(ImportError):
        tools["run_query"].invoke({"sql": "SELECT 1"})
