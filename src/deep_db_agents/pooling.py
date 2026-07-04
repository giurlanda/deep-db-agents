"""Client-side reuse of drivers that are already thread-safe connection pools.

The MongoDB (``MongoClient``), Neo4j (``Driver``), and Elasticsearch/OpenSearch drivers
internally manage a connection pool and expose a thread-safe client object: recreating
it on every tool call would pay an unnecessary TCP/TLS/auth handshake. ``LazyClient``
keeps a single instance per dialect (i.e. per agent), initialized lazily and in a
thread-safe way.

DB-API 2.0 SQL drivers (pymysql, psycopg), on the other hand, remain
connection-per-call: the connection is not thread-safe and tool calls may run in
parallel.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class LazyClient:
    """Thread-safe container for a reusable driver client, initialized on first use."""

    def __init__(self) -> None:
        """Initialize an empty, not-yet-built client container."""
        self._client: Any = None
        self._lock = threading.Lock()

    def get(self, factory: Callable[[], Any]) -> Any:
        """Return the cached client, building it with ``factory`` on the first call.

        Args:
            factory: Zero-argument callable that constructs the client. Only invoked
                once, on the first call to this method.

        Returns:
            Any: The cached (or newly built) client instance.
        """
        if self._client is None:
            with self._lock:
                if self._client is None:  # double-checked: build only once
                    self._client = factory()
        return self._client

    def close(self) -> None:
        """Close and forget the client, if present (idempotent).

        Returns:
            None.
        """
        with self._lock:
            client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            client.close()
