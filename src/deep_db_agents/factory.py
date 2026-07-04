"""Factory functions: the entry points of the library.

Exposes ``create_deep_db_agents``, ``create_db_agent`` and ``create_deep_db_multi_agent``,
which turn a database URL into a ready-to-use LangChain / Deep Agent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deepagents import CompiledSubAgent, create_deep_agent
from langchain.agents import create_agent

from . import dialects as _dialects  # noqa: F401  (populates the dialect registry)
from . import registry
from .connection import ConnectionConfig
from .exceptions import InvalidMultiAgentConfigError
from .guardrails import GuardrailConfig
from .observability import SessionMetrics
from .prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from .url import parse_db_url

_USER_INSTRUCTIONS_HEADER = "\n\n# Database-specific instructions\n"
_ORCHESTRATOR_ROSTER_HEADER = "\n\n# Available database agents\n"
_ORCHESTRATOR_INSTRUCTIONS_HEADER = "\n\n# Additional context\n"


def _build_dialect_parts(
    db_url: str,
    credential: dict[str, Any] | None,
    system: str,
    guardrails: GuardrailConfig | None,
    *,
    materialize_enable: bool,
    metrics: SessionMetrics | None = None,
) -> tuple[list[Any], str]:
    """Shared logic for the factory functions: URL -> dialect -> (tools, prompt).

    Args:
        db_url: Database connection URL, e.g. ``<scheme>://<host>:<port>``.
        credential: Access credentials forwarded to the dialect's connection config.
        system: User-provided system prompt, appended to the dialect's own prompt.
        guardrails: Safety thresholds for the generated tools, or ``None`` for defaults.
        materialize_enable: Whether to expose the ``materialize_*`` tools (require a
            filesystem backend, so only enabled for Deep Agents).
        metrics: Optional ``SessionMetrics`` to attach to the dialect so its tools update
            session counters.

    Returns:
        A ``(db_tools, prompt)`` tuple: the dialect's tools (credentials captured in their
        closures) and the system prompt (dialect prompt + user instructions, if any).
    """
    parsed = parse_db_url(db_url)
    dialect = registry.resolve(parsed.scheme)()
    if metrics is not None:
        dialect._metrics = metrics

    conn = ConnectionConfig(
        scheme=parsed.scheme,
        host=parsed.host,
        port=parsed.port,
        credential=dict(credential or {}),
        path=parsed.path,
    )
    db_tools = dialect.build_tools(
        conn, guardrails or GuardrailConfig(), materialize_enable=materialize_enable
    )

    prompt = dialect.system_prompt()
    if system:
        prompt += _USER_INSTRUCTIONS_HEADER + system
    return db_tools, prompt


def create_deep_db_agents(
    db_url: str,
    credential: dict[str, Any] | None = None,
    system: str = "",
    *,
    guardrails: GuardrailConfig | None = None,
    enable_code_interpreter: bool = False,
    metrics: SessionMetrics | None = None,
    **kwargs: Any,
):
    """Create a Deep Agent specialized on the database pointed to by ``db_url``.

    Args:
        db_url: Database URL in the form ``<scheme>://<host>:<port>``. The scheme
            (``mysql``, ``postgres``, ``mongodb``, ``neo4j``, ...) selects the dialect.
        credential: Access credentials; the expected keys depend on the target database
            (e.g. ``{"user": ..., "password": ...}`` or ``{"secret_key": ...}``).
        system: Database-specific system prompt, appended to the dialect's generic prompt.
        guardrails: Safety thresholds for the tools (max LIMIT, timeout, EXPLAIN threshold,
            row budget). Defaults to ``GuardrailConfig()`` when omitted.
        enable_code_interpreter: If ``True``, adds a ``CodeInterpreterMiddleware`` (requires
            the optional ``code-interpreter`` extra) that can call the generated DB tools.
        metrics: Optional ``SessionMetrics``; when given, the tools update its session
            counters (queries run, rows returned, blocks from estimation and budget),
            readable after the call.
        **kwargs: Forwarded as-is to ``create_deep_agent`` (``model``, ``subagents``,
            ``checkpointer``, ...). Any ``tools`` passed here are merged with the dialect's
            tools.

    Returns:
        The compiled agent, with an ``agent.invoke({"messages": [...]}, config=...)``
        interface. Also supports ``await agent.ainvoke(...)``: tools are synchronous, but
        under ``ainvoke`` LangChain runs them in a thread pool, dispatching a turn's tool
        calls concurrently (see ``examples/async_quickstart.py``).

    Example:
        ```python
        from deep_db_agents import create_deep_db_agents

        agent = create_deep_db_agents(
            "postgres://localhost:5432/mydb",
            credential={"user": "reader", "password": "secret"},
            system="Focus on the `orders` and `customers` tables.",
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "How many orders last week?"}]}
        )
        ```
    """
    db_tools, prompt = _build_dialect_parts(
        db_url, credential, system, guardrails, materialize_enable=True, metrics=metrics
    )
    user_tools = kwargs.pop("tools", []) or []

    if enable_code_interpreter:
        # Lazy import: langchain-quickjs is an optional extra (``code-interpreter``).
        from langchain_quickjs import CodeInterpreterMiddleware

        code_interpreter = CodeInterpreterMiddleware(ptc=[*db_tools, *user_tools])
        # Accepts any middleware sequence (list, tuple...) without overwriting it.
        existing = list(kwargs.pop("middleware", []) or [])
        kwargs["middleware"] = [*existing, code_interpreter]

    return create_deep_agent(
        tools=[*db_tools, *user_tools],
        system_prompt=prompt,
        **kwargs,
    )


def create_db_agent(
    db_url: str,
    credential: dict[str, Any] | None = None,
    system: str = "",
    *,
    guardrails: GuardrailConfig | None = None,
    metrics: SessionMetrics | None = None,
    **kwargs: Any,
):
    """Create a plain LangChain Agent specialized on the database pointed to by ``db_url``.

    Unlike :func:`create_deep_db_agents`, the file-materialization tools (``materialize_*``)
    are not exposed: they require the deepagents filesystem backend, which the plain agent
    does not have.

    Args:
        db_url: Database URL in the form ``<scheme>://<host>:<port>``. The scheme
            (``mysql``, ``postgres``, ``mongodb``, ``neo4j``, ...) selects the dialect.
        credential: Access credentials; the expected keys depend on the target database
            (e.g. ``{"user": ..., "password": ...}`` or ``{"secret_key": ...}``).
        system: Database-specific system prompt, appended to the dialect's generic prompt.
        guardrails: Safety thresholds for the tools (max LIMIT, timeout, EXPLAIN threshold,
            row budget). Defaults to ``GuardrailConfig()`` when omitted.
        metrics: Optional ``SessionMetrics``; when given, the tools update its session
            counters (queries run, rows returned, blocks from estimation and budget),
            readable after the call.
        **kwargs: Forwarded as-is to ``create_agent`` (``model``, ``checkpointer``, ...).
            Any ``tools`` passed here are merged with the dialect's tools.

    Returns:
        The compiled agent, with an ``agent.invoke({"messages": [...]}, config=...)``
        interface. Also supports ``await agent.ainvoke(...)`` (synchronous tools run in a
        thread pool).

    Example:
        ```python
        from deep_db_agents import create_db_agent

        agent = create_db_agent(
            "sqlite:///./data/app.db",
            system="Answer briefly, cite the exact table and column names.",
        )
        result = agent.invoke({"messages": [{"role": "user", "content": "List all tables."}]})
        ```
    """
    # Unlike create_deep_db_agents, materialization tools stay excluded: the plain
    # LangChain agent has no deepagents filesystem/backend to save them to.
    db_tools, prompt = _build_dialect_parts(
        db_url, credential, system, guardrails, materialize_enable=False, metrics=metrics
    )

    user_tools = kwargs.pop("tools", []) or []
    return create_agent(
        tools=[*db_tools, *user_tools],
        system_prompt=prompt,
        **kwargs,
    )


def create_deep_db_multi_agent(
    db_agents: Mapping[str, Mapping[str, Any]],
    system: str = "",
    **kwargs: Any,
):
    """Create a Deep Agent orchestrator that coordinates multiple ``deep_db_agents``.

    The orchestrator never queries a database directly: it delegates each sub-question to
    the sub-agent specialized on the relevant database (exposed through the ``task`` tool)
    and combines the results. This enables answering questions that span multiple databases.

    Args:
        db_agents: Mapping of ``name -> {"description": ..., "agent": ...}``. ``description``
            is a short description of the sub-agent and its database (the orchestrator uses
            it to decide whom to delegate to); ``agent`` is an already-compiled agent from
            :func:`create_deep_db_agents`.
        system: Extra system prompt, appended to the orchestrator's generic prompt and the
            sub-agent roster, to provide additional context (domain, join rules, ...).
        **kwargs: Forwarded as-is to ``create_deep_agent`` (``model``, ``middleware``,
            ``checkpointer``, ...). Any ``subagents`` passed here are merged with the ones
            derived from ``db_agents``.

    Returns:
        The compiled orchestrator agent, with an ``agent.invoke({"messages": [...]})``
        interface.

    Raises:
        InvalidMultiAgentConfigError: If ``db_agents`` is empty, an entry is missing the
            ``description``/``agent`` keys, or ``agent`` is not a compiled agent (no
            ``.invoke``).

    Example:
        ```python
        from deep_db_agents import create_deep_db_agents, create_deep_db_multi_agent

        orders_agent = create_deep_db_agents("postgres://localhost:5432/orders")
        events_agent = create_deep_db_agents("mongodb://localhost:27017/events")

        orchestrator = create_deep_db_multi_agent(
            {
                "orders": {
                    "description": "Orders and customers (Postgres)",
                    "agent": orders_agent,
                },
                "events": {"description": "Raw event log (MongoDB)", "agent": events_agent},
            }
        )
        query = "Compare orders vs. events last week."
        result = orchestrator.invoke({"messages": [{"role": "user", "content": query}]})
        ```
    """
    if not db_agents:
        raise InvalidMultiAgentConfigError("db_agents must not be empty.")

    subagents: list[CompiledSubAgent] = []
    roster_lines: list[str] = []
    for name, spec in db_agents.items():
        if not isinstance(spec, Mapping) or "description" not in spec or "agent" not in spec:
            raise InvalidMultiAgentConfigError(
                f"db_agents[{name!r}] must be a dict with the 'description' and 'agent' keys."
            )
        if not callable(getattr(spec["agent"], "invoke", None)):
            raise InvalidMultiAgentConfigError(
                f"db_agents[{name!r}]['agent'] must be a compiled agent (with .invoke), "
                f"got {type(spec['agent']).__name__!r}."
            )
        description = spec["description"]
        # CompiledSubAgent: the agent is already a compiled runnable, used as-is.
        subagents.append(
            CompiledSubAgent(name=name, description=description, runnable=spec["agent"])
        )
        roster_lines.append(f"- `{name}`: {description}")

    prompt = ORCHESTRATOR_SYSTEM_PROMPT + _ORCHESTRATOR_ROSTER_HEADER + "\n".join(roster_lines)
    if system:
        prompt += _ORCHESTRATOR_INSTRUCTIONS_HEADER + system

    extra_subagents = kwargs.pop("subagents", []) or []
    return create_deep_agent(
        system_prompt=prompt,
        subagents=[*subagents, *extra_subagents],
        **kwargs,
    )
