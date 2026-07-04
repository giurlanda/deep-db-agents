"""Connection configuration passed to dialects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConnectionConfig:
    """Connection parameters for a database.

    ``credential`` is a free-form dictionary whose content depends on the specific DB
    (e.g. ``{"user": ..., "password": ...}`` or ``{"secret_key": ...}``). Well-known keys
    (``database``/``db``) are exposed as convenience properties.

    Attributes:
        scheme: The URL scheme identifying the dialect (e.g. ``"postgres"``).
        host: Hostname for network databases, or ``None`` for file-based databases.
        port: Port for network databases, or ``None`` for file-based databases.
        credential: Free-form credential/connection dictionary.
        path: File/directory path for file-based databases (``sqlite``/``duckdb``);
            ``None`` for network databases. ``:memory:`` denotes an in-memory database.
    """

    scheme: str
    host: str | None
    port: int | None
    credential: dict[str, Any] = field(default_factory=dict)
    #: File/directory path for file-based databases (``sqlite``/``duckdb``); ``None`` for
    #: network databases. ``:memory:`` denotes an in-memory database.
    path: str | None = None

    @property
    def database(self) -> str | None:
        """Return the logical database/schema name, if provided in the credentials.

        Returns:
            str | None: The value of the ``database`` or ``db`` credential key, or
            ``None`` if neither is present.
        """
        return self.credential.get("database") or self.credential.get("db")

    def __repr__(self) -> str:
        """Return a repr with credentials masked.

        Returns:
            str: A string representation of this config where ``credential`` content
            is masked so passwords never leak into logs, tracebacks, or error messages.
        """
        # Mask the contents of ``credential`` so passwords never end up in logs,
        # tracebacks, or error messages when the object is printed.
        cred = "{…}" if self.credential else "{}"
        return (
            f"ConnectionConfig(scheme={self.scheme!r}, host={self.host!r}, "
            f"port={self.port!r}, credential={cred}, path={self.path!r})"
        )
