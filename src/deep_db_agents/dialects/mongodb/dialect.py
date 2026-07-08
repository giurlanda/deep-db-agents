"""Full MongoDB dialect (pymongo driver).

Carries the context-management principles over to the document model: exploration of
collections and inferred schema, counting before extraction, read-only find/aggregate with
forced limit and projection, and file materialization of large results.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager

from deepagents.backends.protocol import BackendProtocol
from langchain.tools import BaseTool, ToolRuntime, tool
from langgraph.types import Command

from ...base import DbDialect
from ...connection import ConnectionConfig
from ...exceptions import DeepDbAgentError, EstimateExceededError, QueryNotAllowedError
from ...guardrails import GuardrailConfig, SessionBudget
from ...pooling import LazyClient
from ...query_errors import format_estimate_block, format_query_error
from ...registry import register
from ...workspace import materialize_result, write_command
from . import tools
from .prompt import MONGODB_SYSTEM_PROMPT


@register("mongodb")
class MongoDBDialect(DbDialect):
    """Agent specialized on MongoDB."""

    schemes = ("mongodb",)

    def __init__(self) -> None:
        # The MongoClient is a thread-safe pool: reuse it for the agent's whole lifetime.
        self._client_cache = LazyClient()

    def system_prompt(self) -> str:
        return MONGODB_SYSTEM_PROMPT

    def close(self) -> None:
        """Closes the reused MongoClient. Call at the end of the agent's lifetime."""
        self._client_cache.close()

    # Driver-specific hook, overridable in tests.
    @contextmanager
    def _db(self, conn: ConnectionConfig, guardrails: GuardrailConfig | None = None):
        if not conn.database:
            raise QueryNotAllowedError(
                "Missing database name: specify 'database' in the credentials."
            )
        # Enforces a network-operation timeout consistent with query_timeout_s, so a
        # find/aggregate whose socket blocks fails (NetworkTimeout) instead of hanging the
        # whole tool node (parallel tool calls wait for each other in order).
        socket_timeout_ms = (guardrails.query_timeout_s * 1000) if guardrails else None
        client = self._client_cache.get(
            lambda: tools.connect(conn, socket_timeout_ms=socket_timeout_ms)
        )
        yield client[conn.database]

    def build_tools(
        self,
        conn: ConnectionConfig,
        guardrails: GuardrailConfig,
        materialize_enable: bool = False,
        backend: BackendProtocol | None = None,
    ) -> Sequence[BaseTool]:
        metrics = getattr(self, "_metrics", None)
        budget = SessionBudget(guardrails.row_budget, metrics=metrics)
        # Server-side execution timeout (maxTimeMS), aligned with query_timeout_s: interrupts
        # the operation on the server, not just the wait on the client-side socket.
        max_time_ms = guardrails.query_timeout_s * 1000 if guardrails.query_timeout_s else None

        def _find(coll, *args, **kwargs) -> list:
            """find() applying maxTimeMS on the cursor when the driver supports it."""
            cursor = coll.find(*args, **kwargs)
            if max_time_ms and hasattr(cursor, "max_time_ms"):
                cursor = cursor.max_time_ms(max_time_ms)
            return list(cursor)

        @tool
        def list_collections() -> str:
            """Lists the collections available in the current database."""
            try:
                with self._db(conn, guardrails) as db:
                    names = sorted(db.list_collection_names())
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, what="collection list")
            return "Collections: " + (", ".join(names) if names else "(none)")

        @tool
        def describe_collection(collection: str, sample_size: int = 20) -> str:
            """Infers a collection's schema by sampling a few documents.

            Collections are schemaless: this tool reports, for each top-level field, the
            types observed in the sample.
            """
            n = min(max(sample_size, 1), 100)
            try:
                with self._db(conn, guardrails) as db:
                    docs = _find(db[collection], limit=n)
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=collection, what="collection read")
            schema: dict[str, set[str]] = {}
            for doc in docs:
                for key, value in doc.items():
                    schema.setdefault(key, set()).add(type(value).__name__)
            if not schema:
                return f"Collection {collection}: no documents sampled."
            lines = [f"{k}: {', '.join(sorted(v))}" for k, v in schema.items()]
            return f"Inferred schema of {collection} (sample {len(docs)}):\n" + "\n".join(lines)

        @tool
        def count_documents(collection: str, filter: str | dict | None = None) -> str:
            """Counts the documents of a collection, with an optional JSON-string filter.

            Use it BEFORE extracting, to assess whether the volume is manageable (explore
            before extracting). The filter is a JSON document, e.g. ``{"year": 2025}``.
            """
            try:
                if isinstance(filter, dict):
                    query = filter
                else:
                    query = tools.parse_json(filter, what="filter") or {}
                tools.ensure_read_only_filter(query)
                with self._db(conn, guardrails) as db:
                    total = db[collection].count_documents(query, maxTimeMS=max_time_ms)
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error → feedback to the agent
                return format_query_error(exc, query=str(filter), what="filter")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=filter, what="filter")
            suffix = f" with filter {filter}" if filter else ""
            return f"{collection}: {total:,} documents{suffix}"

        @tool
        def sample_documents(collection: str, limit: int = 5) -> str:
            """Returns a small sample of documents from a collection (default 5)."""
            try:
                n = guardrails.clamp_limit(min(limit, 20))
                with self._db(conn, guardrails) as db:
                    docs = _find(db[collection], limit=n)
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=collection, what="collection read")
            budget.charge(len(docs))
            return f"Sample of {collection} ({len(docs)} documents): {docs}"

        @tool
        def find(
            collection: str,
            filter: str | dict | None = None,
            projection: str | dict | None = None,
            page: int = 0,
            page_size: int | None = None,
        ) -> str:
            """Runs a read-only find with forced limit and pagination.

            ``filter`` and ``projection`` are strings containing JSON documents. Before
            extraction the number of matching documents is estimated and the query is blocked
            if it exceeds the threshold. Always project only the fields you need.
            """
            try:
                try:
                    if isinstance(filter, dict):
                        query = filter
                    else:
                        query = tools.parse_json(filter, what="filter") or {}
                except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error → feedback to the agent
                    return format_query_error(exc, query=str(filter), what="filter")
                try:
                    if isinstance(projection, dict):
                        proj = projection
                    else:
                        proj = tools.parse_json(projection, what="projection")
                except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error → feedback to the agent
                    return format_query_error(exc, query=str(projection), what="projection")
                tools.ensure_read_only_filter(query)
                limit = guardrails.clamp_limit(page_size)
                skip = max(page, 0) * limit
                with self._db(conn, guardrails) as db:
                    coll = db[collection]
                    total = coll.count_documents(query, maxTimeMS=max_time_ms)
                    guardrails.check_estimate(total, metrics)
                    docs = _find(coll, query, proj, skip=skip, limit=limit)
            except QueryNotAllowedError as exc:  # noqa: BLE001 - forbidden filter → feedback
                return format_query_error(exc, query=str(filter), what="filter")
            except EstimateExceededError as exc:  # noqa: BLE001 - estimate too high → feedback to the agent
                return format_estimate_block(exc, what="find")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(
                    exc, query=f"filter={filter} projection={projection}", what="find"
                )
            budget.charge(len(docs))
            more = (
                f" More documents available: ask for page={page + 1}." if len(docs) == limit else ""
            )
            return f"{len(docs)} documents (page {page}, page_size {limit}).{more}\n{docs}"

        @tool
        def aggregate(collection: str, pipeline: str | list) -> str:
            """Runs a read-only aggregation pipeline (no $out/$merge).

            ``pipeline`` is a string containing a JSON list of stages. Prefer pushing work into
            the database with $match, $group, $project, $count instead of extracting and
            aggregating by hand. A safety $limit is appended to the end of the pipeline.
            """
            try:
                if isinstance(pipeline, list):
                    pipl = pipeline
                else:
                    pipl = tools.parse_json(pipeline, what="pipeline") or []
                stages = tools.ensure_read_only_pipeline(pipl)
                capped = [*stages, {"$limit": guardrails.hard_max_rows}]
                with self._db(conn, guardrails) as db:
                    docs = list(db[collection].aggregate(capped, maxTimeMS=max_time_ms))
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error → feedback to the agent
                return format_query_error(exc, query=str(pipeline), what="query")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=str(pipeline), what="pipeline")
            budget.charge(len(docs))
            return (
                f"{len(docs)} documents from the aggregation "
                f"(limit {guardrails.hard_max_rows}).\n{docs}"
            )

        @tool
        def materialize_aggregate(
            runtime: ToolRuntime,
            collection: str,
            pipeline: str | list,
            fmt: str = "csv",
            filename: str | None = None,
        ) -> Command | str:
            """Runs an aggregation pipeline (a string containing a JSON list of stages) and
            saves the result to file (Parquet/CSV).

            Returns ONLY metadata: path, columns, preview and statistics. Use it for analysis
            or charts on large volumes. Nested values are serialized.
            """
            if backend is None:
                return "Cannot write to file: no filesystem backend is configured."

            try:
                if isinstance(pipeline, list):
                    pipl = pipeline
                else:
                    pipl = tools.parse_json(pipeline, what="pipeline") or []
                stages = tools.ensure_read_only_pipeline(pipl)
                capped = [*stages, {"$limit": guardrails.hard_max_rows}]
                with self._db(conn, guardrails) as db:
                    docs = list(db[collection].aggregate(capped, maxTimeMS=max_time_ms))
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error → feedback to the agent
                return format_query_error(exc, query=str(pipeline), what="query")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=str(pipeline), what="pipeline")
            budget.charge(len(docs))
            columns, rows = tools.docs_to_table(docs)
            result = materialize_result(columns, rows, fmt=fmt, filename=filename, backend=backend)
            return write_command(result.to_summary(), runtime.tool_call_id, result.files_update)

        tools_list = [
            list_collections,
            describe_collection,
            count_documents,
            sample_documents,
            find,
            aggregate,
        ]
        if materialize_enable:
            tools_list.append(materialize_aggregate)
        return tools_list
