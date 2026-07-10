"""Fixtures condivise: connessione DB-API finta per testare i tool senza un database reale."""

from __future__ import annotations

from collections.abc import Callable

import pytest


class FakeCursor:
    """Cursore DB-API minimale, programmabile con un handler ``(sql, params) -> (desc, rows)``."""

    def __init__(self, handler: Callable[[str, tuple | None], tuple[list, list]]):
        self._handler = handler
        self.executed: list[tuple[str, tuple | None]] = []
        self.description: list[tuple] = []
        self._rows: list = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))
        desc, rows = self._handler(sql, params)
        self.description = desc
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)

    def fetchmany(self, size: int) -> list:
        chunk, self._rows = list(self._rows[:size]), list(self._rows[size:])
        return chunk

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self) -> None:  # noqa: D401
        pass


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor

    def close(self) -> None:  # noqa: D401
        pass


@pytest.fixture
def make_dialect(monkeypatch):
    """Restituisce una factory che istanzia un dialect col metodo ``_connect`` mockato.

    L'handler riceve ``(sql, params)`` e ritorna ``(description, rows)`` in stile DB-API.
    Espone anche il ``FakeCursor`` per ispezionare gli statement eseguiti.
    """

    def _factory(dialect_cls, handler):
        cursor = FakeCursor(handler)
        dialect = dialect_cls()
        monkeypatch.setattr(
            dialect, "_connect", lambda conn, guardrails=None: FakeConnection(cursor)
        )
        return dialect, cursor

    return _factory
