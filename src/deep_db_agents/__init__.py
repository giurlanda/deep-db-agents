"""deep-db-agents: factory for Deep Agents specialized on different databases."""

from __future__ import annotations

from .connection import ConnectionConfig
from .exceptions import (
    DeepDbAgentError,
    EstimateExceededError,
    GuardrailError,
    InvalidDbUrlError,
    InvalidMultiAgentConfigError,
    QueryNotAllowedError,
    UnsupportedSchemeError,
)
from .factory import create_db_agents, create_deep_db_agents, create_deep_db_multi_agents
from .guardrails import GuardrailConfig
from .observability import SessionMetrics, configure_logging
from .registry import available_schemes

__all__ = [
    "create_deep_db_agents",
    "create_deep_db_multi_agents",
    "create_db_agents",
    "GuardrailConfig",
    "ConnectionConfig",
    "SessionMetrics",
    "configure_logging",
    "available_schemes",
    "DeepDbAgentError",
    "InvalidDbUrlError",
    "UnsupportedSchemeError",
    "InvalidMultiAgentConfigError",
    "QueryNotAllowedError",
    "GuardrailError",
    "EstimateExceededError",
]

__version__ = "0.2.0"
