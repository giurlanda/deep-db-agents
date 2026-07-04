from __future__ import annotations

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.mariadb import MariaDBDialect
from deep_db_agents.dialects.mariadb.prompt import MARIADB_SYSTEM_PROMPT
from deep_db_agents.guardrails import GuardrailConfig

CONN = ConnectionConfig(scheme="mariadb", host="localhost", port=3306, credential={"user": "u"})


def _handler(sql, params):
    s = sql.strip().upper()
    if s.startswith("SET"):
        return [], []
    if s.startswith("EXPLAIN"):
        return [("rows",)], [(3,)]
    return [("id",)], [(1,)]


def test_mariadb_uses_max_statement_time(make_dialect):
    dialect, cursor = make_dialect(MariaDBDialect, _handler)
    tools = {t.name: t for t in dialect.build_tools(CONN, GuardrailConfig())}
    tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    set_stmts = [sql for sql, _ in cursor.executed if sql.strip().upper().startswith("SET")]
    assert any("max_statement_time" in sql for sql in set_stmts)
    # Non deve usare la sintassi MySQL.
    assert all("MAX_EXECUTION_TIME" not in sql.upper() for sql in set_stmts)


def test_mariadb_prompt_and_reuse():
    dialect = MariaDBDialect()
    assert dialect.system_prompt() == MARIADB_SYSTEM_PROMPT
    assert dialect.schemes == ("mariadb",)
    # Riusa i tool SQL di MySQLDialect.
    names = {t.name for t in dialect.build_tools(CONN, GuardrailConfig(), materialize_enable=True)}
    assert {"run_query", "count_rows", "materialize_query"} <= names
