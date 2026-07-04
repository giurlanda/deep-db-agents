"""Global registry mapping URL scheme -> dialect class."""

from __future__ import annotations

from .base import DbDialect
from .exceptions import UnsupportedSchemeError

_REGISTRY: dict[str, type[DbDialect]] = {}


def register(*schemes: str):
    """Return a decorator that registers a ``DbDialect`` class for one or more URL schemes.

    Args:
        *schemes: URL scheme names to register the class under. If omitted, the
            class's own ``schemes`` attribute is used instead.

    Returns:
        Callable[[type[DbDialect]], type[DbDialect]]: A decorator that registers the
        decorated class and returns it unchanged.

    Examples:
        >>> @register("postgres", "postgresql")
        ... class PostgresDialect(DbDialect): ...
    """

    def decorator(cls: type[DbDialect]) -> type[DbDialect]:
        """Register ``cls`` under the enclosing ``schemes`` and return it.

        Args:
            cls: The dialect class to register.

        Returns:
            type[DbDialect]: The same class, unchanged.

        Raises:
            ValueError: If no scheme names are available (neither passed to
                :func:`register` nor set on ``cls.schemes``).
        """
        names = schemes or cls.schemes
        if not names:
            raise ValueError(f"{cls.__name__} does not specify any scheme to register.")
        cls.schemes = tuple(names)
        for scheme in names:
            _REGISTRY[scheme.lower()] = cls
        return cls

    return decorator


def resolve(scheme: str) -> type[DbDialect]:
    """Return the dialect class for a scheme.

    Args:
        scheme: The URL scheme to resolve (case-insensitive).

    Returns:
        type[DbDialect]: The dialect class registered for ``scheme``.

    Raises:
        UnsupportedSchemeError: If no dialect is registered for ``scheme``.
    """
    try:
        return _REGISTRY[scheme.lower()]
    except KeyError:
        raise UnsupportedSchemeError(scheme, available_schemes()) from None


def available_schemes() -> list[str]:
    """List the schemes currently registered.

    Returns:
        list[str]: The list of registered scheme names.
    """
    return list(_REGISTRY)
