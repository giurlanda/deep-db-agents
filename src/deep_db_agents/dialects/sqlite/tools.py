"""Driver-specific helpers for SQLite (stdlib ``sqlite3`` module)."""

from __future__ import annotations

from urllib.request import pathname2url


def connect(path: str, *, busy_timeout_s: int = 10, read_only: bool = False):
    """Opens a SQLite connection.

    ``busy_timeout_s`` is the wait timeout on a locked database (the ``timeout`` parameter of
    ``sqlite3.connect``); query execution duration is bounded separately by a watchdog calling
    ``interrupt()`` (see ``FileSqlDialect``). ``check_same_thread=False`` allows the watchdog
    thread to call ``interrupt()`` on the connection.

    ``read_only=True`` opens the file in read-only mode (URI ``?mode=ro``), so that even a
    bypass of the keyword whitelist still cannot write to the database (defense in depth).
    This does not apply to ``:memory:``, which remains writable.

    Args:
        path: Path to the SQLite database file, or ``:memory:`` for an in-memory database.
        busy_timeout_s: Seconds to wait on a locked database before giving up.
        read_only: If True, opens the file in read-only mode via a ``file:`` URI.

    Returns:
        An open ``sqlite3.Connection``.
    """
    import sqlite3

    if read_only and path != ":memory:":
        uri = f"file:{pathname2url(path)}?mode=ro"
        return sqlite3.connect(uri, timeout=busy_timeout_s, check_same_thread=False, uri=True)
    return sqlite3.connect(path, timeout=busy_timeout_s, check_same_thread=False)
