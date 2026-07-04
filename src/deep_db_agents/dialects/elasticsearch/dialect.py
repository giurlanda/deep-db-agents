"""Complete Elasticsearch dialect (official ``elasticsearch`` driver).

Reuses the entire tool logic from :mod:`..search_base` (shared by Elasticsearch and
OpenSearch); this module only provides the driver-specific connection opening.
"""

from __future__ import annotations

from typing import Any

from ...connection import ConnectionConfig
from ...guardrails import GuardrailConfig
from ...registry import register
from ..search_base import SearchDialect
from . import tools
from .prompt import ELASTICSEARCH_SYSTEM_PROMPT


@register("elasticsearch")
class ElasticsearchDialect(SearchDialect):
    """Agent specialized on Elasticsearch.

    Access is restricted to the index(es) configured in ``credential["index"]``
    (single name, CSV, or a ``*`` pattern); every tool inherited from
    :class:`SearchDialect` validates the requested index against this scope before
    querying the cluster.
    """

    schemes = ("elasticsearch",)

    def system_prompt(self) -> str:
        """Return the Elasticsearch-specific system prompt.

        Returns:
            str: The system prompt text for the Elasticsearch agent.
        """
        return ELASTICSEARCH_SYSTEM_PROMPT

    def _connect(self, conn: ConnectionConfig, guardrails: GuardrailConfig | None = None) -> Any:
        """Open the driver connection using the given configuration and guardrails.

        Args:
            conn: Connection configuration (host, port, credentials) for the cluster.
            guardrails: Optional guardrail configuration; its ``query_timeout_s``, if
                set, is forwarded as the request timeout for the driver client.

        Returns:
            Any: An initialized Elasticsearch client instance.
        """
        timeout = guardrails.query_timeout_s if guardrails else None
        return tools.connect(conn, request_timeout=timeout)
