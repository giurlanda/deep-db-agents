"""Observability: structured logging and guardrail session counters.

Two independent tools:

- **Logging** under the ``deep_db_agents`` namespace (one logger per area). The library
  does not configure its own handlers (following the convention that configuration is
  the application's responsibility); :func:`configure_logging` is a convenience to
  enable it quickly.
- **Counters** :class:`SessionMetrics`: a thread-safe object the application can pass to
  the factory to read, at the end of a session, how many queries were run/blocked, how
  many rows were returned, etc. It is optional and must be created by the caller (one
  per agent/session).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

LOGGER_NAMESPACE = "deep_db_agents"


def get_logger(area: str) -> logging.Logger:
    """Return a logger for a library area.

    Args:
        area: Name of the library area (e.g. ``"guardrails"``, ``"query_errors"``).

    Returns:
        logging.Logger: A logger named ``deep_db_agents.<area>``.
    """
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{area}")


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a ``stderr`` handler to the library logger (convenience, idempotent).

    Args:
        level: Logging level to set on the library logger. Defaults to
            ``logging.INFO``.

    Returns:
        None.
    """
    logger = logging.getLogger(LOGGER_NAMESPACE)
    if not any(getattr(h, "_deep_db_agents", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler._deep_db_agents = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(level)


@dataclass
class SessionMetrics:
    """Cumulative counters for a session/agent, updated by the guardrails.

    Thread-safe: tool calls may run in parallel. Pass an instance to the factory
    (``metrics`` parameter) and read it after invoking the agent.

    Attributes:
        queries_run: Total number of queries executed.
        rows_returned: Total number of rows returned across all queries.
        budget_exhausted: Number of times the session row budget was exhausted.
        estimate_blocked: Number of times a query was blocked by the EXPLAIN
            row-estimate threshold.
    """

    queries_run: int = 0
    rows_returned: int = 0
    budget_exhausted: int = 0
    estimate_blocked: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_query(self, rows: int) -> None:
        """Record that a query ran and returned ``rows`` rows.

        Args:
            rows: Number of rows returned by the query.

        Returns:
            None.
        """
        with self._lock:
            self.queries_run += 1
            self.rows_returned += rows

    def record_budget_exhausted(self) -> None:
        """Record that the session row budget was exhausted.

        Returns:
            None.
        """
        with self._lock:
            self.budget_exhausted += 1

    def record_estimate_blocked(self) -> None:
        """Record that a query was blocked by the EXPLAIN row-estimate threshold.

        Returns:
            None.
        """
        with self._lock:
            self.estimate_blocked += 1

    def summary(self) -> str:
        """Build a human-readable summary of the session counters.

        Returns:
            str: A one-line summary of queries run, rows returned, and blocks.
        """
        return (
            f"queries run={self.queries_run}, rows returned={self.rows_returned:,}, "
            f"blocked by estimate={self.estimate_blocked}, "
            f"budget exhausted={self.budget_exhausted}"
        )
