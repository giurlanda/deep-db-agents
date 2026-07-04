from __future__ import annotations

import pytest

from deep_db_agents.exceptions import InvalidDbUrlError
from deep_db_agents.url import parse_db_url


def test_parse_full_url():
    parsed = parse_db_url("mysql://localhost:3306")
    assert parsed.scheme == "mysql"
    assert parsed.host == "localhost"
    assert parsed.port == 3306


def test_scheme_is_lowercased():
    assert parse_db_url("Postgres://db:5432").scheme == "postgres"


def test_missing_host_and_port_ok():
    parsed = parse_db_url("neo4j://")
    assert parsed.scheme == "neo4j"
    assert parsed.host is None
    assert parsed.port is None


@pytest.mark.parametrize("bad", ["", "not-a-url", "://nohost"])
def test_invalid_url_raises(bad):
    with pytest.raises(InvalidDbUrlError):
        parse_db_url(bad)


def test_non_numeric_port_raises():
    with pytest.raises(InvalidDbUrlError):
        parse_db_url("mysql://localhost:abc")


def test_sqlite_relative_path():
    parsed = parse_db_url("sqlite:///data/app.db")
    assert parsed.scheme == "sqlite"
    assert parsed.host is None and parsed.port is None
    assert parsed.path == "data/app.db"


def test_sqlite_absolute_path():
    parsed = parse_db_url("sqlite:////var/db/app.db")
    assert parsed.path == "/var/db/app.db"


def test_sqlite_in_memory():
    assert parse_db_url("sqlite://:memory:").path == ":memory:"


def test_duckdb_folder_keeps_trailing_slash():
    # La trailing slash distingue una cartella (data lake) da un file.
    assert parse_db_url("duckdb:///lake/").path == "lake/"


def test_duckdb_two_slash_relative_tolerated():
    assert parse_db_url("duckdb://warehouse/dw.duckdb").path == "warehouse/dw.duckdb"
