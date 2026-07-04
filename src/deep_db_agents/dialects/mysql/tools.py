"""MySQL-specific driver helpers (pymysql)."""

from __future__ import annotations

from ...connection import ConnectionConfig


def connect(conn: ConnectionConfig, *, network_timeout_s: int | None = None):
    """Open a pymysql connection from credentials, with lazy driver import.

    ``network_timeout_s`` enforces a timeout on socket read/write: without it, a query
    whose socket stalls would hang forever (``connect_timeout`` only covers connection
    setup). It should be derived from ``guardrails.query_timeout_s``.

    Args:
        conn: Connection parameters (host, port, credentials, database).
        network_timeout_s: Read/write socket timeout in seconds, or ``None`` to fall
            back to the ``read_timeout`` credential (if any).

    Returns:
        An open pymysql connection.

    Raises:
        ImportError: If the ``pymysql`` driver is not installed.
    """
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - depends on the installed extra
        raise ImportError(
            "The MySQL dialect requires the 'mysql' extra (pip install 'deep-db-agents[mysql]')."
        ) from exc

    cred = conn.credential
    read_timeout = network_timeout_s if network_timeout_s is not None else cred.get("read_timeout")
    return pymysql.connect(
        host=conn.host or "localhost",
        port=conn.port or 3306,
        user=cred.get("user"),
        password=cred.get("password", ""),
        database=conn.database,
        connect_timeout=cred.get("connect_timeout", 10),
        read_timeout=read_timeout,
        write_timeout=read_timeout,
        cursorclass=pymysql.cursors.Cursor,
    )


def estimate_rows(cursor, sql: str) -> int:
    """Estimate the rows returned by ``sql`` using ``EXPLAIN`` (``rows`` column).

    Args:
        cursor: DB-API cursor to run the ``EXPLAIN`` on.
        sql: SQL query to estimate.

    Returns:
        Estimated row count, or 0 if it cannot be determined.
    """
    cursor.execute(f"EXPLAIN {sql}")
    cols = [d[0] for d in (cursor.description or [])]
    try:
        idx = cols.index("rows")
    except ValueError:
        return 0
    total = 0
    for row in cursor.fetchall():
        value = row[idx]
        if value is not None:
            total += int(value)
    return total
