"""PostgreSQL-specific driver helpers (psycopg 3)."""

from __future__ import annotations

import json

from ...connection import ConnectionConfig


def connect(conn: ConnectionConfig, *, network_timeout_s: int | None = None):
    """Open a psycopg connection from credentials, with lazy driver import.

    ``network_timeout_s`` configures TCP keepalives so that a connection whose peer is dead
    (socket hanging, no data) is torn down within ~``network_timeout_s``, instead of waiting
    indefinitely. It should be derived from ``guardrails.query_timeout_s``.

    Args:
        conn: Connection parameters (host, port, credentials, database).
        network_timeout_s: Idle time in seconds before keepalive probing starts, or
            ``None``/``0`` to disable keepalives.

    Returns:
        An open psycopg connection.

    Raises:
        ImportError: If the ``psycopg`` driver is not installed.
    """
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on the installed extra
        raise ImportError(
            "The Postgres dialect requires the 'postgres' extra "
            "(pip install 'deep-db-agents[postgres]')."
        ) from exc

    cred = conn.credential
    keepalive_kwargs: dict[str, object] = {}
    if network_timeout_s and network_timeout_s > 0:
        # Detect a dead peer within ~network_timeout_s: after network_timeout_s of
        # inactivity, send a probe every few seconds and close after a few failed probes.
        keepalive_kwargs = {
            "keepalives": 1,
            "keepalives_idle": network_timeout_s,
            "keepalives_interval": 5,
            "keepalives_count": 3,
        }
    return psycopg.connect(
        host=conn.host or "localhost",
        port=conn.port or 5432,
        user=cred.get("user"),
        password=cred.get("password"),
        dbname=conn.database,
        connect_timeout=cred.get("connect_timeout", 10),
        autocommit=True,
        **keepalive_kwargs,
    )


def estimate_rows(cursor, sql: str) -> int:
    """Estimate rows via ``EXPLAIN (FORMAT JSON)``, reading ``Plan Rows`` of the root node.

    Args:
        cursor: DB-API cursor to run the ``EXPLAIN`` on.
        sql: SQL query to estimate.

    Returns:
        Estimated row count, or 0 if it cannot be determined.
    """
    cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}")
    plan = cursor.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    try:
        return int(plan[0]["Plan"]["Plan Rows"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0
