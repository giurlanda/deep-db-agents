"""Full MariaDB dialect.

MariaDB is compatible with the MySQL protocol, so it reuses the ``pymysql`` driver and most
of ``MySQLDialect``'s logic. It differs in the query timeout mechanism: MariaDB uses the
``max_statement_time`` session variable (in seconds), while MySQL uses ``MAX_EXECUTION_TIME``
(in milliseconds).
"""

from __future__ import annotations

from ...registry import register
from ..mysql.dialect import MySQLDialect
from .prompt import MARIADB_SYSTEM_PROMPT


@register("mariadb")
class MariaDBDialect(MySQLDialect):
    """Agent specialized on MariaDB."""

    schemes = ("mariadb",)

    def system_prompt(self) -> str:
        """Return the MariaDB-specific system prompt.

        Returns:
            The full system prompt text for the MariaDB agent.
        """
        return MARIADB_SYSTEM_PROMPT

    def _apply_timeout(self, cursor, timeout_s: int) -> None:
        """Set the server-side statement timeout on the given cursor.

        MariaDB: ``max_statement_time`` is expressed in seconds (fractional values allowed).

        Args:
            cursor: DB-API cursor on which to set the timeout.
            timeout_s: Timeout in seconds.
        """
        cursor.execute("SET SESSION max_statement_time = %s", (timeout_s,))
