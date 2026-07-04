"""Driver-specific helpers for Elasticsearch (official ``elasticsearch`` driver)."""

from __future__ import annotations

from typing import Any

from ...connection import ConnectionConfig


def connect(conn: ConnectionConfig, *, request_timeout: int | None = None) -> Any:
    """Open an Elasticsearch client from the given credentials.

    The driver is imported lazily so the package can be imported without the
    ``elasticsearch`` extra installed.

    Args:
        conn: Connection configuration with host, port, and credentials. Recognized
            ``credential`` keys: ``use_ssl``, ``verify_certs``, ``ca_certs``,
            ``api_key``, ``user``, ``password``.
        request_timeout: Optional request timeout in seconds; forwarded to the
            client only if positive.

    Returns:
        Any: An initialized ``Elasticsearch`` client instance.

    Raises:
        ImportError: If the ``elasticsearch`` package is not installed.
    """
    try:
        from elasticsearch import Elasticsearch
    except ImportError as exc:  # pragma: no cover - depends on the installed extra
        raise ImportError(
            "The Elasticsearch dialect requires the 'elasticsearch' extra "
            "(pip install 'deep-db-agents[elasticsearch]')."
        ) from exc

    cred = conn.credential
    host = conn.host or "localhost"
    port = conn.port or 9200
    scheme = "https" if cred.get("use_ssl") else "http"
    kwargs: dict[str, Any] = {
        "hosts": [f"{scheme}://{host}:{port}"],
        "verify_certs": cred.get("verify_certs", True),
    }
    if request_timeout and request_timeout > 0:
        kwargs["request_timeout"] = request_timeout
    if cred.get("ca_certs"):
        kwargs["ca_certs"] = cred["ca_certs"]
    if cred.get("api_key"):
        kwargs["api_key"] = cred["api_key"]
    elif cred.get("user"):
        kwargs["basic_auth"] = (cred["user"], cred.get("password", ""))
    return Elasticsearch(**kwargs)
