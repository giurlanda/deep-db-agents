"""Materialization of large results to file, outside the agent's context.

The pattern: datasets too large to "read" are saved to file (Parquet or CSV) in a
workspace area; only metadata, a preview, and a few statistics are returned to the
agent. This decouples *data volume* from *context volume*.
"""

from __future__ import annotations

import csv
import io
import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain_core.messages import ToolMessage
from langgraph.types import Command


@dataclass
class MaterializedResult:
    """Metadata about a result saved to file (never the full rows).

    Attributes:
        path: Path of the file the result was written to.
        fmt: Format the result was written in (``"csv"`` or ``"parquet"``).
        row_count: Total number of rows written.
        columns: Ordered list of column names.
        preview: The first few rows, as a list of ``{column: value}`` dicts.
        stats: Per-column numeric statistics (``min``/``max``/``mean``), only for
            columns containing numeric values.
        files_update: State update returned by a ``StateBackend`` write (the files the
            agent must apply to its graph state), or ``None`` for external backends that
            persist directly (``FilesystemBackend``/``StoreBackend``).
    """

    path: str
    fmt: str
    row_count: int
    columns: list[str]
    preview: list[dict[str, Any]]
    stats: dict[str, dict[str, float]]
    files_update: dict[str, Any] | None = None

    def to_summary(self) -> str:
        """Build a compact textual summary to return to the agent.

        Returns:
            str: A multi-line summary describing where the result was saved, its
            columns, numeric statistics (if any), and a preview of the first rows.
        """
        lines = [
            f"Query executed. {self.row_count:,} rows saved to {self.path} ({self.fmt}).",
            f"Columns: {', '.join(self.columns)}",
        ]
        if self.stats:
            stat_parts = [
                f"{col}: min={s['min']:g}, max={s['max']:g}, mean={s['mean']:g}"
                for col, s in self.stats.items()
            ]
            lines.append("Numeric statistics: " + "; ".join(stat_parts))
        lines.append(f"Preview (first {len(self.preview)} rows): {self.preview}")
        return "\n".join(lines)


def _numeric_stats(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> dict[str, dict[str, float]]:
    """Compute min/max/mean for each numeric column.

    Args:
        columns: Ordered column names.
        rows: Row data, each row aligned with ``columns``.

    Returns:
        dict[str, dict[str, float]]: For each column containing at least one numeric
        (non-``None``) value, a dict with ``min``, ``max`` and ``mean`` keys. Columns
        with no numeric values are omitted.
    """
    stats: dict[str, dict[str, float]] = {}
    for idx, col in enumerate(columns):
        values = [r[idx] for r in rows if isinstance(r[idx], (int, float)) and r[idx] is not None]
        if values:
            stats[col] = {
                "min": float(min(values)),
                "max": float(max(values)),
                "mean": float(statistics.fmean(values)),
            }
    return stats


def materialize_result(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    backend: BackendProtocol,
    *,
    fmt: str = "csv",
    filename: str | None = None,
    preview_rows: int = 5,
) -> MaterializedResult:
    """Save ``rows`` to file and return only the metadata.

    Args:
        columns: Ordered column names.
        rows: Row data, each row aligned with ``columns``.
        backend: The deepagents backend used to write the file (workspace filesystem).
        fmt: Output format, either ``"csv"`` (stdlib only, default) or ``"parquet"``
            (requires the ``analysis`` extra: pandas + pyarrow).
        filename: Name of the file to write; if ``None``, a random name is generated.
        preview_rows: Number of rows to include in the returned preview.

    Returns:
        MaterializedResult: Metadata about the saved file, including path, row count,
        columns, a preview, and numeric statistics.

    Raises:
        ImportError: If ``fmt="parquet"`` and pandas is not installed.
        OSError: If writing the file via ``backend`` fails.
        ValueError: If ``fmt`` is neither ``"csv"`` nor ``"parquet"``.
    """
    columns = list(columns)

    if filename is None:
        filename = f"result_{uuid.uuid4().hex[:8]}.{fmt}"

    if fmt == "parquet":
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - depends on the installed extra
            raise ImportError(
                "Parquet format requires the 'analysis' extra "
                "(pip install 'deep-db-agents[analysis]'). Use fmt='csv' instead."
            ) from exc
        # Parquet is binary: serialize in memory and upload as bytes via upload_files,
        # which the backend base64-encodes (write() only accepts text).
        buf = io.BytesIO()
        pd.DataFrame(list(rows), columns=columns).to_parquet(buf, index=False)
        responses = backend.upload_files([(filename, buf.getvalue())])
        if responses and responses[0].error:
            raise OSError(f"Failed to write {filename!r}: {responses[0].error}")
        # upload_files returns no state update; StateBackend applies it internally.
        files_update = None
    elif fmt == "csv":
        # CSV is text: build it in memory and write it via write().
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        writer.writerows(rows)
        result = backend.write(filename, buf.getvalue())
        if result.error:
            raise OSError(f"Failed to write {filename!r}: {result.error}")
        # StateBackend reports the files to apply to the agent's state; external
        # backends (Filesystem/Store) persist directly and return None here.
        files_update = getattr(result, "files_update", None)
    else:
        raise ValueError(f"Unsupported format: {fmt!r}. Use 'parquet' or 'csv'.")

    preview = [dict(zip(columns, r, strict=False)) for r in rows[:preview_rows]]
    return MaterializedResult(
        path=filename,
        fmt=fmt,
        row_count=len(rows),
        columns=columns,
        preview=preview,
        stats=_numeric_stats(columns, rows),
        files_update=files_update,
    )


def write_command(
    message: str, tool_call_id: str | None, files_update: dict[str, Any] | None
) -> Command:
    """Wrap a tool's file-write result in a ``Command`` for the agent.

    Returns a ``Command`` carrying the tool message plus, when a ``StateBackend`` produced
    a ``files_update``, the ``files`` state update the agent must apply. External backends
    (Filesystem/Store) persist directly, so ``files_update`` is ``None`` and no ``files``
    key is added.

    Args:
        message: Textual summary returned to the agent as the tool result.
        tool_call_id: Identifier of the originating tool call (from ``ToolRuntime``).
        files_update: Files to apply to the agent state, or ``None`` for external backends.

    Returns:
        Command: The state update wrapping the tool message and any file updates.
    """
    update: dict[str, Any] = {"messages": [ToolMessage(content=message, tool_call_id=tool_call_id)]}
    if files_update:
        update["files"] = files_update
    return Command(update=update)
