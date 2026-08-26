"""Guard the deepagents API surface this library builds on.

The package declares a wide ``deepagents>=0.7.8,<1.0`` range, so these tests fail fast
if an upstream release inside that range removes or renames something we depend on,
instead of surfacing as an obscure error at agent build time.
"""

from __future__ import annotations

from importlib.metadata import version

import pytest


def test_installed_deepagents_matches_declared_range():
    major, minor, *_ = (int(part) for part in version("deepagents").split(".")[:2])
    assert (major, minor) >= (0, 7), "deep-db-agents requires deepagents >= 0.7.8"
    assert major < 1, "deepagents 1.x is outside the declared range"


def test_agent_entry_points_are_importable():
    from deepagents import CompiledSubAgent, create_deep_agent

    assert callable(create_deep_agent)
    # Used by create_deep_db_multi_agents to wrap already-compiled sub-agents.
    sub = CompiledSubAgent(name="db", description="a database", runnable=object())
    assert sub["name"] == "db"


def test_backend_protocol_surface():
    from deepagents.backends import FilesystemBackend
    from deepagents.backends.protocol import BackendProtocol
    from deepagents.backends.state import StateBackend

    for method in ("write", "read", "ls", "upload_files"):
        assert callable(getattr(BackendProtocol, method, None)), method
    assert issubclass(StateBackend, BackendProtocol)
    assert issubclass(FilesystemBackend, BackendProtocol)


@pytest.mark.parametrize("field", ["path", "error"])
def test_write_result_fields(field):
    from deepagents.backends.protocol import WriteResult

    # workspace.materialize_result reads both attributes off the write result.
    assert hasattr(WriteResult(path="/x.csv"), field)
