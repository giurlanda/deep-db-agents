"""Driver-specific helpers for MongoDB (pymongo)."""

from __future__ import annotations

import json
from typing import Any

from ... import tabular
from ...connection import ConnectionConfig
from ...exceptions import QueryNotAllowedError

# Write stages not allowed in a read-only aggregation pipeline.
_WRITE_STAGES = {"$out", "$merge"}
# Operators that execute arbitrary JavaScript server-side: forbidden even in read-only mode,
# because they allow arbitrary computation (and evasion of intent) inside the DB.
_JS_OPERATORS = {"$where", "$function", "$accumulator"}
# Keys forbidden anywhere in the document (filter or pipeline), at any depth.
_FORBIDDEN_KEYS = _WRITE_STAGES | _JS_OPERATORS


def _reject_forbidden_keys(obj: Any, *, what: str) -> None:
    """Recursively scans ``obj`` and raises if it finds write stages or JS operators.

    Args:
        obj: The value to scan (dict, list, or scalar).
        what: Description of the value being validated, used in the error message.

    Raises:
        QueryNotAllowedError: If a forbidden stage/operator is found at any depth.

    Checking only the first level would miss nested stages/operators (e.g. ``$where``
    inside a ``$match``, or ``$out`` inside a ``$facet``): the scan is therefore recursive.
    """
    if isinstance(obj, dict):
        forbidden = _FORBIDDEN_KEYS & obj.keys()
        if forbidden:
            raise QueryNotAllowedError(
                f"Operator/stage not allowed in the {what}: {', '.join(sorted(forbidden))}."
            )
        for value in obj.values():
            _reject_forbidden_keys(value, what=what)
    elif isinstance(obj, list):
        for item in obj:
            _reject_forbidden_keys(item, what=what)


def ensure_read_only_filter(filter_: Any) -> Any:
    """Validates a filter/projection: no server-side JS operator (``$where``/``$function``).

    Args:
        filter_: The filter or projection document to validate.

    Returns:
        The same value, unchanged, if validation passes.

    Raises:
        QueryNotAllowedError: If a forbidden operator is found.
    """
    _reject_forbidden_keys(filter_, what="filter")
    return filter_


def connect(conn: ConnectionConfig, *, socket_timeout_ms: int | None = None):
    """Opens a MongoClient from the credentials. Lazy import of the driver.

    Args:
        conn: Connection configuration (host, port, credentials).
        socket_timeout_ms: Timeout enforced on network operations (socket read/write):
            without it, a ``find``/``aggregate`` whose socket blocks would hang forever even
            after the server has been selected. Should be derived from
            ``guardrails.query_timeout_s`` to align with the behavior of the SQL dialects.

    Returns:
        A connected ``pymongo.MongoClient`` instance.

    Raises:
        ImportError: If the ``mongodb`` extra is not installed.
    """
    try:
        import pymongo
    except ImportError as exc:  # pragma: no cover - depends on the installed extra
        raise ImportError(
            "The MongoDB dialect requires the 'mongodb' extra "
            "(pip install 'deep-db-agents[mongodb]')."
        ) from exc

    cred = conn.credential
    connect_timeout_ms = int(cred.get("connect_timeout", 10)) * 1000
    kwargs: dict[str, Any] = {
        "host": conn.host or "localhost",
        "port": conn.port or 27017,
        "serverSelectionTimeoutMS": connect_timeout_ms,
        "connectTimeoutMS": connect_timeout_ms,
    }
    if socket_timeout_ms and socket_timeout_ms > 0:
        kwargs["socketTimeoutMS"] = socket_timeout_ms
    if cred.get("user"):
        kwargs["username"] = cred["user"]
    if cred.get("password"):
        kwargs["password"] = cred["password"]
    auth_source = cred.get("authSource") or cred.get("auth_source")
    if auth_source:
        kwargs["authSource"] = auth_source
    return pymongo.MongoClient(**kwargs)


def parse_json(text: str | None, *, what: str = "argument") -> Any:
    """Decodes JSON (extended, with ObjectId/date support) or returns an empty default.

    Args:
        text: The JSON string to decode, or ``None``/empty string.
        what: Description of the value being parsed, used in the error message.

    Returns:
        The decoded value, or ``None`` if ``text`` is empty.

    Raises:
        QueryNotAllowedError: If ``text`` is not valid JSON.
    """
    if text is None or (isinstance(text, str) and not text.strip()):
        return None
    if not isinstance(text, str):
        return text
    try:
        from bson import json_util

        return json_util.loads(text)
    except ImportError:  # pragma: no cover - bson is bundled with pymongo
        return json.loads(text)
    except ValueError as exc:
        raise QueryNotAllowedError(f"Invalid {what} JSON: {exc}") from exc


def ensure_read_only_pipeline(pipeline: Any) -> list:
    """Validates the pipeline: a list with no write stages ($out/$merge) nor JS operators,
    at any depth (even nested in ``$facet``/``$lookup``).

    Args:
        pipeline: The aggregation pipeline to validate.

    Returns:
        The same pipeline, unchanged, if validation passes.

    Raises:
        QueryNotAllowedError: If ``pipeline`` is not a list, or contains a forbidden stage.
    """
    if not isinstance(pipeline, list):
        raise QueryNotAllowedError("The aggregation pipeline must be a list of stages.")
    _reject_forbidden_keys(pipeline, what="pipeline")
    return pipeline


def _scalar(value: Any) -> Any:
    """Keeps scalars as-is; serializes nested values (dict/list) to string.

    Args:
        value: The value to normalize.

    Returns:
        The scalar unchanged, or a JSON string for dict/list values.

    Uses ``bson.json_util`` (bundled with pymongo) to serialize BSON types
    (ObjectId, date...), with a plain JSON fallback.
    """
    if isinstance(value, (dict, list)):
        try:
            from bson import json_util

            return json_util.dumps(value)
        except ImportError:  # pragma: no cover
            return json.dumps(value, default=str)
    return value


def docs_to_table(docs: list[dict]) -> tuple[list[str], list[list[Any]]]:
    """Converts heterogeneous documents into (columns, rows) for file materialization.

    Args:
        docs: The list of documents to convert.

    Returns:
        A tuple of (column names, row values).
    """
    return tabular.docs_to_table(docs, scalar=_scalar)
