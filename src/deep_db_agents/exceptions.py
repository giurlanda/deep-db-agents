"""Exceptions raised by the deep-db-agents library."""

from __future__ import annotations


class DeepDbAgentError(Exception):
    """Base class for all exceptions raised by the library."""


class InvalidDbUrlError(DeepDbAgentError):
    """The database URL is not in the ``<scheme>://<host>:<port>`` format."""


class UnsupportedSchemeError(DeepDbAgentError):
    """No dialect is registered for the requested scheme."""

    def __init__(self, scheme: str, available: list[str]):
        """Initialize the error with the offending scheme and the available ones.

        Args:
            scheme: The unsupported URL scheme that was requested.
            available: The list of currently registered schemes.
        """
        self.scheme = scheme
        self.available = available
        super().__init__(
            f"Unsupported database scheme: {scheme!r}. "
            f"Available schemes: {', '.join(sorted(available)) or '(none)'}."
        )


class InvalidMultiAgentConfigError(DeepDbAgentError):
    """The ``db_agents`` configuration for the multi-database orchestrator is invalid."""


class QueryNotAllowedError(DeepDbAgentError):
    """The query violates the whitelist of allowed operations (e.g. it is not a SELECT)."""


class GuardrailError(DeepDbAgentError):
    """A hard guardrail blocked execution (e.g. EXPLAIN threshold or budget exceeded)."""


class EstimateExceededError(GuardrailError):
    """The EXPLAIN row estimate exceeded the configured threshold.

    Unlike other :class:`GuardrailError` cases (e.g. the session row budget), the query
    tools catch this and turn it into corrective feedback for the agent instead of
    interrupting the turn: the query is still **not executed**, but the agent is told to
    refine its filters or aggregate and retry.
    """
