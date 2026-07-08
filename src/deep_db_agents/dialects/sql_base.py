"""Shared base for SQL dialects (MySQL, Postgres).

Implements the SQL tools exactly once — schema exploration, counting, sampling, query
execution with guardrails and materialization — delegating the driver-specific part
(connection, row estimation via EXPLAIN, timeout, quoting) to abstract methods. The SQL
drivers used (pymysql, psycopg) are both DB-API 2.0, so the execution logic is shared.
"""

from __future__ import annotations

import os
import re
import threading
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain.tools import BaseTool, ToolRuntime, tool
from langgraph.types import Command

from ..base import DbDialect
from ..connection import ConnectionConfig
from ..exceptions import DeepDbAgentError, EstimateExceededError, QueryNotAllowedError
from ..guardrails import GuardrailConfig, SessionBudget
from ..query_errors import format_estimate_block, format_query_error
from ..workspace import materialize_result, write_command

# Simple SQL identifier (table/column). Intentionally restrictive.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Write clauses forbidden even inside a CTE (``WITH ... AS (DELETE ...) SELECT``):
# without this check a ``WITH`` would be treated as a SELECT and would pass the whitelist.
_WRITE_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "CREATE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "REPLACE",
        "CALL",
        "EXEC",
        "EXECUTE",
    }
)
_WORD_RE = re.compile(r"[A-Za-z_]+")

# Margin, in seconds, between the server-side query timeout and the client-side network
# timeout: lets the DB timeout fire first (clearer error), keeping the socket timeout as a
# last safety net against network stalls.
NETWORK_TIMEOUT_GRACE_S = 5


def _ensure_single_select(sql: str, allowed: frozenset[str]) -> str:
    """Validates that ``sql`` is a single allowed statement (usually a SELECT).

    Args:
        sql: The raw SQL text submitted by the agent.
        allowed: Set of allowed statement keywords (e.g. ``{"SELECT"}``).

    Returns:
        The stripped SQL statement, with the trailing semicolon (if any) removed.

    Raises:
        QueryNotAllowedError: If ``sql`` is empty, contains multiple statements, contains a
            write clause inside a CTE, or its leading keyword is not in ``allowed``.
    """
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise QueryNotAllowedError("Only single statements are allowed: remove internal ';'.")
    if not stripped:
        raise QueryNotAllowedError("Empty query.")
    head = stripped.split(None, 1)[0].upper()
    # WITH ... SELECT is allowed if SELECT is in the whitelist, but a CTE can contain a
    # write statement (``WITH x AS (DELETE ... RETURNING *) SELECT * FROM x``): in that
    # case scan the tokens and reject if a write clause appears.
    if head == "WITH":
        write_tokens = {t.upper() for t in _WORD_RE.findall(stripped)} & _WRITE_KEYWORDS
        if write_tokens:
            joined = ", ".join(sorted(write_tokens))
            raise QueryNotAllowedError(f"Write clause not allowed inside the CTE: {joined}.")
        effective = "SELECT"
    else:
        effective = head
    if effective not in allowed:
        raise QueryNotAllowedError(
            f"Operation '{head}' not allowed. Allowed: {', '.join(sorted(allowed))}."
        )
    return stripped


def _validate_where(where: str) -> str:
    """Validates an interpolated ``WHERE`` fragment: rejects multi-statement and write clauses.

    The fragment cannot be parametrized (it is arbitrary SQL produced by the agent), so the
    minimum check is preventing it from escaping the SELECT context (``;`` or write keywords).

    Args:
        where: The raw WHERE fragment submitted by the agent.

    Returns:
        The stripped WHERE fragment.

    Raises:
        QueryNotAllowedError: If the fragment contains ``;`` or a write keyword.
    """
    fragment = where.strip()
    if ";" in fragment:
        raise QueryNotAllowedError("The WHERE filter cannot contain ';'.")
    write_tokens = {t.upper() for t in _WORD_RE.findall(fragment)} & _WRITE_KEYWORDS
    if write_tokens:
        joined = ", ".join(sorted(write_tokens))
        raise QueryNotAllowedError(f"Write clause not allowed in the WHERE filter: {joined}.")
    return fragment


class SqlDialect(DbDialect):
    """Abstract dialect for relational databases with a DB-API 2.0 interface."""

    #: Identifier quoting character: ``"`` (standard SQL) or ``` ` ``` (MySQL).
    quote_char: str = '"'

    # --- driver-specific hooks -------------------------------------------------

    @abstractmethod
    def _connect(self, conn: ConnectionConfig, guardrails: GuardrailConfig):
        """Opens and returns a DB-API connection. Lazy driver import belongs in here.

        Args:
            conn: Connection parameters (host, port, credentials).
            guardrails: Guardrail configuration; lets a client-side network (socket) timeout
                be derived from ``query_timeout_s``. Without it, a socket read that blocks
                would hang forever even with a server-side timeout, freezing the whole tool
                node when tool calls run in parallel.

        Returns:
            An open DB-API 2.0 connection.
        """

    @abstractmethod
    def _apply_timeout(self, cursor, timeout_s: int) -> None:
        """Sets the execution timeout on the current connection/cursor.

        Args:
            cursor: The DB-API cursor to configure.
            timeout_s: Timeout in seconds.
        """

    @abstractmethod
    def _estimate_rows(self, cursor, sql: str) -> int:
        """Estimates the rows returned by ``sql`` via EXPLAIN (DB-specific idiom).

        Args:
            cursor: The DB-API cursor to run the estimation on.
            sql: The SQL statement to estimate.

        Returns:
            The estimated row count.
        """

    @abstractmethod
    def _list_tables_sql(self, conn: ConnectionConfig) -> str:
        """Builds the SQL that lists the tables of the current schema.

        Args:
            conn: Connection parameters, in case the schema/database name is needed.

        Returns:
            The SQL statement text.
        """

    @abstractmethod
    def _describe_table_sql(self, table: str) -> tuple[str, tuple[Any, ...]]:
        """Builds the parametric SQL (and parameters) to describe a table's columns.

        Args:
            table: The table name to describe.

        Returns:
            A tuple of (SQL text, query parameters).
        """

    def _quote_ident(self, ident: str) -> str:
        """Validates and quotes an identifier using the dialect's ``quote_char``.

        Args:
            ident: The raw identifier (table/column name) to validate and quote.

        Returns:
            The quoted identifier.

        Raises:
            QueryNotAllowedError: If ``ident`` does not match the allowed identifier pattern.
        """
        if not _IDENT_RE.match(ident):
            raise QueryNotAllowedError(f"Invalid identifier: {ident!r}.")
        return f"{self.quote_char}{ident}{self.quote_char}"

    # --- execution helpers --------------------------------------------------

    def _watchdog(self, connection: Any, cursor: Any, timeout_s: int):
        """Optional hook: context manager that interrupts the query past ``timeout_s``.

        Default: no-op — server DBs enforce the timeout server-side (see ``_apply_timeout``).
        File-based DBs override this with a watchdog on ``interrupt()``.

        Args:
            connection: The open DB-API connection.
            cursor: The active DB-API cursor.
            timeout_s: Timeout in seconds.

        Returns:
            A context manager (no-op by default).
        """
        return nullcontext()

    @contextmanager
    def _cursor(self, conn: ConnectionConfig, guardrails: GuardrailConfig):
        """Opens a connection, applies the timeout/watchdog and yields a ready-to-use cursor.

        Args:
            conn: Connection parameters (host, port, credentials).
            guardrails: Guardrail configuration, used for the query timeout.

        Yields:
            A DB-API cursor with the timeout and watchdog already applied. The connection is
            closed automatically when the context exits.
        """
        connection = self._connect(conn, guardrails)
        try:
            cur = connection.cursor()
            self._apply_timeout(cur, guardrails.query_timeout_s)
            with self._watchdog(connection, cur, guardrails.query_timeout_s):
                yield cur
                cur.close()
        finally:
            connection.close()

    @staticmethod
    def _columns(cursor) -> list[str]:
        """Extracts the column names from a DB-API cursor's ``description``.

        Args:
            cursor: The DB-API cursor to read the description from, after ``execute``.

        Returns:
            The list of column names, or an empty list if the cursor has no description.
        """
        return [d[0] for d in (cursor.description or [])]

    # --- tool construction --------------------------------------------------

    def build_tools(
        self,
        conn: ConnectionConfig,
        guardrails: GuardrailConfig,
        materialize_enable: bool = False,
        backend: BackendProtocol | None = None,
    ) -> Sequence[BaseTool]:
        """Builds the LangChain tools exposed to the agent for this SQL dialect.

        Args:
            conn: Connection parameters injected into the tool closures.
            guardrails: Guardrail configuration (limits, timeout, allowed statements, budget)
                enforced by every tool.
            materialize_enable: Whether to also expose the ``materialize_query`` tool.
            backend: Filesystem backend injected into ``materialize_query``'s closure; when
                ``None`` the tool reports that no backend is configured.

        Returns:
            The sequence of tools: ``list_tables``, ``describe_table``, ``count_rows``,
            ``sample_rows``, ``run_query`` and, if enabled, ``materialize_query``.
        """
        metrics = getattr(self, "_metrics", None)
        budget = SessionBudget(guardrails.row_budget, metrics=metrics)

        @tool
        def list_tables() -> str:
            """Lists the tables available in the current database."""
            sql = self._list_tables_sql(conn)
            try:
                with self._cursor(conn, guardrails) as cur:
                    cur.execute(sql)
                    tables = [row[0] for row in cur.fetchall()]
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, what="table listing")
            return "Tables: " + (", ".join(tables) if tables else "(none)")

        @tool
        def describe_table(table: str) -> str:
            """Describes the columns (name and type) of a table."""
            try:
                self._quote_ident(table)
                sql, params = self._describe_table_sql(table)
                with self._cursor(conn, guardrails) as cur:
                    cur.execute(sql, params)
                    cols = self._columns(cur)
                    rows = cur.fetchall()
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=table, what="table description")
            lines = [", ".join(f"{c}={v}" for c, v in zip(cols, r, strict=False)) for r in rows]
            return f"Columns of {table}:\n" + ("\n".join(lines) or "(none)")

        @tool
        def count_rows(table: str, where: str | None = None) -> str:
            """Counts the rows of a table with an optional WHERE filter.

            Use this tool BEFORE extracting data, to decide whether the query is manageable
            or needs to be refined/aggregated (explore before extracting).
            """
            qtable = self._quote_ident(table)
            sql = f"SELECT COUNT(*) FROM {qtable}"
            try:
                if where:
                    sql += f" WHERE {_validate_where(where)}"
                with self._cursor(conn, guardrails) as cur:
                    cur.execute(sql)
                    total = cur.fetchone()[0]
            except QueryNotAllowedError as exc:  # noqa: BLE001 - filter not allowed -> feedback
                return format_query_error(exc, query=where, what="WHERE filter")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=sql, what="count")
            return f"{table}: {total:,} rows" + (f" with WHERE {where}" if where else "")

        @tool
        def sample_rows(table: str, limit: int = 5) -> str:
            """Returns a small sample of rows from a table (default 5)."""
            qtable = self._quote_ident(table)
            n = guardrails.clamp_limit(min(limit, 20))
            try:
                with self._cursor(conn, guardrails) as cur:
                    cur.execute(f"SELECT * FROM {qtable} LIMIT {n}")
                    cols = self._columns(cur)
                    rows = cur.fetchall()
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=table, what="sample")
            budget.charge(len(rows))
            data = [dict(zip(cols, r, strict=False)) for r in rows]
            return f"Sample of {table} ({len(data)} rows): {data}"

        @tool
        def run_query(sql: str, page: int = 0, page_size: int | None = None) -> str:
            """Runs a read-only query (SELECT) with forced LIMIT and pagination.

            Only SELECT/WITH are allowed. A maximum LIMIT that cannot be bypassed is applied,
            and the query is blocked if EXPLAIN estimates too many rows. For large datasets to
            analyze, use `materialize_query` instead. Prefer already-aggregated queries.
            """

            try:
                stmt = _ensure_single_select(sql, guardrails.allowed_statements)
                limit = guardrails.clamp_limit(page_size)
                offset = max(page, 0) * limit
                paginated = f"SELECT * FROM ({stmt}) AS _q LIMIT {limit} OFFSET {offset}"
                with self._cursor(conn, guardrails) as cur:
                    guardrails.check_estimate(self._estimate_rows(cur, stmt), metrics)
                    cur.execute(paginated)
                    cols = self._columns(cur)
                    rows = cur.fetchall()
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error -> feedback to the agent
                return format_query_error(exc, query=sql, what="query")
            except EstimateExceededError as exc:  # noqa: BLE001 - estimate too high -> feedback to the agent
                return format_estimate_block(exc, what="query")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=sql)
            budget.charge(len(rows))
            data = [dict(zip(cols, r, strict=False)) for r in rows]
            more = f" More rows available: ask for page={page + 1}." if len(rows) == limit else ""
            return f"{len(rows)} rows (page {page}, page_size {limit}).{more}\n{data}"

        @tool
        def materialize_query(
            runtime: ToolRuntime, sql: str, fmt: str = "csv", filename: str | None = None
        ) -> Command | str:
            """Runs a SELECT and saves the entire result to file (Parquet/CSV).

            Returns ONLY metadata: file path, columns, preview and statistics.
            Use this tool when the data is needed for analysis or charts but is too much to
            read in chat. The maximum LIMIT still applies as a safety ceiling.
            """
            if backend is None:
                return "Cannot write to file: no filesystem backend is configured."
            try:
                stmt = _ensure_single_select(sql, guardrails.allowed_statements)
                capped = f"SELECT * FROM ({stmt}) AS _q LIMIT {guardrails.hard_max_rows}"
                with self._cursor(conn, guardrails) as cur:
                    guardrails.check_estimate(self._estimate_rows(cur, stmt), metrics)
                    cur.execute(capped)
                    cols = self._columns(cur)
                    rows = cur.fetchall()
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error -> feedback to the agent
                return format_query_error(exc, query=sql, what="query")
            except EstimateExceededError as exc:  # noqa: BLE001 - estimate too high -> feedback to the agent
                return format_estimate_block(exc, what="query")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=sql)
            budget.charge(len(rows))
            result = materialize_result(cols, rows, fmt=fmt, filename=filename, backend=backend)
            return write_command(result.to_summary(), runtime.tool_call_id, result.files_update)

        tool_list = [list_tables, describe_table, count_rows, sample_rows, run_query]
        if materialize_enable:
            tool_list.append(materialize_query)
        return tool_list


class FileSqlDialect(SqlDialect):
    """Base for file-based SQL dialects (SQLite, DuckDB).

    These engines have no server-side ``statement_timeout``: the execution timeout is
    enforced by a watchdog that calls ``connection.interrupt()`` from a separate thread past
    ``query_timeout_s``, so a long or hung query gets interrupted instead of freezing the
    whole tool node when tool calls run in parallel.
    """

    def _apply_timeout(self, cursor, timeout_s: int) -> None:
        """No-op: file-based engines have no native statement timeout.

        Args:
            cursor: The DB-API cursor (unused).
            timeout_s: Timeout in seconds (unused here; handled by ``_watchdog`` instead).

        Returns:
            None. The actual timeout enforcement is handled by ``_watchdog`` via
            ``interrupt()``.
        """
        return None

    def _interrupt_target(self, connection: Any, cursor: Any) -> Any:
        """Returns the object on which to call ``interrupt()`` to stop the running query.

        SQLite executes on the connection (the cursor shares it); DuckDB executes on a
        cursor that is an independent connection, so it overrides this to point at the
        cursor instead.

        Args:
            connection: The open DB-API connection.
            cursor: The active DB-API cursor.

        Returns:
            The object exposing ``interrupt()``.
        """
        return connection

    @contextmanager
    def _watchdog(self, connection: Any, cursor: Any, timeout_s: int) -> Iterator[None]:
        """Context manager that interrupts the query if it runs past ``timeout_s``.

        Args:
            connection: The open DB-API connection.
            cursor: The active DB-API cursor.
            timeout_s: Timeout in seconds; if falsy or non-positive, no watchdog is armed.

        Yields:
            None. On exit, the watchdog timer is cancelled.
        """
        if not timeout_s or timeout_s <= 0:
            yield
            return
        target = self._interrupt_target(connection, cursor)
        timer = threading.Timer(timeout_s, target.interrupt)
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    @staticmethod
    def resolve_path(path: str | None) -> str:
        """Resolves the DB path relative to the working dir (``:memory:`` passes through).

        Args:
            path: The raw path from the connection URL, or ``None``.

        Returns:
            The absolute, expanded path, or ``":memory:"`` unchanged.
        """
        if not path or path == ":memory:":
            return ":memory:"
        return os.path.abspath(os.path.expanduser(path))

    @staticmethod
    def is_directory(conn: ConnectionConfig) -> bool:
        """Checks whether the URL points to a folder (trailing slash or existing directory).

        Args:
            conn: Connection parameters whose ``path`` is inspected.

        Returns:
            True if the URL path is a directory (or has a trailing slash), False otherwise.
        """
        raw = conn.path or ""
        if raw == ":memory:":
            return False
        if raw.endswith("/"):
            return True
        return os.path.isdir(FileSqlDialect.resolve_path(raw))
