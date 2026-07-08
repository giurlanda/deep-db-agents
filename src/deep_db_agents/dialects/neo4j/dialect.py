"""Full Neo4j dialect (official ``neo4j`` driver).

Carries the context-management principles over to the graph: exploration of labels,
relationship types and property keys, counting before extraction, execution of read-only
Cypher with a consumer-side forced limit, and file materialization of large results.
Queries run in read transactions (``execute_read``), which prevent server-side writes on
top of the clause whitelist.
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
from .prompt import NEO4J_SYSTEM_PROMPT


def _collect(tx, cypher: str, limit: int) -> tuple[list[str], list[dict], bool]:
    """Runs the Cypher query and collects at most ``limit`` records (consumer-side cap).

    Args:
        tx: The active Neo4j transaction.
        cypher: The Cypher statement to run.
        limit: Maximum number of records to collect.

    Returns:
        A tuple (result keys, collected records, whether more records were available).
    """
    result = tx.run(cypher)
    keys = list(result.keys())
    records: list[dict] = []
    more = False
    for i, record in enumerate(result):
        if i >= limit:
            more = True
            break
        records.append(record.data())
    return keys, records, more


def _estimate(tx, cypher: str) -> int:
    """Estimates the row count via ``EXPLAIN``, reading ``EstimatedRows`` from the plan
    (best-effort).

    Args:
        tx: The active Neo4j transaction.
        cypher: The Cypher statement to estimate.

    Returns:
        The estimated row count, or 0 if the estimate could not be obtained.
    """
    try:
        summary = tx.run(f"EXPLAIN {cypher}").consume()
        plan = summary.plan or {}
        return int(plan.get("args", {}).get("EstimatedRows", 0))
    except Exception:  # noqa: BLE001 - the estimate is best-effort, never blocking on its own
        return 0


@register("neo4j")
class Neo4jDialect(DbDialect):
    """Agent specialized on Neo4j."""

    schemes = ("neo4j",)

    def __init__(self) -> None:
        # The Neo4j Driver is a thread-safe pool: reuse it for the agent's whole lifetime,
        # opening a session (not thread-safe, but cheap) per individual tool call.
        self._driver_cache = LazyClient()

    def system_prompt(self) -> str:
        return NEO4J_SYSTEM_PROMPT

    def close(self) -> None:
        """Closes the reused Neo4j Driver. Call at the end of the agent's lifetime."""
        self._driver_cache.close()

    # Driver-specific hook, overridable in tests.
    @contextmanager
    def _session(self, conn: ConnectionConfig, guardrails: GuardrailConfig | None = None):
        # Client-side network timeout (pool wait + total retry time) consistent with
        # query_timeout_s, so execute_read cannot hang indefinitely.
        network_timeout_s = guardrails.query_timeout_s if guardrails else None
        driver = self._driver_cache.get(
            lambda: tools.connect(conn, network_timeout_s=network_timeout_s)
        )
        database = conn.database or "neo4j"
        with driver.session(database=database) as session:
            yield session

    def _read(self, conn: ConnectionConfig, guardrails: GuardrailConfig | None, work, *args):
        # The server-side timeout for managed transactions must be set with
        # ``neo4j.unit_of_work``, not by wrapping the Cypher in a ``neo4j.Query``: the latter
        # is only accepted by ``session.run``, not by ``tx.run`` inside
        # execute_read/execute_write (the driver raises
        # ``TypeError: Query object is only supported for session.run``).
        timeout_s = guardrails.query_timeout_s if guardrails else None
        if timeout_s and timeout_s > 0:
            import neo4j

            work = neo4j.unit_of_work(timeout=float(timeout_s))(work)
        with self._session(conn, guardrails) as session:
            return session.execute_read(work, *args)

    def build_tools(
        self,
        conn: ConnectionConfig,
        guardrails: GuardrailConfig,
        materialize_enable: bool = False,
        backend: BackendProtocol | None = None,
    ) -> Sequence[BaseTool]:
        metrics = getattr(self, "_metrics", None)
        budget = SessionBudget(guardrails.row_budget, metrics=metrics)

        @tool
        def list_labels() -> str:
            """Lists the node labels present in the graph."""
            try:
                _, records, _ = self._read(conn, guardrails, _collect, "CALL db.labels()", 1000)
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, what="label list")
            labels = [r.get("label") for r in records]
            return "Labels: " + (", ".join(labels) if labels else "(none)")

        @tool
        def list_relationship_types() -> str:
            """Lists the relationship types present in the graph."""
            try:
                _, records, _ = self._read(
                    conn, guardrails, _collect, "CALL db.relationshipTypes()", 1000
                )
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, what="relationship type list")
            types = [r.get("relationshipType") for r in records]
            return "Relationship types: " + (", ".join(types) if types else "(none)")

        @tool
        def schema() -> str:
            """Reports the graph's labels, relationship types and property keys."""
            try:
                _, labels, _ = self._read(conn, guardrails, _collect, "CALL db.labels()", 1000)
                _, rels, _ = self._read(
                    conn, guardrails, _collect, "CALL db.relationshipTypes()", 1000
                )
                _, props, _ = self._read(conn, guardrails, _collect, "CALL db.propertyKeys()", 1000)
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, what="graph schema")
            return (
                "Labels: " + ", ".join(r.get("label") for r in labels) + "\n"
                "Relationship types: " + ", ".join(r.get("relationshipType") for r in rels) + "\n"
                "Property keys: " + ", ".join(r.get("propertyKey") for r in props)
            )

        @tool
        def count_nodes(label: str | None = None) -> str:
            """Counts the graph's nodes, optionally filtering by label.

            Use it BEFORE extracting subgraphs, to assess the volume (explore before
            extracting).
            """
            pattern = f"(n:{tools.quote_label(label)})" if label else "(n)"
            cypher = f"MATCH {pattern} RETURN count(n) AS c"
            try:
                _, records, _ = self._read(conn, guardrails, _collect, cypher, 1)
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=cypher, what="Cypher query")
            total = records[0]["c"] if records else 0
            return f"Nodes{f' :{label}' if label else ''}: {total:,}"

        @tool
        def run_cypher(cypher: str, page_size: int | None = None) -> str:
            """Runs a read-only Cypher query with a forced row limit.

            Only read queries (MATCH/RETURN/WITH/CALL ... YIELD) are allowed: write clauses
            are rejected. The number of rows returned is capped at a ceiling that cannot be
            bypassed; prefer RETURN of only the properties you need plus aggregations.
            """
            try:
                stmt = tools.ensure_read_only(cypher)
                limit = guardrails.clamp_limit(page_size)
                est = self._read(conn, guardrails, _estimate, stmt)
                guardrails.check_estimate(est, metrics)
                keys, records, more = self._read(conn, guardrails, _collect, stmt, limit)
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error → feedback to the agent
                return format_query_error(exc, query=cypher, what="cypher")
            except EstimateExceededError as exc:  # noqa: BLE001 - estimate too high → feedback to the agent
                return format_estimate_block(exc, what="Cypher query")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=cypher, what="Cypher query")
            budget.charge(len(records))
            extra = " More rows available: add LIMIT/SKIP or refine the query." if more else ""
            return f"{len(records)} rows (limit {limit}).{extra}\ncolumns={keys}\n{records}"

        @tool
        def materialize_cypher(
            runtime: ToolRuntime, cypher: str, fmt: str = "csv", filename: str | None = None
        ) -> Command | str:
            """Runs a read-only Cypher query and saves the result to file (Parquet/CSV).

            Returns ONLY metadata: path, columns, preview and statistics. Use it for analysis
            or charts on large volumes. Nodes/relationships are serialized.
            """
            if backend is None:
                return "Cannot write to file: no filesystem backend is configured."
            try:
                stmt = tools.ensure_read_only(cypher)
                keys, records, _ = self._read(
                    conn, guardrails, _collect, stmt, guardrails.hard_max_rows
                )
            except QueryNotAllowedError as exc:  # noqa: BLE001 - query parsing error → feedback to the agent
                return format_query_error(exc, query=cypher, what="cypher")
            except (DeepDbAgentError, ImportError):
                raise
            except Exception as exc:  # noqa: BLE001 - driver error → feedback to the agent
                return format_query_error(exc, query=cypher, what="Cypher query")
            budget.charge(len(records))
            columns, rows = tools.records_to_table(keys, records)
            result = materialize_result(columns, rows, fmt=fmt, filename=filename, backend=backend)
            return write_command(result.to_summary(), runtime.tool_call_id, result.files_update)

        tool_list = [
            list_labels,
            list_relationship_types,
            schema,
            count_nodes,
            run_cypher,
        ]
        if materialize_enable:
            tool_list.append(materialize_cypher)
        return tool_list
