from __future__ import annotations

import pytest

from deep_db_agents.exceptions import EstimateExceededError, GuardrailError
from deep_db_agents.guardrails import GuardrailConfig, SessionBudget


def test_clamp_limit_applies_default_when_missing():
    g = GuardrailConfig(default_rows=100, hard_max_rows=1000)
    assert g.clamp_limit(None) == 100
    assert g.clamp_limit(0) == 100


def test_clamp_limit_caps_at_hard_max():
    g = GuardrailConfig(default_rows=100, hard_max_rows=1000)
    assert g.clamp_limit(50) == 50
    assert g.clamp_limit(99999) == 1000


def test_max_materialized_bytes_defaults_to_10_mib():
    # Il limite in byte dei file materializzati è finito di default (10 MiB) e configurabile.
    assert GuardrailConfig().max_materialized_bytes == 10 * 1024 * 1024
    assert GuardrailConfig(max_materialized_bytes=1024).max_materialized_bytes == 1024


def test_check_estimate_blocks_over_threshold():
    g = GuardrailConfig(explain_row_threshold=1000)
    g.check_estimate(999)  # ok
    with pytest.raises(EstimateExceededError):
        g.check_estimate(1001)


def test_estimate_exceeded_is_a_guardrail_error():
    # Subclass relationship: check_estimate stays a GuardrailError while being catchable
    # on its own so the tools can turn it into feedback (the session budget stays hard).
    assert issubclass(EstimateExceededError, GuardrailError)


def test_session_budget_enforced():
    budget = SessionBudget(budget=150)
    budget.charge(100)
    with pytest.raises(GuardrailError):
        budget.charge(100)


def test_session_budget_unlimited_when_none():
    budget = SessionBudget(budget=None)
    budget.charge(10_000_000)  # nessuna eccezione
