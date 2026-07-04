"""Conversion of heterogeneous documents/records into tabular (columns, rows) form.

Helper shared by non-relational dialects (MongoDB, Neo4j, Elasticsearch/OpenSearch) for
materializing results to file: columns are the ordered union of top-level keys, nested
values are serialized to a string.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def scalar_json(value: Any) -> Any:
    """Keep scalars as-is; serialize nested values (dict/list) to a JSON string.

    Args:
        value: The value to normalize.

    Returns:
        Any: ``value`` unchanged if it is a scalar, otherwise its JSON string
        serialization.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def union_columns(docs: list[dict]) -> list[str]:
    """Compute the ordered (first-occurrence) union of documents' top-level keys.

    Args:
        docs: List of documents (dicts) to inspect.

    Returns:
        list[str]: The keys found across all documents, in first-occurrence order.
    """
    columns: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        for key in doc:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def docs_to_table(
    docs: list[dict], *, scalar: Callable[[Any], Any] = scalar_json
) -> tuple[list[str], list[list[Any]]]:
    """Convert heterogeneous documents into (columns, rows).

    Args:
        docs: List of documents (dicts) to convert.
        scalar: Callable used to serialize nested values; the default uses JSON,
            dialects can pass a specific serializer (e.g. ``bson.json_util`` for
            MongoDB).

    Returns:
        tuple[list[str], list[list[Any]]]: The ordered column names and the list of
        rows, each row aligned with ``columns``.
    """
    columns = union_columns(docs)
    rows = [[scalar(doc.get(col)) for col in columns] for doc in docs]
    return columns, rows
