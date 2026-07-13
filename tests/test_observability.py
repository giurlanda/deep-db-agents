from __future__ import annotations

import logging

import pytest

from deep_db_agents.exceptions import EstimateExceededError, RowBudgetExceededError
from deep_db_agents.guardrails import GuardrailConfig, SessionBudget
from deep_db_agents.observability import SessionMetrics, configure_logging


def test_session_budget_records_metrics():
    metrics = SessionMetrics()
    budget = SessionBudget(budget=None, metrics=metrics)
    budget.charge(3)
    budget.charge(5)
    assert metrics.queries_run == 2
    assert metrics.rows_returned == 8


def test_budget_exhausted_recorded():
    metrics = SessionMetrics()
    budget = SessionBudget(budget=4, metrics=metrics)
    with pytest.raises(RowBudgetExceededError):
        budget.charge(10)
    assert metrics.budget_exhausted == 1


def test_check_estimate_records_block():
    metrics = SessionMetrics()
    guardrails = GuardrailConfig(explain_row_threshold=10)
    with pytest.raises(EstimateExceededError):
        guardrails.check_estimate(1000, metrics)
    assert metrics.estimate_blocked == 1
    # Sotto soglia non incrementa.
    guardrails.check_estimate(5, metrics)
    assert metrics.estimate_blocked == 1


def test_metrics_summary_is_readable():
    metrics = SessionMetrics(queries_run=2, rows_returned=1234)
    assert "queries run=2" in metrics.summary()
    assert "1,234" in metrics.summary()


def test_query_error_is_logged(caplog):
    from deep_db_agents.query_errors import format_query_error

    with caplog.at_level(logging.WARNING, logger="deep_db_agents.query_errors"):
        format_query_error(RuntimeError("boom"), query="SELECT 1", what="query")
    assert any("boom" in r.message for r in caplog.records)


def test_configure_logging_is_idempotent():
    from deep_db_agents.observability import LOGGER_NAMESPACE

    configure_logging()
    configure_logging()
    logger = logging.getLogger(LOGGER_NAMESPACE)
    marked = [h for h in logger.handlers if getattr(h, "_deep_db_agents", False)]
    assert len(marked) == 1  # un solo handler della libreria, anche con più chiamate
