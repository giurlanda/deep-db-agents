"""Database URL parsing.

Two supported forms:

- **Network DBs** (``mysql``, ``postgres``, ``mongodb``, ``neo4j``…):
  ``<scheme>://<host>:<port>``.
- **File-based DBs** (``sqlite``, ``duckdb``): ``<scheme>://<path>`` where ``<path>``
  is the file path (or directory, for the DuckDB data lake) following the SQLAlchemy
  convention: ``sqlite:///rel.db`` is relative to the working dir, ``sqlite:////abs.db``
  is absolute, ``sqlite://:memory:`` is in-memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .exceptions import InvalidDbUrlError

#: Schemes whose URLs point to a local file/directory instead of host:port.
FILE_SCHEMES = frozenset({"sqlite", "duckdb"})


@dataclass(frozen=True)
class ParsedUrl:
    """Components of a database URL.

    ``scheme`` is normalized to lowercase and is what selects the dialect.
    ``host``/``port`` are set for network DBs; ``path`` is set for file-based DBs
    (with ``:memory:`` for in-memory databases). A trailing slash in ``path``
    indicates a directory (DuckDB data lake).

    Attributes:
        scheme: The normalized (lowercase) URL scheme.
        host: Hostname for network databases, or ``None`` for file-based databases.
        port: Port for network databases, or ``None`` for file-based databases.
        path: File/directory path for file-based databases, or ``None`` for network
            databases.
    """

    scheme: str
    host: str | None
    port: int | None
    path: str | None = None


def _parse_file_url(scheme: str, db_url: str) -> ParsedUrl:
    """Extract the path from a SQLAlchemy-style file-based DB URL.

    Args:
        scheme: The normalized scheme (must be one of ``FILE_SCHEMES``).
        db_url: The full URL string to parse.

    Returns:
        ParsedUrl: The parsed URL with ``host``/``port`` set to ``None`` and
        ``path`` populated (``:memory:`` for in-memory databases).

    Raises:
        InvalidDbUrlError: If the URL has no path after the scheme.
    """
    rest = db_url.split("://", 1)[1]
    if rest == "" or rest == ":memory:":
        return ParsedUrl(scheme=scheme, host=None, port=None, path=":memory:")
    if rest.startswith("//"):
        path = rest[1:]  # four slashes total -> absolute path (/abs/...)
    elif rest.startswith("/"):
        path = rest[1:]  # three slashes total -> path relative to the working dir
    else:
        path = rest  # tolerance: two slashes (sqlite://rel.db) -> relative
    if not path:
        raise InvalidDbUrlError(f"Missing file path in URL: {db_url!r}.")
    return ParsedUrl(scheme=scheme, host=None, port=None, path=path)


def parse_db_url(db_url: str) -> ParsedUrl:
    """Extract the components from a ``<scheme>://<host>:<port>`` or ``<scheme>://<path>`` URL.

    Args:
        db_url: The database URL to parse.

    Returns:
        ParsedUrl: The parsed URL components.

    Raises:
        InvalidDbUrlError: If ``db_url`` is not a string, has no ``"://"`` separator,
            has an empty scheme, or (for network schemes) has a non-numeric port.

    Examples:
        >>> parse_db_url("mysql://localhost:3306")
        ParsedUrl(scheme='mysql', host='localhost', port=3306, path=None)
        >>> parse_db_url("sqlite:///data/app.db")
        ParsedUrl(scheme='sqlite', host=None, port=None, path='data/app.db')
    """
    if not isinstance(db_url, str) or "://" not in db_url:
        raise InvalidDbUrlError(
            f"Invalid URL: {db_url!r}. Expected format '<scheme>://<host>:<port>'."
        )

    scheme = db_url.split("://", 1)[0].lower().strip()
    if not scheme:
        raise InvalidDbUrlError(f"Missing scheme in URL: {db_url!r}.")

    if scheme in FILE_SCHEMES:
        return _parse_file_url(scheme, db_url)

    parts = urlsplit(db_url)
    try:
        port = parts.port
    except ValueError as exc:  # non-numeric port
        raise InvalidDbUrlError(f"Invalid port in URL: {db_url!r}.") from exc

    return ParsedUrl(scheme=scheme, host=parts.hostname, port=port)
