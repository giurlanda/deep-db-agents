"""Driver-specific helpers for DuckDB (``duckdb`` package)."""

from __future__ import annotations

import glob
import os
import re

# File extension -> DuckDB reader function, for the data lake (path = folder).
_READERS = {
    ".parquet": "read_parquet",
    ".csv": "read_csv_auto",
    ".tsv": "read_csv_auto",
    ".json": "read_json_auto",
    ".ndjson": "read_json_auto",
}

# Captures cardinality estimates in the EXPLAIN plan text ("~N rows" or "EC: N").
_ESTIMATE_RE = re.compile(r"(?:EC:\s*|~\s*)([\d,]+)")


def _import_duckdb():
    """Lazily imports the ``duckdb`` package.

    Returns:
        The imported ``duckdb`` module.

    Raises:
        ImportError: If the ``duckdb`` package is not installed.
    """
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on the installed extra
        raise ImportError(
            "The DuckDB dialect requires the 'duckdb' extra (pip install 'deep-db-agents[duckdb]')."
        ) from exc
    return duckdb


def _sanitize_view_name(name: str) -> str:
    """Turns a file name into a valid SQL identifier (for data-lake views).

    Args:
        name: The raw file name (without extension) to sanitize.

    Returns:
        A valid SQL identifier derived from ``name``, prefixed with ``_`` if it would
        otherwise be empty or start with a digit.
    """
    safe = re.sub(r"\W", "_", name)
    if not safe or safe[0].isdigit():
        safe = "_" + safe
    return safe


def register_datalake_views(con, folder: str) -> list[str]:
    """Creates a view for each data file (parquet/csv/json) in ``folder``.

    Exposes the files as queryable tables with regular SQL, so the SQL dialect's tools work
    unmodified. Each view is named after the (sanitized) file name without its extension.

    Args:
        con: An open DuckDB connection on which to create the views.
        folder: Path to the data-lake folder to scan for data files.

    Returns:
        The list of created view names.
    """
    created: list[str] = []
    for filepath in sorted(glob.glob(os.path.join(folder, "*"))):
        ext = os.path.splitext(filepath)[1].lower()
        reader = _READERS.get(ext)
        if not reader:
            continue
        view = _sanitize_view_name(os.path.splitext(os.path.basename(filepath))[0])
        escaped = filepath.replace("'", "''")
        con.execute(f"CREATE OR REPLACE VIEW \"{view}\" AS SELECT * FROM {reader}('{escaped}')")
        created.append(view)
    return created


def connect_file(path: str, *, read_only: bool = True):
    """Opens a connection to a DuckDB file.

    ``read_only=True`` allows concurrent connections to the same file (needed when tool calls
    run in parallel): in write mode DuckDB holds an exclusive lock. Query duration is bounded
    separately by the watchdog calling ``interrupt()`` (see ``FileSqlDialect``).

    Args:
        path: Path to the DuckDB database file.
        read_only: If True, opens the connection in read-only mode.

    Returns:
        An open DuckDB connection.
    """
    duckdb = _import_duckdb()
    return duckdb.connect(path, read_only=read_only)


def connect_datalake(folder: str):
    """Opens an in-memory DuckDB connection with a view for each data file in the folder.

    Args:
        folder: Path to the data-lake folder to expose as tables.

    Returns:
        An in-memory DuckDB connection with one view registered per recognized data file.
    """
    duckdb = _import_duckdb()
    con = duckdb.connect(":memory:")
    register_datalake_views(con, folder)
    return con


def estimate_rows(cursor, sql: str) -> int:
    """Estimates the row count by reading the cardinality from the ``EXPLAIN`` plan.

    This is best-effort: any failure to run or parse ``EXPLAIN`` results in an estimate of 0
    rather than raising.

    Args:
        cursor: An open DuckDB cursor.
        sql: The SELECT statement to estimate.

    Returns:
        The largest cardinality estimate found in the EXPLAIN plan text, or 0 if none could
        be extracted.
    """
    try:
        cursor.execute(f"EXPLAIN {sql}")
        text = "\n".join(str(row[-1]) for row in cursor.fetchall())
    except Exception:  # noqa: BLE001 - the estimate is best-effort, never blocking on its own
        return 0
    nums = [int(m.replace(",", "")) for m in _ESTIMATE_RE.findall(text)]
    return max(nums) if nums else 0
