from __future__ import annotations

import pytest

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.postgres import PostgresDialect
from deep_db_agents.exceptions import GuardrailError
from deep_db_agents.guardrails import GuardrailConfig

CONN = ConnectionConfig(scheme="postgres", host="localhost", port=5432, credential={"user": "u"})


def _handler(estimate=5):
    def handler(sql, params):
        s = sql.strip().upper()
        if s.startswith("SET"):
            return [], []
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
    tools, _ = _tools(
        make_dialect,
        _handler(estimate=5_000_000),
        guardrails=GuardrailConfig(explain_row_threshold=1000),
    )
    with pytest.raises(GuardrailError):
        tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})


def test_quote_ident_double_quotes(make_dialect):
    dialect, _ = make_dialect(PostgresDialect, _handler())
    assert dialect._quote_ident("ordini") == '"ordini"'
