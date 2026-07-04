"""Imports all dialects to populate the registry when the package is imported.

The side-effect import is intentional: each module registers its own dialect via the
``@register`` decorator.
"""

from __future__ import annotations

from . import (  # noqa: F401
    duckdb,
    elasticsearch,
    mariadb,
    mongodb,
    mysql,
    neo4j,
    opensearch,
    postgres,
    sqlite,
)

__all__ = [
    "mysql",
    "mariadb",
    "postgres",
    "mongodb",
    "neo4j",
    "sqlite",
    "duckdb",
    "elasticsearch",
    "opensearch",
]
