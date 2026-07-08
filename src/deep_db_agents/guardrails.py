"""Hard guardrails enforced by the tool wrapper (not by the agent).

The defense hierarchy encoded here: limit and paginate, explore before extracting,
estimate cost via EXPLAIN, allow only whitelisted operations, enforce a per-session
row budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .exceptions import EstimateExceededError, GuardrailError
from .observability import SessionMetrics, get_logger

_logger = get_logger("guardrails")


@dataclass
class GuardrailConfig:
    """Safety thresholds that cannot be bypassed by the agent.

    Attributes:
        default_rows: LIMIT applied automatically when the agent does not specify one.
        hard_max_rows: Hard cap on rows per single query, never exceedable.
        explain_row_threshold: If the row estimate from ``EXPLAIN`` exceeds this
            threshold, the query is blocked and the agent is asked to refine its
            filters or aggregate instead.
        query_timeout_s: Maximum execution timeout for a query, enforced by the
            driver/database.
        allowed_statements: Whitelist of allowed statement types (only ``SELECT`` by
            default).
        row_budget: Cumulative row budget returned per session (``None`` = unlimited).
    """

    default_rows: int = 100
    hard_max_rows: int = 1000
    explain_row_threshold: int = 1_000_000
    query_timeout_s: int = 30
    allowed_statements: frozenset[str] = frozenset({"SELECT"})
    #: Cumulative row budget per session. Finite by default to avoid unbounded chained
    #: extractions; pass ``None`` to explicitly disable it.
    row_budget: int | None = 50_000

    def clamp_limit(self, requested: int | None) -> int:
        """Compute the effective LIMIT to apply, capped by ``hard_max_rows``.

        Args:
            requested: The row limit requested by the agent, or ``None``/non-positive
                to fall back to ``default_rows``.

        Returns:
            int: The effective limit, never greater than ``hard_max_rows``.
        """
        if requested is None or requested <= 0:
            requested = self.default_rows
        return min(requested, self.hard_max_rows)

    def check_estimate(self, estimated_rows: int, metrics: SessionMetrics | None = None) -> None:
        """Block the query if the EXPLAIN estimate exceeds the threshold.

        Args:
            estimated_rows: The estimated number of rows the query would return,
                typically obtained via ``EXPLAIN``.
            metrics: Optional session counters to update when the query is blocked.

        Raises:
            EstimateExceededError: If ``estimated_rows`` exceeds ``explain_row_threshold``.
                A subclass of ``GuardrailError`` that the query tools reflect back to the
                agent as corrective feedback instead of interrupting the turn.
        """
        if estimated_rows > self.explain_row_threshold:
            if metrics is not None:
                metrics.record_estimate_blocked()
            _logger.warning(
                "query blocked: estimate ~%d rows exceeds threshold %d",
                estimated_rows,
                self.explain_row_threshold,
            )
            raise EstimateExceededError(
                f"Query blocked: ~{estimated_rows:,} estimated rows exceed the "
                f"threshold of {self.explain_row_threshold:,}. Refine your filters "
                "or use aggregation."
            )


@dataclass
class SessionBudget:
    """Tracks row consumption in a session and enforces its budget.

    If a ``SessionMetrics`` is associated, session counters are updated as well.

    Attributes:
        budget: Maximum cumulative rows allowed for the session, or ``None`` for
            unlimited.
        metrics: Optional session counters updated alongside the budget.
        consumed: Rows consumed so far in the session (not set at init).
    """

    budget: int | None = None
    metrics: SessionMetrics | None = None
    consumed: int = field(default=0, init=False)

    def charge(self, rows: int) -> None:
        """Charge ``rows`` against the session budget.

        Args:
            rows: Number of rows just returned, to add to the consumed total.

        Raises:
            GuardrailError: If the cumulative consumed rows exceed ``budget``.
        """
        if self.metrics is not None:
            self.metrics.record_query(rows)
        if self.budget is None:
            return
        self.consumed += rows
        if self.consumed > self.budget:
            if self.metrics is not None:
                self.metrics.record_budget_exhausted()
            _logger.warning("session row budget exhausted: %d/%d", self.consumed, self.budget)
            raise GuardrailError(
                f"Session row budget exhausted "
                f"({self.consumed:,}/{self.budget:,}). Start a new session or aggregate more."
            )
