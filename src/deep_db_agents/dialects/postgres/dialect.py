"""Full PostgreSQL dialect (psycopg 3 driver)."""

from __future__ import annotations

from typing import Any

from ...connection import ConnectionConfig
from ...guardrails import GuardrailConfig
from ...registry import register
from ..sql_base import SqlDialect
from . import tools
from .prompt import POSTGRES_SYSTEM_PROMPT


@register("postgres", "postgresql")
class PostgresDialect(SqlDialect):
    """Agent specialized on PostgreSQL."""

    schemes = ("postgres", "postgresql")

    def system_prompt(self) -> str:
        """Return the PostgreSQL-specific system prompt.

        Returns:
            The full system prompt text for the PostgreSQL agent.
        """
        return POSTGRES_SYSTEM_PROMPT

    def _connect(self, conn: ConnectionConfig, guardrails: GuardrailConfig):
        """Open a psycopg connection with TCP keepalives derived from the guardrails.

        ``statement_timeout`` (server-side) limits query execution; TCP keepalives are the
        client-side defense against a pure network stall (dead peer but socket still open),
        so the connection does not hang forever.

        Args:
            conn: Connection parameters (host, port, credentials, database).
            guardrails: Guardrail configuration providing the query timeout.

        Returns:
            An open psycopg connection.
        """
        return tools.connect(conn, network_timeout_s=guardrails.query_timeout_s)

    def _apply_timeout(self, cursor, timeout_s: int) -> None:
        """Set the server-side statement timeout on the given cursor.

        Args:
            cursor: DB-API cursor on which to set the timeout.
            timeout_s: Timeout in seconds.
        """
        cursor.execute("SET statement_timeout = %s", (timeout_s * 1000,))

    def _estimate_rows(self, cursor, sql: str) -> int:
        """Estimate the number of rows a query would return.

        Args:
            cursor: DB-API cursor to run the estimate on.
            sql: SQL query to estimate.

        Returns:
            Estimated row count.
        """
        return tools.estimate_rows(cursor, sql)

    def _list_tables_sql(self, conn: ConnectionConfig) -> str:
        """Build the SQL statement that lists tables in the current database.

        Args:
            conn: Connection parameters (unused for Postgres, kept for interface parity).

        Returns:
            The SQL statement to list tables.
        """
        return (
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY tablename"
        )

    def _describe_table_sql(self, table: str) -> tuple[str, tuple[Any, ...]]:
        """Build the SQL statement that describes a table's columns.

        Args:
            table: Name of the table to describe.

        Returns:
            A tuple of (SQL statement, query parameters).
        """
        return (
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        )
