"""Driver-specific helpers for OpenSearch (official ``opensearch-py`` driver)."""

from __future__ import annotations

from typing import Any

from ...connection import ConnectionConfig


def connect(conn: ConnectionConfig, *, request_timeout: int | None = None) -> Any:
    """Open an OpenSearch client from the given credentials.

    The driver is imported lazily so the package can be imported without the
    ``opensearch`` extra installed.

    Args:
        conn: Connection configuration with host, port, and credentials. Recognized
            ``credential`` keys: ``use_ssl``, ``verify_certs``, ``ca_certs``, ``user``,
            ``password``.
        request_timeout: Optional request timeout in seconds; forwarded to the
            client only if positive.

    Returns:
        Any: An initialized ``OpenSearch`` client instance.

    Raises:
        ImportError: If the ``opensearchpy`` package is not installed.
    """
    try:
        from opensearchpy import OpenSearch
    except ImportError as exc:  # pragma: no cover - depends on the installed extra
        raise ImportError(
            "The OpenSearch dialect requires the 'opensearch' extra "
            "(pip install 'deep-db-agents[opensearch]')."
        ) from exc

    cred = conn.credential
    host = conn.host or "localhost"
    port = conn.port or 9200
    kwargs: dict[str, Any] = {
        "hosts": [{"host": host, "port": port}],
        "use_ssl": bool(cred.get("use_ssl", False)),
        "verify_certs": cred.get("verify_certs", True),
    }
    if request_timeout and request_timeout > 0:
        kwargs["timeout"] = request_timeout
    if cred.get("ca_certs"):
        kwargs["ca_certs"] = cred["ca_certs"]
    if cred.get("user"):
        kwargs["http_auth"] = (cred["user"], cred.get("password", ""))
    return OpenSearch(**kwargs)
