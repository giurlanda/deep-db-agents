"""Singleton registry of ``BackendProtocol`` backends indexed by UUID.

Allows registering a backend instance (derived from deepagents' ``BackendProtocol``),
obtaining an opaque identifier that can later be used to retrieve or remove it,
without letting the reference to the object circulate through the system.

The registry is shared at the process level: whoever registers a backend (``add``)
is responsible for removing it (``remove``) at the end of the agent's lifetime,
otherwise the instance stays referenced for the entire process lifetime.
"""

from __future__ import annotations

import threading
import uuid

from deepagents.backends.protocol import BackendProtocol


class BERegistry:
    """Singleton ``UUID -> BackendProtocol`` registry.

    Every instantiation returns the same object, so the registry is shared at the
    process level. Creation and mutations are protected by a lock: tools may run
    in parallel threads (parallel tool calls in the tool node).

    >>> reg = BERegistry()
    >>> key = reg.add(my_backend)
    >>> reg.get(key) is my_backend
    True
    >>> reg.remove(key)
    >>> reg.get(key) is None
    True
    """

    _instance: BERegistry | None = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> BERegistry:
        """Create or return the singleton instance.

        Returns:
            BERegistry: The single shared instance of the registry.
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:  # double-checked: avoids duplicate instances
                    instance = super().__new__(cls)
                    instance._backends = {}
                    instance._lock = threading.Lock()
                    cls._instance = instance
        return cls._instance

    def add(self, backend: BackendProtocol) -> str:
        """Register a backend and return its identifier.

        Args:
            backend: The ``BackendProtocol`` instance to register.

        Returns:
            str: The UUID (as a string) associated with the registered backend.

        Raises:
            TypeError: If ``backend`` is not a ``BackendProtocol`` instance.
        """
        if not isinstance(backend, BackendProtocol):
            raise TypeError(f"Expected a BackendProtocol, got {type(backend).__name__!r}.")
        key = str(uuid.uuid4())
        with self._lock:
            self._backends[key] = backend
        return key

    def get(self, key: str) -> BackendProtocol | None:
        """Retrieve a registered backend.

        Args:
            key: The UUID string previously returned by :meth:`add`.

        Returns:
            BackendProtocol | None: The registered backend, or ``None`` if
            ``key`` is not registered.
        """
        with self._lock:
            return self._backends.get(key)

    def remove(self, key: str) -> None:
        """Remove a registered backend.

        Args:
            key: The UUID string previously returned by :meth:`add`.

        Returns:
            None. This is a no-op if ``key`` does not exist.
        """
        with self._lock:
            self._backends.pop(key, None)
