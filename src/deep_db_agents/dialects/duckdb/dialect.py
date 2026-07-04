"""Full DuckDB dialect (``duckdb`` package).

DuckDB is a file-based analytical SQL database: the URL ``duckdb://<path>`` indicates the
file (SQLAlchemy style: ``duckdb:///rel.duckdb`` relative to the working dir,
``duckdb:////abs.duckdb`` absolute) or a **folder** (trailing slash, e.g. ``duckdb:///lake/``)
interpreted as a data lake: the parquet/csv/json files inside it are exposed as tables. It
reuses ``SqlDialect``'s logic; the execution timeout is enforced by ``FileSqlDialect``'s
watchdog.

In data lake mode the views are created only once per dialect instance (a snapshot of the
folder taken on the first call). Warning: with ``duckdb://:memory:`` every tool call opens a
brand-new, empty database (the connection is per-call).
"""

from __future__ import annotations

import threading
from typing import Any

from ...connection import ConnectionConfig
from ...guardrails import GuardrailConfig
from ...registry import register
from ..sql_base import FileSqlDialect
from . import tools
from .prompt import DUCKDB_SYSTEM_PROMPT


@register("duckdb")
class DuckDBDialect(FileSqlDialect):
    """Agent specialized on DuckDB."""

    schemes = ("duckdb",)

    def __init__(self) -> None:
        """Initializes the dialect with an empty data-lake connection cache."""
        # Data-lake connection cache (per dialect instance, i.e. per agent): the folder glob
        # and the view creation happen only once.
        self._datalake_con: Any = None
        self._datalake_folder: str | None = None
        self._datalake_lock = threading.Lock()

    def system_prompt(self) -> str:
        """Returns the DuckDB-specific system prompt.

        Returns:
            The system prompt text for the DuckDB agent.
        """
        return DUCKDB_SYSTEM_PROMPT

    def _connect(self, conn: ConnectionConfig, guardrails: GuardrailConfig):
        """Opens a DuckDB connection, either to a file or to a cached data-lake view set.

        Args:
            conn: Connection configuration; ``conn.path`` is either a database file path or a
                data-lake folder (trailing slash / existing directory).
            guardrails: Guardrail configuration (unused here; the execution timeout is
                enforced separately by the watchdog).

        Returns:
            A DuckDB connection (or cursor, in data-lake mode) ready for query execution.
        """
        if self.is_directory(conn):
            folder = self.resolve_path(conn.path)
            with self._datalake_lock:
                if self._datalake_con is None or self._datalake_folder != folder:
                    self._datalake_con = tools.connect_datalake(folder)
                    self._datalake_folder = folder
                # cursor() is an independent connection on the same in-memory DB: closing it
                # at the end of the tool call does not close the parent connection that holds
                # the views.
                # Note: the schema is a snapshot of the folder taken on the first call; files
                # added/removed during the session are not reflected.
                return self._datalake_con.cursor()
        path = self.resolve_path(conn.path)
        # read_only allows concurrent connections (parallel tool calls); :memory: does not.
        return tools.connect_file(path, read_only=path != ":memory:")

    def _interrupt_target(self, connection: Any, cursor: Any) -> Any:
        """Returns the object on which the timeout watchdog should call ``interrupt()``.

        In DuckDB the cursor is an independent connection: the query runs there, not on the
        parent connection.

        Args:
            connection: The parent DuckDB connection.
            cursor: The DuckDB cursor (an independent connection) executing the query.

        Returns:
            The cursor, since that is where the running query lives.
        """
        return cursor

    def _estimate_rows(self, cursor, sql: str) -> int:
        """Estimates the number of rows a query would return.

        Args:
            cursor: An open DB-API cursor.
            sql: The SELECT statement to estimate.

        Returns:
            The estimated row count from DuckDB's ``EXPLAIN`` plan, or 0 if it could not be
            determined.
        """
        return tools.estimate_rows(cursor, sql)

    def _list_tables_sql(self, conn: ConnectionConfig) -> str:
        """Builds the SQL statement listing the tables of the current database.

        Args:
            conn: Connection configuration (unused; the query relies on
                ``information_schema``, which is schema-agnostic).

        Returns:
            A SQL SELECT statement returning one table name per row.
        """
        return (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
            "ORDER BY table_name"
        )

    def _describe_table_sql(self, table: str) -> tuple[str, tuple[Any, ...]]:
        """Builds the parameterized SQL statement describing a table's columns.

        Args:
            table: Name of the table to describe.

        Returns:
            A tuple ``(sql, params)`` where ``sql`` is a parameterized SELECT and ``params``
            are the bind parameters (the table name).
        """
        return (
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = ? "
            "ORDER BY ordinal_position",
            (table,),
        )
