from __future__ import annotations

from deepagents.backends.protocol import BackendProtocol, WriteResult

from deep_db_agents.workspace import materialize_result


class RecordingBackend(BackendProtocol):
    def __init__(self):
        self.written: dict[str, str] = {}

    def write(self, path, content):
        self.written[path] = content
        return WriteResult(path=path)


def test_materialize_defaults_to_csv_without_analysis_extra():
    # Il default non deve richiedere pandas/pyarrow (extra 'analysis'): deve essere CSV,
    # così una install base non fallisce con ImportError dentro il tool.
    backend = RecordingBackend()
    result = materialize_result(["a", "b"], [[1, 2], [3, 4]], backend=backend, filename="out.csv")
    assert result.fmt == "csv"
    assert result.row_count == 2
    assert "out.csv" in backend.written
    assert backend.written["out.csv"].splitlines()[0] == "a,b"
