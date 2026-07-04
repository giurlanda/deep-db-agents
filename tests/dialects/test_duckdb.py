"""Test di integrazione del dialect DuckDB (saltati se l'extra non è installato)."""

from __future__ import annotations

import pytest

from deep_db_agents.guardrails import GuardrailConfig

duckdb = pytest.importorskip("duckdb")


def _tools(path, guardrails=None):
    from deep_db_agents.connection import ConnectionConfig
    from deep_db_agents.dialects.duckdb import DuckDBDialect

    conn = ConnectionConfig(scheme="duckdb", host=None, port=None, credential={}, path=path)
    dialect = DuckDBDialect()
    built = dialect.build_tools(conn, guardrails or GuardrailConfig(), materialize_enable=True)
    return {t.name: t for t in built}


@pytest.fixture
def file_tools(tmp_path):
    db_file = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_file))
    con.execute("CREATE TABLE vendite (id INTEGER, regione VARCHAR, importo DOUBLE)")
    con.execute("INSERT INTO vendite VALUES (1,'Toscana',10.0),(2,'Lazio',20.0)")
    con.close()
    return _tools(str(db_file), GuardrailConfig(default_rows=10))


def test_file_list_and_describe(file_tools):
    assert "vendite" in file_tools["list_tables"].invoke({})
    out = file_tools["describe_table"].invoke({"table": "vendite"})
    assert "regione" in out and "importo" in out


def test_file_query_and_estimate(file_tools):
    sql = "SELECT regione, SUM(importo) AS tot FROM vendite GROUP BY regione ORDER BY regione"
    out = file_tools["run_query"].invoke({"sql": sql})
    assert "rows" in out and "Toscana" in out


def test_file_rejects_non_select(file_tools):
    out = file_tools["run_query"].invoke({"sql": "DROP TABLE vendite"})
    assert "was not executed" in out.lower()
    assert "DROP TABLE vendite" in out


def test_concurrent_connections_read_only(tmp_path):
    """read_only deve permettere due connessioni simultanee allo stesso file (tool paralleli)."""
    from concurrent.futures import ThreadPoolExecutor

    db_file = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db_file))
    con.execute("CREATE TABLE t(a INTEGER)")
    con.execute("INSERT INTO t VALUES (1),(2),(3)")
    con.close()
    tools = _tools(str(db_file))

    def call(_):
        return tools["count_rows"].invoke({"table": "t"})

    with ThreadPoolExecutor(max_workers=2) as ex:
        outs = list(ex.map(call, range(2)))
    assert all("3" in o for o in outs)


def test_datalake_folder_exposes_files_as_tables(tmp_path):
    # Crea due file parquet nella cartella: devono diventare tabelle interrogabili.
    lake = tmp_path / "lake"
    lake.mkdir()
    con = duckdb.connect(":memory:")
    con.execute(
        f"COPY (SELECT 1 AS id, 'a' AS nome) TO '{lake / 'clienti.parquet'}' (FORMAT parquet)"
    )
    con.execute(f"COPY (SELECT 10 AS qty) TO '{lake / 'ordini.parquet'}' (FORMAT parquet)")
    con.close()

    tools = _tools(f"{lake}/")  # trailing slash -> data lake
    listed = tools["list_tables"].invoke({})
    assert "clienti" in listed and "ordini" in listed
    out = tools["run_query"].invoke({"sql": "SELECT nome FROM clienti"})
    assert "a" in out


def test_datalake_views_created_once(tmp_path, monkeypatch):
    # La connessione data-lake è cached per istanza di dialect: glob + CREATE VIEW
    # avvengono una sola volta, non a ogni tool call.
    from deep_db_agents.dialects.duckdb import dialect as duckdb_dialect_mod

    lake = tmp_path / "lake"
    lake.mkdir()
    con = duckdb.connect(":memory:")
    con.execute(f"COPY (SELECT 1 AS id) TO '{lake / 'clienti.parquet'}' (FORMAT parquet)")
    con.close()

    calls = {"n": 0}
    original = duckdb_dialect_mod.tools.connect_datalake

    def counting_connect(folder):
        calls["n"] += 1
        return original(folder)

    monkeypatch.setattr(duckdb_dialect_mod.tools, "connect_datalake", counting_connect)
    tools = _tools(f"{lake}/")
    tools["list_tables"].invoke({})
    tools["count_rows"].invoke({"table": "clienti"})
    tools["list_tables"].invoke({})
    assert calls["n"] == 1
