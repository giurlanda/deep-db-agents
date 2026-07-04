"""Test di integrazione del dialect SQLite (sqlite3 è nella stdlib, nessun mock)."""

from __future__ import annotations

import sqlite3

import pytest

from deep_db_agents.exceptions import GuardrailError, QueryNotAllowedError
from deep_db_agents.guardrails import GuardrailConfig
from deep_db_agents.url import parse_db_url


@pytest.fixture
def db_tools(tmp_path):
    """Crea un DB SQLite su file e restituisce (tools, path) costruiti dall'URL relativo."""
    from deep_db_agents.connection import ConnectionConfig
    from deep_db_agents.dialects.sqlite import SQLiteDialect

    db_file = tmp_path / "shop.db"
    con = sqlite3.connect(db_file)
    con.executescript(
        "CREATE TABLE ordini (id INTEGER PRIMARY KEY, regione TEXT NOT NULL, importo REAL);"
        "INSERT INTO ordini (regione, importo) VALUES ('Toscana', 10), ('Lazio', 20);"
    )
    con.commit()
    con.close()

    parsed = parse_db_url(f"sqlite:////{db_file}")  # path assoluto
    conn = ConnectionConfig(scheme="sqlite", host=None, port=None, credential={}, path=parsed.path)
    dialect = SQLiteDialect()
    built = dialect.build_tools(conn, GuardrailConfig(default_rows=10), materialize_enable=True)
    return {t.name: t for t in built}


def test_list_tables(db_tools):
    assert "ordini" in db_tools["list_tables"].invoke({})


def test_describe_table(db_tools):
    out = db_tools["describe_table"].invoke({"table": "ordini"})
    assert "regione" in out and "importo" in out


def test_count_and_query(db_tools):
    assert "2" in db_tools["count_rows"].invoke({"table": "ordini"})
    out = db_tools["run_query"].invoke({"sql": "SELECT * FROM ordini ORDER BY id"})
    assert "2 rows" in out and "Toscana" in out


def test_run_query_rejects_non_select(db_tools):
    out = db_tools["run_query"].invoke({"sql": "DELETE FROM ordini"})
    assert "was not executed" in out.lower()
    assert "DELETE FROM ordini" in out


def test_describe_rejects_bad_identifier(db_tools):
    with pytest.raises(QueryNotAllowedError):
        db_tools["count_rows"].invoke({"table": "bad name; DROP"})


def test_run_query_rejects_write_cte(db_tools):
    # Una CTE che contiene uno statement di scrittura non deve passare la whitelist SELECT.
    out = db_tools["run_query"].invoke(
        {"sql": "WITH x AS (DELETE FROM ordini RETURNING *) SELECT * FROM x"}
    )
    assert "was not executed" in out.lower()


def test_count_rows_with_where(db_tools):
    out = db_tools["count_rows"].invoke({"table": "ordini", "where": "importo > 15"})
    assert "1 rows" in out


def test_count_rows_rejects_write_in_where(db_tools):
    out = db_tools["count_rows"].invoke({"table": "ordini", "where": "1=1; DROP TABLE ordini"})
    assert "was not executed" in out.lower()


def test_run_query_blocked_by_real_estimate(db_tools):
    # SQLite ora fornisce una stima reale (COUNT sulla sotto-query): la soglia EXPLAIN si attiva.
    from deep_db_agents.connection import ConnectionConfig
    from deep_db_agents.dialects.sqlite import SQLiteDialect

    # Riusa lo stesso file del fixture ricostruendo i tool con soglia bassa.
    tool = db_tools["run_query"]
    conn_path = None
    for cell in tool.func.__closure__ or []:
        val = cell.cell_contents
        if isinstance(val, ConnectionConfig):
            conn_path = val.path
    assert conn_path is not None
    conn = ConnectionConfig(scheme="sqlite", host=None, port=None, credential={}, path=conn_path)
    built = SQLiteDialect().build_tools(conn, GuardrailConfig(explain_row_threshold=1))
    tools = {t.name: t for t in built}
    with pytest.raises(GuardrailError):
        tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})


def test_readonly_connection_blocks_writes(tmp_path):
    # tools.connect(read_only=True) apre il file in sola lettura: uno scrittura fallisce a
    # livello di driver, difesa in profondità oltre alla whitelist di keyword.
    from deep_db_agents.dialects.sqlite import tools as sqlite_tools

    db_file = tmp_path / "ro.db"
    con = sqlite3.connect(db_file)
    con.executescript("CREATE TABLE t (id INTEGER);")
    con.commit()
    con.close()

    ro = sqlite_tools.connect(str(db_file), read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t (id) VALUES (1)")
    ro.close()


def test_run_query_forces_limit(db_tools):
    # default_rows=10 deve essere applicato come LIMIT.
    out = db_tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
    assert "page_size 10" in out


def test_query_budget_enforced(db_tools, tmp_path):
    # Budget di sessione: oltre il tetto deve scattare il guardrail.
    from deep_db_agents.connection import ConnectionConfig
    from deep_db_agents.dialects.sqlite import SQLiteDialect

    db_file = next(tmp_path.glob("*.db"))
    conn = ConnectionConfig(scheme="sqlite", host=None, port=None, credential={}, path=str(db_file))
    built = SQLiteDialect().build_tools(
        conn, GuardrailConfig(row_budget=1), materialize_enable=True
    )
    tools = {t.name: t for t in built}
    with pytest.raises(GuardrailError):
        tools["run_query"].invoke({"sql": "SELECT * FROM ordini"})
