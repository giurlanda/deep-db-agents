"""Abstract interface that every database dialect must implement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool

from .connection import ConnectionConfig
from .guardrails import GuardrailConfig
from .observability import SessionMetrics


class DbDialect(ABC):
    """Specialization of the agent for a database type.

    A dialect provides two things: the **generic system prompt** that instructs the
    agent on how to operate efficiently on that database, and the set of **tools**
    (with credentials injected) to interact with it.
    """

    #: URL schemes handled by this dialect (e.g. ``("postgres", "postgresql")``).
    schemes: tuple[str, ...] = ()

    #: Optional session counters, injected by the factory; tools update them if present.
    _metrics: SessionMetrics | None = None

    @abstractmethod
    def system_prompt(self) -> str:
        """Return the generic prompt for this database, concatenated with the user's own.

        Returns:
            str: The dialect-specific system prompt text.
        """

    @abstractmethod
    def build_tools(
        self,
        conn: ConnectionConfig,
        guardrails: GuardrailConfig,
        materialize_enable: bool = False,
        backend: BackendProtocol | None = None,
    ) -> Sequence[BaseTool]:
        """Build the LangChain tools bound to the connection and guardrails.

        Args:
            conn: Connection parameters (host/port/credentials or file path).
            guardrails: Hard safety thresholds enforced by the tool wrappers.
            materialize_enable: Whether to expose the tool(s) that materialize large
                results to file instead of returning them inline.
            backend: Filesystem backend injected into the file-writing tools' closures.
                When ``None``, those tools report that they cannot write to file because
                no backend is configured.

        Returns:
            Sequence[BaseTool]: The tools built for this dialect, with credentials
            captured in their closures.
        """
