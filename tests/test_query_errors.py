from __future__ import annotations

from deep_db_agents.connection import ConnectionConfig
from deep_db_agents.exceptions import RowBudgetExceededError
from deep_db_agents.query_errors import format_budget_block, format_query_error


def test_redacts_dsn_credentials_in_driver_message():
    exc = RuntimeError("could not connect to mysql://admin:s3cr3t@db.internal:3306/shop")
    out = format_query_error(exc)
    assert "s3cr3t" not in out
    assert "admin" not in out
    assert "mysql://***@" in out


def test_redacts_key_value_secrets():
    exc = RuntimeError("auth failed: password='hunter2' token=abc123")
    out = format_query_error(exc)
    assert "hunter2" not in out
    assert "abc123" not in out
    assert "password=***" in out


def test_keeps_non_secret_detail():
    exc = RuntimeError("unknown column 'foo' in table 'bar'")
    out = format_query_error(exc, query="SELECT foo FROM bar")
    assert "unknown column 'foo'" in out
    assert "SELECT foo FROM bar" in out


def test_budget_block_is_actionable_feedback():
    exc = RowBudgetExceededError("Session row budget exhausted (2/1). Start a new session.")
    out = format_budget_block(exc, what="query")
    assert "row budget is exhausted" in out
    assert "2/1" in out
    # The message steers the agent to aggregate or reset rather than interrupting the turn.
    assert "aggregate" in out.lower()


def test_connection_config_repr_masks_credentials():
    conn = ConnectionConfig(
        scheme="postgres",
        host="db",
        port=5432,
        credential={"user": "u", "password": "s3cr3t"},
    )
    text = repr(conn)
    assert "s3cr3t" not in text
    assert "password" not in text
    assert "postgres" in text and "db" in text
