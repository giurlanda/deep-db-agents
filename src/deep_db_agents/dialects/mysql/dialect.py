"""Full MySQL dialect (pymysql driver)."""

from __future__ import annotations

from typing import Any

from ...connection import ConnectionConfig
from ...guardrails import GuardrailConfig
from ...registry import register
from ..sql_base import NETWORK_TIMEOUT_GRACE_S, SqlDialect
from . import tools
from .prompt import MYSQL_SYSTEM_PROMPT


@register("mysql")
class MySQLDialect(SqlDialect):
    """Agent specialized on MySQL."""

    schemes = ("mysql",)
    quote_char = "`"

    def system_prompt(self) -> str:
        """Return the MySQL-specific system prompt.

        Returns:
            The full system prompt text for the MySQL agent.
        """
        return MYSQL_SYSTEM_PROMPT

    def _connect(self, conn: ConnectionConfig, guardrails: GuardrailConfig):
        """Open a pymysql connection with a network timeout derived from the guardrails.

        Client-side network timeout = query timeout + grace period: the server-side
        timeout (MAX_EXECUTION_TIME) fires first with a clear error; this is the safety
        net against a socket stall that would otherwise hang indefinitely.

        Args:
            conn: Connection parameters (host, port, credentials, database).
            guardrails: Guardrail configuration providing the query timeout.

        Returns:
            An open pymysql connection.
        """
        return tools.connect(
            conn, network_timeout_s=guardrails.query_timeout_s + NETWORK_TIMEOUT_GRACE_S
        )

    def _apply_timeout(self, cursor, timeout_s: int) -> None:
        """Set the server-side statement timeout on the given cursor.

        MAX_EXECUTION_TIME is expressed in milliseconds and applies to SELECT statements.

        Args:
            cursor: DB-API cursor on which to set the timeout.
            timeout_s: Timeout in seconds.
        """
        cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (timeout_s * 1000,))

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
            conn: Connection parameters (unused for MySQL, kept for interface parity).

        Returns:
            The SQL statement to list tables.
        """
        return "SHOW TABLES"

    def _describe_table_sql(self, table: str) -> tuple[str, tuple[Any, ...]]:
        """Build the SQL statement that describes a table's columns.

        Args:
            table: Name of the table to describe.

        Returns:
            A tuple of (SQL statement, query parameters).
        """
        return (
            "SELECT column_name, column_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        )
