"""Shared base for document 'search' dialects (Elasticsearch, OpenSearch).

``elasticsearch-py`` and ``opensearch-py`` expose the same REST API (``cat.indices``,
``indices.get_mapping``, ``count``, ``search``), so the entire tool logic is shared here;
each concrete dialect implements only the driver-specific connection opening (``_connect``).

Unlike the SQL dialects there is no need for a statement whitelist: the tools only use
read endpoints (``_search``, ``_count``, ``_cat``, ``_mapping``), never write or delete
endpoints (``_bulk``, ``_update``, ``_delete_by_query``...) — the main guardrail is
therefore the restriction on queryable indices, configured via the ``index`` key of the
credentials (single name, CSV list or ``*`` pattern).
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from abc import abstractmethod
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain.tools import BaseTool, ToolRuntime, tool
from langgraph.types import Command

from ..base import DbDialect
from ..connection import ConnectionConfig
from ..exceptions import DeepDbAgentError, EstimateExceededError, QueryNotAllowedError
from ..guardrails import GuardrailConfig, SessionBudget
from ..pooling import LazyClient
from ..query_errors import format_estimate_block, format_query_error
from ..tabular import docs_to_table
from ..workspace import materialize_result, write_command

# Ceiling on the size (in characters of the serialized JSON) of the ``aggregate`` result
# returned inline to the agent. High-cardinality or nested ``terms`` aggregations can
# generate thousands of buckets: past this threshold the full result is materialized to
# file and only a preview is returned to the agent.
MAX_AGG_INLINE_CHARS = 20_000
# Length of the truncated JSON text preview when the threshold is exceeded.
MAX_AGG_PREVIEW_CHARS = 2_000


def allowed_index_pattern(conn: ConnectionConfig) -> str:
    """Returns the index pattern/CSV the agent may operate on, from credentials (default ``*``).

    Args:
        conn: Connection parameters whose ``credential["index"]`` is read.

    Returns:
        The configured index pattern/CSV, or ``"*"`` if unset.
    """
    pattern = conn.credential.get("index")
    return pattern.strip() if isinstance(pattern, str) and pattern.strip() else "*"


def resolve_index(requested: str | None, allowed: str) -> str:
    """Validates the requested index against the allowed pattern, raising on violation.

    ``allowed`` can be a single name, a CSV list or a ``*`` pattern. If ``requested`` is
    ``None``, ``allowed`` is used directly (pattern/CSV expansion is left to the driver). If
    ``requested`` is given, every CSV element of it must match at least one allowed pattern,
    otherwise the request is rejected.

    Args:
        requested: The index/indices requested by the agent, or ``None`` for the default scope.
        allowed: The configured allowed index pattern/CSV.

    Returns:
        The index/indices to actually query.

    Raises:
        QueryNotAllowedError: If a requested index does not match any allowed pattern.
    """
    if allowed in ("*", ""):
        return requested or "*"
    if requested is None:
        return allowed
    allowed_parts = [p.strip() for p in allowed.split(",") if p.strip()]
    requested_parts = [p.strip() for p in requested.split(",") if p.strip()]
    for part in requested_parts:
        if not any(fnmatch.fnmatchcase(part, pat) for pat in allowed_parts):
            raise QueryNotAllowedError(f"Index {part!r} not allowed. Allowed indices: {allowed}.")
    return requested


def parse_query(text: str | None, *, what: str = "query") -> dict:
    """Decodes the Query DSL clause (JSON), or returns ``match_all`` if absent.

    Args:
        text: The raw JSON text of the Query DSL clause, or ``None``.
        what: Label used in the error message if parsing fails.

    Returns:
        The decoded query clause as a dict, or ``{"match_all": {}}`` if ``text`` is empty.

    Raises:
        QueryNotAllowedError: If ``text`` is not valid JSON or is not a JSON object.
    """
    if text is None or not text.strip():
        return {"match_all": {}}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise QueryNotAllowedError(f"Invalid {what} JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise QueryNotAllowedError(f"{what} must be a JSON object (Query DSL clause).")
    return parsed


def parse_aggs(value: str | dict) -> dict:
    """Decodes the ``aggs`` clause (JSON), which must be a non-empty object.

    Args:
        value: The ``aggs`` clause, either already a dict or a raw JSON string.

    Returns:
        The decoded ``aggs`` clause as a dict.

    Raises:
        QueryNotAllowedError: If ``value`` is empty, not valid JSON, or not a non-empty
            JSON object.
    """
    if isinstance(value, dict):
        parsed = value
    else:
        if value is None or not value.strip():
            raise QueryNotAllowedError("The 'aggs' clause is required and cannot be empty.")
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise QueryNotAllowedError(f"Invalid aggs JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise QueryNotAllowedError(
            "aggs must be a non-empty JSON object (Query DSL 'aggs' clause)."
        )
    return parsed


def hits_to_table(hits: list[dict]) -> tuple[list[str], list[list[Any]]]:
    """Converts search hits into (columns, rows) for materialization to file.

    Args:
        hits: The raw list of hit objects returned by a search.

    Returns:
        A tuple of (column names, row values). Columns are the union of ``_source`` keys
        plus ``_id``/``_index``; nested values are serialized.
    """
    return docs_to_table(_hits_to_docs(hits))


def _hits_to_docs(hits: list[dict]) -> list[dict]:
    """Flattens raw search hits into plain documents with ``_id``/``_index`` merged in.

    Args:
        hits: The raw list of hit objects returned by a search.

    Returns:
        A list of dicts, each containing ``_id``, ``_index`` and the ``_source`` fields.
    """
    return [{"_id": h.get("_id"), "_index": h.get("_index"), **h.get("_source", {})} for h in hits]


class SearchDialect(DbDialect):
    """Base for document search engine dialects (Elasticsearch, OpenSearch)."""

    schemes: tuple[str, ...] = ()

    def __init__(self) -> None:
        """Initializes the dialect with an empty, lazily-populated client cache."""
        # The ES/OS client internally manages a thread-safe connection pool: it is reused
        # for the whole lifetime of the agent instead of being recreated on every tool call.
        self._client_cache = LazyClient()

    @abstractmethod
    def _connect(self, conn: ConnectionConfig, guardrails: GuardrailConfig | None = None) -> Any:
        """Opens the driver-specific client from the credentials.

        Args:
            conn: Connection parameters (host, port, credentials).
            guardrails: Optional guardrail configuration.

        Returns:
            The opened driver client (elasticsearch-py or opensearch-py).
        """

    def close(self) -> None:
        """Closes the reused ES/OS client. Call this at the end of the agent's lifetime."""
        self._client_cache.close()

    # Hook overridable in tests (analogous to MongoDB/Neo4j's ``_db``/``_session``).
    @contextmanager
    def _client(self, conn: ConnectionConfig, guardrails: GuardrailConfig | None = None):
        """Yields the cached driver client, opening it lazily on first use.

        Args:
            conn: Connection parameters used to open the client if not already cached.
            guardrails: Optional guardrail configuration passed through to ``_connect``.

        Yields:
            The driver client.
        """
        yield self._client_cache.get(lambda: self._connect(conn, guardrails))

    def build_tools(
        self,
        conn: ConnectionConfig,
        guardrails: GuardrailConfig,
        materialize_enable: bool = False,
        backend: BackendProtocol | None = None,
    ) -> Sequence[BaseTool]:
        """Builds the LangChain tools exposed to the agent for this search dialect.

        Args:
            conn: Connection parameters injected into the tool closures.
            guardrails: Guardrail configuration (limits, timeout, budget) enforced by every
                tool.
            materialize_enable: Whether to also expose the ``materialize_query`` tool.
            backend: Filesystem backend injected into the file-writing tools' closures
                (``aggregate`` overflow and ``materialize_query``); when ``None`` those tools
                fall back to a truncated preview or report that no backend is configured.

        Returns:
            The sequence of tools: ``list_indices``, ``describe_index``, ``count_documents``,
            ``sample_documents``, ``run_query``, ``search_query``, ``aggregate`` and, if
            enabled, ``materialize_query``.
        """
        metrics = getattr(self, "_metrics", None)
        budget = SessionBudget(guardrails.row_budget, metrics=metrics)
        allowed = allowed_index_pattern(conn)
        timeout = guardrails.query_timeout_s

        def _paginated_search(
            target: str, q: dict, page: int, page_size: int | None
        ) -> tuple[list[dict], int]:
            limit = guardrails.clamp_limit(page_size)
            offset = max(page, 0) * limit
            with self._client(conn, guardrails) as client:
                estimate = client.count(index=target, body={"query": q}, request_timeout=timeout)[
                    "count"
                ]
                guardrails.check_estimate(estimate, metrics)
                res = client.search(
                    index=target,
                    body={"query": q, "from": offset, "size": limit},
                    request_timeout=timeout,
                )
            return res.get("hits", {}).get("hits", []), limit

        @tool
        def list_indices() -> str:
            """Lists the indices visible to this agent (within the configured index scope)."""
            try:
                with self._client(conn, guardrails) as client:
                    stats = client.cat.indices(
                        index=allowed, format="json", request_timeout=timeout
                    )
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=allowed, what="index listing")
            if not stats:
                return f"No index found for the allowed pattern {allowed!r}."
            lines = [f"{s.get('index')}: {s.get('docs.count', '?')} documents" for s in stats]
            return "Indices:\n" + "\n".join(lines)

        @tool
        def describe_index(index: str | None = None) -> str:
            """Reports the field mapping (names and types) of one or more indices.

            ``index`` can be a single index name, a CSV list, or a wildcard pattern; it must
            stay within the configured index scope.
            """
            try:
                target = resolve_index(index, allowed)
                with self._client(conn, guardrails) as client:
                    mapping = client.indices.get_mapping(index=target, request_timeout=timeout)
            except QueryNotAllowedError as exc:  # noqa: BLE001 - scope violation -> feedback to the agent
                return format_query_error(exc, query=index, what="index mapping")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=index, what="index mapping")
            lines = []
            for idx_name, idx_mapping in mapping.items():
                props = idx_mapping.get("mappings", {}).get("properties", {})
                fields = ", ".join(f"{f}:{d.get('type', '?')}" for f, d in props.items())
                lines.append(f"{idx_name}: {fields or '(no mapped field)'}")
            return "\n".join(lines)

        @tool
        def count_documents(index: str | None = None, query: str | dict | None = None) -> str:
            """Counts the documents matching a Query DSL clause, without retrieving them.

            Use it BEFORE searching, to assess whether the result volume is manageable
            (explore before extracting). ``query`` is a string containing the JSON Query DSL
            'query' clause, e.g. '{"match": {"title": "foo"}}'; omit it to count all documents.
            """
            try:
                target = resolve_index(index, allowed)
                if isinstance(query, dict):
                    q = query
                else:
                    q = parse_query(query, what="query")
                with self._client(conn, guardrails) as client:
                    total = client.count(index=target, body={"query": q}, request_timeout=timeout)[
                        "count"
                    ]
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error -> feedback to the agent
                return format_query_error(exc, query=str(query), what="query")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=str(query), what="count")
            return f"{target}: {total:,} documents" + (f" with query {query}" if query else "")

        @tool
        def sample_documents(index: str | None = None, limit: int = 5) -> str:
            """Returns a small sample of documents from the index scope (default 5)."""
            try:
                target = resolve_index(index, allowed)
                n = guardrails.clamp_limit(min(limit, 20))
                with self._client(conn, guardrails) as client:
                    res = client.search(
                        index=target,
                        body={"query": {"match_all": {}}, "size": n},
                        request_timeout=timeout,
                    )
            except QueryNotAllowedError as exc:  # noqa: BLE001 - scope violation -> feedback to the agent
                return format_query_error(exc, query=index, what="search")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=target, what="search")
            docs = _hits_to_docs(res.get("hits", {}).get("hits", []))
            budget.charge(len(docs))
            return f"Sample of {target} ({len(docs)} documents): {docs}"

        @tool
        def run_query(
            index: str | None = None,
            query: str | dict | None = None,
            page: int = 0,
            page_size: int | None = None,
        ) -> str:
            """Runs a read-only Query DSL search with forced limit and pagination.

            ``query`` is a string containing the JSON Query DSL 'query' clause (e.g.
            '{"bool": {"must": [{"match": {"status": "ok"}}]}}'). Before extraction the number
            of matching documents is estimated and the search is blocked if it exceeds the
            threshold. Prefer pushing aggregations into the engine (terms/metric aggregations,
            `_count`) instead of downloading documents and aggregating them by hand.
            """
            try:
                target = resolve_index(index, allowed)
                if isinstance(query, dict):
                    q = query
                else:
                    q = parse_query(query, what="query")
                hits, limit = _paginated_search(target, q, page, page_size)
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error -> feedback to the agent
                return format_query_error(exc, query=str(query), what="query")
            except EstimateExceededError as exc:  # noqa: BLE001 - estimate too high -> feedback to the agent
                return format_estimate_block(exc, what="search")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=str(query), what="search")
            budget.charge(len(hits))
            docs = _hits_to_docs(hits)
            more = (
                f" More documents available: ask for page={page + 1}." if len(hits) == limit else ""
            )
            return f"{len(docs)} documents (page {page}, page_size {limit}).{more}\n{docs}"

        @tool
        def search_query(
            index: str | None = None,
            query_string: str = "",
            page: int = 0,
            page_size: int | None = None,
        ) -> str:
            """Runs a simplified Lucene-like text search (the `query_string` DSL clause).

            Use this for natural, search-engine-style queries (e.g. 'status:active AND
            price:[10 TO 50]') instead of building a full Query DSL clause by hand. Forced
            limit and pagination apply, just like `run_query`.
            """
            try:
                target = resolve_index(index, allowed)
                q = {"query_string": {"query": query_string}}
                hits, limit = _paginated_search(target, q, page, page_size)
            except QueryNotAllowedError as exc:  # noqa: BLE001 - scope violation -> feedback to the agent
                return format_query_error(exc, query=query_string, what="query_string search")
            except EstimateExceededError as exc:  # noqa: BLE001 - estimate too high -> feedback to the agent
                return format_estimate_block(exc, what="query_string search")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=query_string, what="query_string search")
            budget.charge(len(hits))
            docs = _hits_to_docs(hits)
            more = (
                f" More documents available: ask for page={page + 1}." if len(hits) == limit else ""
            )
            return f"{len(docs)} documents (page {page}, page_size {limit}).{more}\n{docs}"

        @tool
        def aggregate(
            runtime: ToolRuntime,
            index: str | None = None,
            aggs: str | dict = "",
            query: str | dict | None = None,
        ) -> Command | str:
            """Runs one or more Query DSL aggregations in the engine, without returning documents.

            This is the PREFERRED way to summarize data (counts per group, averages, min/max,
            cardinality...): the engine does the work and you get back only compact results,
            not raw documents. Pass ``aggs`` as a JSON object mapping each aggregation name to
            its definition, e.g. '{"by_carrier": {"terms": {"field": "Carrier", "size": 20}},
            "avg_price": {"avg": {"field": "AvgTicketPrice"}}}'. An optional ``query`` (Query DSL
            clause) restricts the documents the aggregations run over; omit it to aggregate all
            documents. ``size`` is forced to 0, so no documents are downloaded. If the aggregation
            result is too large for the context (e.g. thousands of `terms` buckets) it is saved to
            file and only a preview is returned: reduce the buckets `size` to get it inline.
            """
            try:
                target = resolve_index(index, allowed)
                a = parse_aggs(aggs)
                if isinstance(query, dict):
                    q = query
                else:
                    q = parse_query(query, what="query")
                with self._client(conn, guardrails) as client:
                    res = client.search(
                        index=target,
                        body={"query": q, "size": 0, "aggs": a},
                        request_timeout=timeout,
                    )
            except QueryNotAllowedError as exc:  # noqa: BLE001 - parsing error -> feedback to the agent
                return format_query_error(exc, query=str(aggs), what="aggregation")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=str(aggs), what="aggregation")
            aggregations = res.get("aggregations", {})
            payload = json.dumps(aggregations, default=str)
            if len(payload) <= MAX_AGG_INLINE_CHARS:
                return f"Aggregations on {target}:\n{payload}"
            # Output past the threshold: like the other tools, don't pour it into the
            # context. If a backend is available, materialize it to file (raw JSON, because
            # aggregations are nested and not tabular like materialize_result); otherwise
            # truncate with a preview and invite the agent to reduce the buckets.
            preview = payload[:MAX_AGG_PREVIEW_CHARS]
            if backend is not None:
                filename = f"aggregations_{uuid.uuid4().hex[:8]}.json"
                write_res = backend.write(filename, payload)
                if not write_res.error:
                    message = (
                        f"Aggregations on {target}: result too large "
                        f"({len(payload):,} characters) saved to {filename}.\n"
                        f"Preview (first {len(preview):,} characters):\n{preview}…\n"
                        "Reduce the buckets' 'size' or refine the aggregation to get it "
                        "fully inline."
                    )
                    return write_command(
                        message, runtime.tool_call_id, getattr(write_res, "files_update", None)
                    )
            return (
                f"Aggregations on {target}: result too large "
                f"({len(payload):,} characters), truncated to the first {len(preview):,}.\n"
                f"{preview}…\n"
                "Reduce the buckets' 'size' or refine the aggregation for a complete output."
            )

        @tool
        def materialize_query(
            runtime: ToolRuntime,
            index: str | None = None,
            query: str | dict | None = None,
            fmt: str = "csv",
            filename: str | None = None,
        ) -> Command | str:
            """Runs a Query DSL search and saves the result to file (Parquet/CSV).

            Returns ONLY metadata: path, columns, preview and statistics. Use it for analysis
            or charts on large volumes. Nested values are serialized.
            """
            if backend is None:
                return "Cannot write to file: no filesystem backend is configured."
            try:
                target = resolve_index(index, allowed)
                if isinstance(query, dict):
                    q = query
                else:
                    q = parse_query(query, what="query")
                with self._client(conn, guardrails) as client:
                    res = client.search(
                        index=target,
                        body={"query": q, "size": guardrails.hard_max_rows},
                        request_timeout=timeout,
                    )
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error -> feedback to the agent
                return format_query_error(exc, query=str(query), what="query")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error -> feedback to the agent
                return format_query_error(exc, query=str(query), what="search")
            hits = res.get("hits", {}).get("hits", [])
            budget.charge(len(hits))
            columns, rows = hits_to_table(hits)
            result = materialize_result(columns, rows, fmt=fmt, filename=filename, backend=backend)
            return write_command(result.to_summary(), runtime.tool_call_id, result.files_update)

        tools_list = [
            list_indices,
            describe_index,
            count_documents,
            sample_documents,
            run_query,
            search_query,
            aggregate,
        ]
        if materialize_enable:
            tools_list.append(materialize_query)
        return tools_list
