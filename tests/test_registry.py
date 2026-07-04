from __future__ import annotations

import pytest

from deep_db_agents import available_schemes
from deep_db_agents.dialects.mysql import MySQLDialect
from deep_db_agents.dialects.postgres import PostgresDialect
from deep_db_agents.exceptions import UnsupportedSchemeError
from deep_db_agents.registry import resolve


def test_all_schemes_registered():
    schemes = set(available_schemes())
    assert {"mysql", "mariadb", "postgres", "postgresql", "mongodb", "neo4j"} <= schemes


def test_resolve_returns_dialect_class():
    assert resolve("mysql") is MySQLDialect
    assert resolve("postgresql") is PostgresDialect


def test_resolve_is_case_insensitive():
    assert resolve("MySQL") is MySQLDialect


def test_unknown_scheme_raises():
    with pytest.raises(UnsupportedSchemeError):
        resolve("oracle")
