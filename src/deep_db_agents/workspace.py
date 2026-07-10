"""Materialization of large results to file, outside the agent's context.

The pattern: datasets too large to "read" are saved to file (Parquet or CSV) in a
workspace area; only metadata, a preview, and a few statistics are returned to the
agent. This decouples *data volume* from *context volume*.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
import uuid
from collections.abc import Callable, Iterable, Sequence
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
        truncated: ``True`` when writing stopped before all rows were saved because the
            file reached the maximum allowed size (``max_materialized_bytes``); the saved
            file is then incomplete.
    """

    path: str
    fmt: str
    row_count: int
    columns: list[str]
    preview: list[dict[str, Any]]
    stats: dict[str, dict[str, float]]
    files_update: dict[str, Any] | None = None
    truncated: bool = False

    def to_summary(self) -> str:
        """Build a compact textual summary to return to the agent.

        Returns:
            str: A multi-line summary describing where the result was saved, its
            columns, numeric statistics (if any), and a preview of the first rows.
            When the result was truncated, a warning that the file is incomplete is
            included.
        """
        lines = [
            f"Query executed. {self.row_count:,} rows saved to {self.path} ({self.fmt}).",
        ]
        if self.truncated:
            lines.append(
                "WARNING: the file is INCOMPLETE — writing stopped at the maximum allowed "
                "size for a materialized file; some rows were not written. Refine or "
                "aggregate the query to fit the result within the limit."
            )
        lines.append(f"Columns: {', '.join(self.columns)}")
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


def json_bytes(obj: Any) -> int:
    """Estimate the serialized JSON size, in bytes, of a document/record.

    Used by the document dialects to bound how much of a lazy cursor they pull into
    memory before materialization: they stop consuming once the accumulated JSON size
    would exceed the budget.

    Args:
        obj: The object to size (typically a document/record dict).

    Returns:
        int: The UTF-8 byte length of ``obj`` serialized to JSON.
    """
    return len(json.dumps(obj, default=str).encode("utf-8"))


def take_within_bytes(
    items: Iterable[Any], size_of: Callable[[Any], int], max_bytes: int | None
) -> tuple[list[Any], bool]:
    """Consume ``items`` lazily, keeping only those that fit within ``max_bytes``.

    Iteration stops as soon as adding the next item would exceed the budget, so a lazy
    source (e.g. a database cursor) is not fully pulled into memory.

    Args:
        items: The items to consume (any iterable, possibly lazy).
        size_of: Callable returning the byte size of a single item.
        max_bytes: Maximum cumulative byte size to keep, or ``None`` to keep everything.

    Returns:
        tuple[list[Any], bool]: The kept items and a flag that is ``True`` when iteration
        stopped early because the budget was reached (i.e. more items were available).
    """
    if max_bytes is None:
        return list(items), False
    kept: list[Any] = []
    total = 0
    for item in items:
        size = size_of(item)
        if total + size > max_bytes:
            return kept, True
        kept.append(item)
        total += size
    return kept, False


def materialize_result(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    backend: BackendProtocol,
    *,
    fmt: str = "csv",
    filename: str | None = None,
    preview_rows: int = 5,
    max_bytes: int | None = None,
) -> MaterializedResult:
    """Save ``rows`` to file (bounded by ``max_bytes``) and return only the metadata.

    Rows are serialized incrementally and consumed lazily: only whole rows whose
    cumulative size stays below ``max_bytes`` are written, and iteration stops before the
    limit is exceeded. The written file is therefore never larger than ``max_bytes`` and,
    for a lazy ``rows`` source, memory stays bounded too. When the budget stops the write
    early, the returned result is flagged as ``truncated``.

    Args:
        columns: Ordered column names.
        rows: Row data, each row aligned with ``columns``; may be a lazy iterable.
        backend: The deepagents backend used to write the file (workspace filesystem).
        fmt: Output format, either ``"csv"`` (stdlib only, default) or ``"parquet"``
            (requires the ``analysis`` extra: pandas + pyarrow).
        filename: Name of the file to write; if ``None``, a random name is generated.
        preview_rows: Number of rows to include in the returned preview.
        max_bytes: Maximum size of the written file, in bytes; ``None`` disables the cap.
            The size is measured on the CSV serialization; a Parquet file (binary and
            compressed) written from the same rows stays at or below this size.

    Returns:
        MaterializedResult: Metadata about the saved file, including path, row count,
        columns, a preview, numeric statistics, and whether the write was truncated.

    Raises:
        ImportError: If ``fmt="parquet"`` and pandas is not installed.
        OSError: If writing the file via ``backend`` fails.
        ValueError: If ``fmt`` is neither ``"csv"`` nor ``"parquet"``.
    """
    columns = list(columns)

    if filename is None:
        filename = f"result_{uuid.uuid4().hex[:8]}.{fmt}"
    elif not filename.startswith("/"):
        filename = "/" + filename

    # Serialize row by row to CSV, tracking the cumulative UTF-8 byte size, and stop before
    # exceeding max_bytes so only whole records within the limit are written. CSV rows are
    # formatted independently, so the accumulated lines equal a single writerows() call.
    header_buf = io.StringIO()
    csv.writer(header_buf).writerow(columns)
    header_text = header_buf.getvalue()
    total = len(header_text.encode("utf-8"))
    parts = [header_text]
    kept_rows: list[Sequence[Any]] = []
    truncated = False
    for row in rows:
        row_buf = io.StringIO()
        csv.writer(row_buf).writerow(row)
        row_text = row_buf.getvalue()
        row_size = len(row_text.encode("utf-8"))
        if max_bytes is not None and total + row_size > max_bytes:
            truncated = True
            break
        parts.append(row_text)
        kept_rows.append(row)
        total += row_size

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
        pd.DataFrame(list(kept_rows), columns=columns).to_parquet(buf, index=False)
        responses = backend.upload_files([(filename, buf.getvalue())])
        if responses and responses[0].error:
            raise OSError(f"Failed to write {filename!r}: {responses[0].error}")
        # upload_files returns no state update; StateBackend applies it internally.
        files_update = None
    elif fmt == "csv":
        # CSV is text: reuse the incrementally built content and write it via write().
        result = backend.write(filename, "".join(parts))
        if result.error:
            raise OSError(f"Failed to write {filename!r}: {result.error}")
        # StateBackend reports the files to apply to the agent's state; external
        # backends (Filesystem/Store) persist directly and return None here.
        files_update = getattr(result, "files_update", None)
    else:
        raise ValueError(f"Unsupported format: {fmt!r}. Use 'parquet' or 'csv'.")

    preview = [dict(zip(columns, r, strict=False)) for r in kept_rows[:preview_rows]]
    return MaterializedResult(
        path=filename,
        fmt=fmt,
        row_count=len(kept_rows),
        columns=columns,
        preview=preview,
        stats=_numeric_stats(columns, kept_rows),
        files_update=files_update,
        truncated=truncated,
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
