"""Driver-specific helpers for Neo4j (official ``neo4j`` driver)."""

from __future__ import annotations

import re
from typing import Any

from ...connection import ConnectionConfig
from ...exceptions import QueryNotAllowedError
from ...tabular import scalar_json

# Cypher write clauses: forbidden in the read-only tools.
_WRITE_CLAUSES = {
    "CREATE",
    "MERGE",
    "DELETE",
    "SET",
    "REMOVE",
    "DROP",
    "FOREACH",
    "LOAD",  # LOAD CSV
}
_TOKEN_RE = re.compile(r"[A-Za-z_]+")


def connect(conn: ConnectionConfig, *, network_timeout_s: int | None = None):
    """Opens a Neo4j driver from the credentials. Lazy import of the driver.

    Args:
        conn: Connection configuration (host, port, credentials).
        network_timeout_s: Client-side timeout (derived from
            ``guardrails.query_timeout_s``) limiting the wait for a connection from the pool
            and the total retry time of managed transactions: without it, ``execute_read``
            could retry/wait indefinitely, blocking the whole tool node when tool calls run
            in parallel.

    Returns:
        A connected ``neo4j.Driver`` instance.

    Raises:
        ImportError: If the ``neo4j`` extra is not installed.
    """
    try:
        import neo4j
    except ImportError as exc:  # pragma: no cover - depends on the installed extra
        raise ImportError(
            "The Neo4j dialect requires the 'neo4j' extra (pip install 'deep-db-agents[neo4j]')."
        ) from exc

    cred = conn.credential
    host = conn.host or "localhost"
    port = conn.port or 7687
    uri = f"neo4j://{host}:{port}"
    auth = (cred["user"], cred.get("password", "")) if cred.get("user") else None
    config: dict[str, Any] = {"connection_timeout": int(cred.get("connect_timeout", 10))}
    if network_timeout_s and network_timeout_s > 0:
        config["connection_acquisition_timeout"] = network_timeout_s
        config["max_transaction_retry_time"] = network_timeout_s
    return neo4j.GraphDatabase.driver(uri, auth=auth, **config)


def quote_label(label: str) -> str:
    """Escapes a Cypher label/identifier (doubled backticks).

    Args:
        label: The raw label/identifier to escape.

    Returns:
        The label wrapped in backticks, with any internal backtick doubled.

    Prevents a label containing a backtick from escaping the quoting in
    ``(n:\\`label\\`)`` and injecting Cypher.
    """
    return "`" + label.replace("`", "``") + "`"


def ensure_read_only(cypher: str) -> str:
    """Validates that the Cypher is a single read-only statement.

    Args:
        cypher: The raw Cypher statement submitted by the agent.

    Returns:
        The stripped statement, if validation passes.

    Raises:
        QueryNotAllowedError: If the statement is empty, contains multiple statements, or
            uses a forbidden write clause.
    """
    stripped = cypher.strip().rstrip(";").strip()
    if not stripped:
        raise QueryNotAllowedError("Empty Cypher query.")
    if ";" in stripped:
        raise QueryNotAllowedError("Only single Cypher statements are allowed: remove the ';'.")
    tokens = {t.upper() for t in _TOKEN_RE.findall(stripped)}
    forbidden = _WRITE_CLAUSES & tokens
    if forbidden:
        raise QueryNotAllowedError(
            f"Write clause not allowed: {', '.join(sorted(forbidden))}. "
            "Only read queries are allowed (MATCH/RETURN/WITH/CALL ... YIELD)."
        )
    return stripped


def records_to_table(keys: list[str], records: list[dict]) -> tuple[list[str], list[list[Any]]]:
    """Converts Cypher records into (columns, rows) for file materialization.

    Args:
        keys: The result column names declared by the Cypher query.
        records: The record data, one dict per row.

    Returns:
        A tuple of (column names, row values).

    Unlike documents, the columns are the ``keys`` declared by the Cypher result (not the
    union of keys); nodes/relationships/nested values are serialized.
    """
    rows = [[scalar_json(rec.get(k)) for k in keys] for rec in records]
    return list(keys), rows
