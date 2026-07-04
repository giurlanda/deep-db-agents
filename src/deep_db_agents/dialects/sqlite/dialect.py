"""Full SQLite dialect (stdlib ``sqlite3`` module).

SQLite is a file-based SQL database: the URL ``sqlite://<path>`` indicates the file
(SQLAlchemy style: ``sqlite:///rel.db`` relative to the working dir, ``sqlite:////abs.db``
absolute, ``sqlite://:memory:`` in memory). It reuses all of ``SqlDialect``'s logic
(DB-API 2.0); the execution timeout is enforced by ``FileSqlDialect``'s watchdog because
SQLite has no native ``statement_timeout``. File connections are opened read-only
(``mode=ro``), a defense-in-depth measure on top of the statement whitelist.

Warning: with ``:memory:`` every tool call opens a brand-new, empty database (the connection
is per-call): useful only for smoke tests, not for data that must survive across calls.
"""

from __future__ import annotations

from typing import Any

from ...connection import ConnectionConfig
from ...guardrails import GuardrailConfig
from ...registry import register
from ..sql_base import FileSqlDialect
from . import tools
from .prompt import SQLITE_SYSTEM_PROMPT


@register("sqlite")
class SQLiteDialect(FileSqlDialect):
    """Agent specialized on SQLite."""

    schemes = ("sqlite",)

    def system_prompt(self) -> str:
        """Returns the SQLite-specific system prompt.

        Returns:
            The system prompt text for the SQLite agent.
        """
        return SQLITE_SYSTEM_PROMPT

    def _connect(self, conn: ConnectionConfig, guardrails: GuardrailConfig):
        """Opens a read-only SQLite connection.

        Args:
            conn: Connection configuration; ``conn.path`` is the database file path (or
                ``:memory:``) and ``conn.credential`` may hold a ``connect_timeout`` override.
            guardrails: Guardrail configuration (unused here; the execution timeout is
                enforced separately by the watchdog).

        Returns:
            A ``sqlite3.Connection`` opened in read-only mode (except for ``:memory:``).
        """
        path = self.resolve_path(conn.path)
        busy_timeout_s = int(conn.credential.get("connect_timeout", 10))
        return tools.connect(path, busy_timeout_s=busy_timeout_s, read_only=True)

    def _estimate_rows(self, cursor, sql: str) -> int:
        """Estimates the number of rows a query would return.

        SQLite does not expose a row estimate via ``EXPLAIN QUERY PLAN``; to still trigger
        the ``check_estimate`` threshold, an exact ``COUNT`` is run on the subquery (the same
        approach MongoDB uses before a ``find``). The timeout watchdog still bounds the
        duration. Fails open (returns 0) on error.

        Args:
            cursor: An open DB-API cursor.
            sql: The SELECT statement to estimate.

        Returns:
            The estimated (here: exact) row count, or 0 if the estimate could not be computed.
        """
        try:
            cursor.execute(f"SELECT COUNT(*) FROM ({sql}) AS _est")
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001 - best-effort estimate, never blocking on its own
            return 0

    def _list_tables_sql(self, conn: ConnectionConfig) -> str:
        """Builds the SQL statement listing the tables of the current database.

        Args:
            conn: Connection configuration (unused; SQLite has a single implicit schema).

        Returns:
            A SQL SELECT statement returning one table name per row.
        """
        return (
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
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
            "SELECT name, type, "
            "CASE WHEN \"notnull\" = 0 THEN 'YES' ELSE 'NO' END AS is_nullable "
            "FROM pragma_table_info(?) ORDER BY cid",
            (table,),
        )
