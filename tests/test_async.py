"""I tool del dialect, pur sincroni, sono invocabili con ``ainvoke``: LangChain li esegue in
un thread executor. Questi test verificano ainvoke ed esecuzione concorrente senza driver
asincroni nativi (non serve pytest-asyncio: usiamo ``asyncio.run``)."""

from __future__ import annotations

import asyncio
import sqlite3

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.dialects.sqlite import SQLiteDialect
from deep_db_agents.guardrails import GuardrailConfig


def _sqlite_tools(tmp_path):
    db_file = tmp_path / "s.db"
    con = sqlite3.connect(db_file)
    con.executescript("CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1),(2),(3);")
    con.commit()
    con.close()
    conn = ConnectionConfig(scheme="sqlite", host=None, port=None, credential={}, path=str(db_file))
    built = SQLiteDialect().build_tools(conn, GuardrailConfig())
    return {t.name: t for t in built}


def test_tool_supports_ainvoke(tmp_path):
    tools = _sqlite_tools(tmp_path)

    async def run():
        return await tools["run_query"].ainvoke({"sql": "SELECT * FROM t"})

    out = asyncio.run(run())
    assert "3 rows" in out


def test_tools_run_concurrently_under_ainvoke(tmp_path):
    # Due ainvoke concorrenti sullo stesso dialect: la lettura read-only SQLite permette
    # connessioni simultanee, i tool sincroni sono offloadati su thread separati.
    tools = _sqlite_tools(tmp_path)

    async def run():
        return await asyncio.gather(
            tools["run_query"].ainvoke({"sql": "SELECT * FROM t"}),
            tools["count_rows"].ainvoke({"table": "t"}),
        )

    query_out, count_out = asyncio.run(run())
    assert "3 rows" in query_out
    assert "3 rows" in count_out
