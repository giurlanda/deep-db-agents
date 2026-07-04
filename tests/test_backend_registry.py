from __future__ import annotations

import uuid

import pytest
from deepagents.backends.protocol import BackendProtocol

from deep_db_agents.backend_registry import BERegistry


class FakeBackend(BackendProtocol):
    """Backend minimale: eredita da ``BackendProtocol`` senza implementare i metodi I/O."""


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Azzera lo stato del singleton prima e dopo ogni test (è globale al processo)."""
    BERegistry._instance = None
    yield
    BERegistry._instance = None


def test_is_singleton():
    assert BERegistry() is BERegistry()


def test_singleton_shares_state():
    backend = FakeBackend()
    key = BERegistry().add(backend)
    # Una nuova "istanza" vede lo stesso registro.
    assert BERegistry().get(key) is backend


def test_add_returns_valid_uuid():
    key = BERegistry().add(FakeBackend())
    assert isinstance(key, str)
    # Non solleva: è un UUID ben formato.
    uuid.UUID(key)


def test_get_returns_registered_backend():
    reg = BERegistry()
    backend = FakeBackend()
    key = reg.add(backend)
    assert reg.get(key) is backend


def test_get_unknown_key_returns_none():
    assert BERegistry().get("non-esiste") is None


def test_add_assigns_distinct_keys():
    reg = BERegistry()
    b1, b2 = FakeBackend(), FakeBackend()
    k1, k2 = reg.add(b1), reg.add(b2)
    assert k1 != k2
    assert reg.get(k1) is b1
    assert reg.get(k2) is b2


def test_remove_deletes_backend():
    reg = BERegistry()
    key = reg.add(FakeBackend())
    reg.remove(key)
    assert reg.get(key) is None


def test_remove_unknown_key_is_noop():
    # Non deve sollevare.
    BERegistry().remove("non-esiste")


def test_add_rejects_non_backend():
    with pytest.raises(TypeError):
        BERegistry().add(object())
