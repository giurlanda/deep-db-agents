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
