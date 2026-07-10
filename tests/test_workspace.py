from __future__ import annotations

from deepagents.backends.protocol import BackendProtocol, WriteResult

from deep_db_agents.workspace import json_bytes, materialize_result, take_within_bytes


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
    assert result.truncated is False
    assert "out.csv" in backend.written
    assert backend.written["out.csv"].splitlines()[0] == "a,b"


def test_materialize_stops_at_byte_limit_and_flags_truncation():
    # Il file materializzato non deve superare max_bytes: le righe vengono scritte finché
    # stanno sotto la soglia, poi la scrittura si ferma e il risultato è marcato incompleto.
    backend = RecordingBackend()
    rows = [[i, "x" * 20] for i in range(1000)]
    result = materialize_result(
        ["id", "payload"], rows, backend=backend, filename="big.csv", max_bytes=200
    )
    written = backend.written["big.csv"]
    assert result.truncated is True
    assert 0 < result.row_count < 1000
    # Il file resta sotto il limite imposto.
    assert len(written.encode("utf-8")) <= 200
    # Vengono scritte solo righe complete (header + N righe, nessuna riga tronca).
    assert len(written.splitlines()) == result.row_count + 1
    # L'avviso di file incompleto è presente nel summary restituito all'agente.
    assert "INCOMPLETE" in result.to_summary()


def test_materialize_consumes_rows_lazily_up_to_the_budget():
    # Con una sorgente pigra (generatore), materialize non deve consumare tutte le righe:
    # si ferma appena il budget in byte è raggiunto.
    backend = RecordingBackend()
    pulled: list[int] = []

    def gen():
        for i in range(10_000):
            pulled.append(i)
            yield [i, "y" * 50]

    result = materialize_result(
        ["id", "payload"], gen(), backend=backend, filename="lazy.csv", max_bytes=500
    )
    assert result.truncated is True
    # Molte meno delle 10.000 righe sono state estratte dalla sorgente.
    assert len(pulled) < 1000


def test_take_within_bytes_keeps_items_below_budget():
    items = [{"a": 1}, {"a": 2}, {"a": 3}]
    size = json_bytes(items[0])
    kept, truncated = take_within_bytes(items, json_bytes, max_bytes=size * 2)
    assert kept == items[:2]
    assert truncated is True

    kept_all, truncated_all = take_within_bytes(items, json_bytes, max_bytes=None)
    assert kept_all == items
    assert truncated_all is False
