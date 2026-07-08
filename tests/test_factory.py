from __future__ import annotations

import pytest

import deep_db_agents.factory as factory_mod
from deep_db_agents import create_deep_db_agents, create_deep_db_multi_agents
from deep_db_agents.base import DbDialect
from deep_db_agents.dialects.mysql.prompt import MYSQL_SYSTEM_PROMPT
from deep_db_agents.exceptions import InvalidMultiAgentConfigError, UnsupportedSchemeError
from deep_db_agents.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from deep_db_agents.registry import _REGISTRY, register


class FakeAgent:
    """Stub di agente compilato: basta un ``.invoke`` per passare la validazione."""

    def invoke(self, *args, **kwargs):  # noqa: ARG002
        return None


@pytest.fixture
def captured(monkeypatch):
    """Sostituisce create_deep_agent e cattura gli argomenti con cui è chiamato."""
    calls = {}

    def fake_create_deep_agent(**kwargs):
        calls.update(kwargs)
        return "AGENT"

    monkeypatch.setattr(factory_mod, "create_deep_agent", fake_create_deep_agent)
    return calls


def test_factory_builds_agent_for_mysql(captured):
    agent = create_deep_db_agents(
        "mysql://localhost:3306",
        {"user": "u", "password": "p", "database": "shop"},
        system="Il DB shop contiene ordini.",
        model="claude-sonnet-4-5-20250929",
    )
    assert agent == "AGENT"
    # I tool del dialect MySQL vengono inoltrati.
    tool_names = {t.name for t in captured["tools"]}
    assert {"list_tables", "count_rows", "run_query", "materialize_query"} <= tool_names
    # model e altri kwargs arrivano intatti.
    assert captured["model"] == "claude-sonnet-4-5-20250929"


def test_factory_concatenates_system_prompt(captured):
    create_deep_db_agents(
        "mysql://localhost:3306",
        {"user": "u", "password": "p"},
        system="ISTRUZIONI SPECIFICHE",
    )
    prompt = captured["system_prompt"]
    assert prompt.startswith(MYSQL_SYSTEM_PROMPT)
    assert "ISTRUZIONI SPECIFICHE" in prompt


def test_factory_merges_user_tools(captured):
    sentinel = object()
    create_deep_db_agents(
        "mysql://localhost:3306",
        {"user": "u"},
        tools=[sentinel],
    )
    assert sentinel in captured["tools"]


def test_factory_defaults_backend_to_state_backend(captured):
    from deepagents.backends.state import StateBackend

    create_deep_db_agents("mysql://localhost:3306", {"user": "u"})
    # A StateBackend is created by default and forwarded to create_deep_agent.
    assert isinstance(captured["backend"], StateBackend)


def test_factory_forwards_provided_backend_to_tools_and_agent(captured):
    from deepagents.backends.state import StateBackend

    backend = StateBackend()
    create_deep_db_agents("mysql://localhost:3306", {"user": "u"}, backend=backend)
    # The same instance reaches the agent (so its filesystem is where tools write).
    assert captured["backend"] is backend


def test_factory_unsupported_scheme(captured):
    with pytest.raises(UnsupportedSchemeError):
        create_deep_db_agents("oracle://host:1521", {})


def test_factory_stub_dialect_raises_not_implemented(captured):
    # Un dialect registrato ma con build_tools non implementato deve propagare
    # NotImplementedError attraverso la factory. (mongodb non è più uno stub,
    # quindi usiamo un dialect fittizio registrato solo per questo test.)
    @register("stubdb")
    class StubDialect(DbDialect):
        def system_prompt(self) -> str:
            return ""

        def build_tools(self, conn, guardrails, materialize_enable=False, backend=None):
            raise NotImplementedError

    try:
        with pytest.raises(NotImplementedError):
            create_deep_db_agents("stubdb://localhost:1234", {})
    finally:
        _REGISTRY.pop("stubdb", None)


def test_multi_agent_wraps_db_agents_as_compiled_subagents(captured):
    sales = FakeAgent()
    crm = FakeAgent()
    agent = create_deep_db_multi_agents(
        {
            "sales": {"description": "DB ordini su Postgres", "agent": sales},
            "crm": {"description": "DB clienti su MongoDB", "agent": crm},
        },
        model="claude-sonnet-4-5-20250929",
    )
    assert agent == "AGENT"
    subagents = {s["name"]: s for s in captured["subagents"]}
    assert subagents.keys() == {"sales", "crm"}
    # Ogni voce è un CompiledSubAgent: l'agente compilato finisce in 'runnable'.
    assert subagents["sales"]["runnable"] is sales
    assert subagents["sales"]["description"] == "DB ordini su Postgres"
    assert subagents["crm"]["runnable"] is crm
    # I kwargs (model, ...) sono inoltrati a create_deep_agent.
    assert captured["model"] == "claude-sonnet-4-5-20250929"


def test_multi_agent_defaults_backend_to_state_backend(captured):
    from deepagents.backends.state import StateBackend

    create_deep_db_multi_agents(
        {"sales": {"description": "DB ordini su Postgres", "agent": FakeAgent()}},
    )
    # The orchestrator owns no DB tools, but its own filesystem gets a default backend.
    assert isinstance(captured["backend"], StateBackend)


def test_multi_agent_prompt_has_orchestrator_roster_and_system(captured):
    create_deep_db_multi_agents(
        {"sales": {"description": "DB ordini su Postgres", "agent": FakeAgent()}},
        system="ISTRUZIONI ORCHESTRATORE",
    )
    prompt = captured["system_prompt"]
    assert prompt.startswith(ORCHESTRATOR_SYSTEM_PROMPT)
    # Il roster elenca nome + descrizione di ciascun sotto-agente.
    assert "`sales`" in prompt
    assert "DB ordini su Postgres" in prompt
    # Il system dell'utente è concatenato in coda.
    assert "ISTRUZIONI ORCHESTRATORE" in prompt


def test_multi_agent_merges_extra_subagents(captured):
    extra = {"name": "notes", "description": "ricerca note", "runnable": object()}
    create_deep_db_multi_agents(
        {"sales": {"description": "DB ordini", "agent": FakeAgent()}},
        subagents=[extra],
    )
    names = {s["name"] for s in captured["subagents"]}
    assert names == {"sales", "notes"}


def test_multi_agent_empty_raises(captured):
    with pytest.raises(InvalidMultiAgentConfigError):
        create_deep_db_multi_agents({})


def test_multi_agent_invalid_spec_raises(captured):
    with pytest.raises(InvalidMultiAgentConfigError):
        create_deep_db_multi_agents({"sales": {"agent": FakeAgent()}})  # manca 'description'


def test_multi_agent_rejects_non_runnable_agent(captured):
    # 'agent' deve essere un agente compilato (con .invoke), non un oggetto qualunque.
    with pytest.raises(InvalidMultiAgentConfigError):
        create_deep_db_multi_agents({"sales": {"description": "DB ordini", "agent": object()}})


def test_code_interpreter_preserves_existing_middleware(captured):
    # Un middleware passato dall'utente (anche come tupla) non viene sovrascritto
    # quando enable_code_interpreter aggiunge il CodeInterpreterMiddleware.
    pytest.importorskip("langchain_quickjs")
    sentinel = object()
    create_deep_db_agents(
        "mysql://localhost:3306",
        {"user": "u"},
        enable_code_interpreter=True,
        middleware=(sentinel,),
    )
    middleware = captured["middleware"]
    assert sentinel in middleware
    assert len(middleware) == 2  # sentinel + code interpreter, in lista
