# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-08-26

### Added

- **GitHub Actions `ci` workflow** ([.github/workflows/ci.yml](.github/workflows/ci.yml)):
  runs `ruff check` + `ruff format --check` and the full `pytest` suite on Python 3.11, 3.12
  and 3.13 for every push to `main` and every pull request.
- **GitHub Actions `release` workflow** ([.github/workflows/release.yml](.github/workflows/release.yml)):
  triggered by a `v*` tag, it runs lint and tests, verifies that the tag matches
  `deep_db_agents.__version__`, builds the sdist/wheel, validates them with `twine check`, then
  publishes to TestPyPI and PyPI via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
  (OIDC through the `testpypi` / `pypi` GitHub environments, no API token stored in the repo).
  See [#12](https://github.com/giurlanda/deep-db-agents/issues/12).
- **`tests/test_workflows.py`**: parses the workflow files and guards the parts the release
  depends on — the CI matrix covering every supported Python version, TestPyPI running before
  PyPI, and both publish jobs declaring `id-token: write` with a named environment.
- **Python 3.13 classifier** in `pyproject.toml`, now that CI tests it.

### Changed

- The `dev` extra also installs `pyyaml`, needed by the new workflow tests.

### Fixed

- **`ci` and `release` workflow install steps**: `uv pip install --system` targeted the runner's
  Debian system interpreter, which is externally managed, so every job failed immediately
  (`The interpreter at /usr is externally managed`). The jobs now create a uv-managed virtualenv
  (`uv venv`), install into it, and run `ruff`, `pytest`, the tag/version check and the build
  through `uv run --no-sync`. See [#14](https://github.com/giurlanda/deep-db-agents/issues/14)
  and [#16](https://github.com/giurlanda/deep-db-agents/issues/16).

### Notes

- Trusted Publishing needs a one-off setup on each index: register `giurlanda/deep-db-agents`,
  workflow `release.yml`, environment `testpypi` (TestPyPI) or `pypi` (PyPI) as a trusted
  publisher. Until that is done the publish jobs will fail to authenticate.

## [0.3.2] - 2026-08-26

### Changed

- **`deepagents` dependency range widened** from `>=0.6,<0.7` to `>=0.7.8,<1.0`, aligning the
  library with the current upstream line (0.7.9 at the time of the bump) and, transitively,
  with newer `langchain` / `langchain-core` / `langgraph` releases.
  See [#10](https://github.com/giurlanda/deep-db-agents/issues/10).

### Added

- **`tests/test_deepagents_compat.py`**: guards the deepagents API surface the library builds
  on (`create_deep_agent`, `CompiledSubAgent`, `BackendProtocol.write/read/ls/upload_files`,
  `WriteResult`), so a future release inside the declared range that removes or renames one of
  them fails fast instead of erroring at agent build time.

### Notes

- On deepagents 0.7, `StateBackend` applies file writes to the graph state itself and
  `WriteResult` no longer carries a `files_update`. `materialize_result` already reads it
  defensively via `getattr`, so materialization keeps working unchanged with both `StateBackend`
  and `FilesystemBackend` (CSV and Parquet verified end to end).
- deepagents 0.7 deprecates passing `model=None` to `create_deep_agent` (removal planned for
  1.0). `create_deep_db_agents` still forwards no model when the caller omits one, which now
  emits a `LangChainDeprecationWarning`. Passing an explicit `model=` — as every example in
  `examples/` does — avoids it. Making the model mandatory is a breaking change, deferred.

## [0.3.1] - 2026-07-13

### Added

- **`RowBudgetExceededError`** (subclass of `GuardrailError`, exported from the package):
  raised by `SessionBudget.charge` when the cumulative session row budget is exhausted.
  See [#8](https://github.com/giurlanda/deep-db-agents/issues/8).
- **`query_errors.format_budget_block`**: formats the budget exhaustion as corrective
  feedback for the agent, mirroring `format_estimate_block`.

### Changed

- Session row-budget exhaustion is now reflected back to the agent as tool feedback instead
  of interrupting the turn with a hard exception. Every tool that charges the budget
  (`sample_rows`, `run_query`, `materialize_query`, and the Search/Neo4j/MongoDB equivalents)
  catches `RowBudgetExceededError` and returns the formatted message.
- **`GuardrailConfig.row_budget`** default raised from `50_000` to `10_000_000`, so normal
  exploration no longer exhausts it prematurely.

## [0.3.0] - 2026-07-10

### Added

- **`GuardrailConfig.max_materialized_bytes`** (default 10 MiB): a hard cap on the size, in
  bytes, of a single materialized file. See [#5](https://github.com/giurlanda/deep-db-agents/issues/5).

### Changed

- The materialization tools (`materialize_query`/`materialize_aggregate`/`materialize_cypher`)
  are now bounded by `max_materialized_bytes` **instead of** `hard_max_rows`. `hard_max_rows`
  bounds the rows returned into the agent's *context*; materialization writes to *file*, so it is
  now bounded by file size, letting an agent save datasets far larger than `hard_max_rows`. Rows
  are streamed and written only while they fit under the byte ceiling; when the write stops early
  the tool response warns that the file is **incomplete**. `materialize_result` gains a `max_bytes`
  parameter and a `truncated` flag on `MaterializedResult`. Elasticsearch/OpenSearch stay
  additionally bound by the engine result window; the Neo4j materialization now also enforces the
  `EXPLAIN` row-estimate guardrail.
- `materialize_result` now normalizes a relative `filename` to an absolute path (a leading `/` is
  prepended), so materialized files are written at the backend filesystem root instead of a
  path-dependent location.

## [0.2.0] - 2026-07-08

### Changed

- **Backend injection (breaking).** The filesystem backend used by the file-writing tools
  (`materialize_query`/`materialize_aggregate`/`materialize_cypher` and the Elasticsearch/
  OpenSearch `aggregate` overflow path) is now injected **directly into the tool closures**
  instead of being resolved at call time through a process-wide registry. `DbDialect.build_tools`
  (and every dialect implementation) gains a `backend: BackendProtocol | None = None` parameter.
  `create_deep_db_agents` and `create_deep_db_multi_agents` default `backend` to an ephemeral
  `StateBackend()` when the caller omits it and forward the same instance to both the agent and
  its tools; `create_db_agents` passes `backend=None`.
- When no backend is configured, the file-writing tools now report *"Cannot write to file: no
  filesystem backend is configured."* instead of a registry-lookup error.
- The file-writing tools now return a `Command` so a `StateBackend` write can notify the files to
  apply to the agent's state; external backends (`FilesystemBackend`/`StoreBackend`) persist
  directly and only the summary message is returned.

### Removed

- **`BERegistry` and the `be_uuid` mechanism (breaking).** The `deep_db_agents.backend_registry`
  module and the `config={"configurable": {"be_uuid": ...}}` indirection are gone; pass the
  backend via the `backend=` kwarg instead.

## [0.1.2] - 2026-07-08

### Changed

- The EXPLAIN row-estimate guardrail no longer interrupts the agent's turn: when a
  query's estimated result set exceeds `explain_row_threshold`, the query tools reflect
  the block back to the agent as corrective feedback (refine filters or aggregate and
  retry) instead of raising. The query is still **not executed**. The session row budget
  remains a hard exception.

### Added

- New `EstimateExceededError` exception (a subclass of `GuardrailError`), raised by
  `GuardrailConfig.check_estimate` and re-exported from the package root.

## [0.1.1] - 2026-07-04

### Changed

- Renamed `create_db_agent` to `create_db_agents` for consistency with
  `create_deep_db_agents`.
- Renamed `create_deep_db_multi_agent` to `create_deep_db_multi_agents` for the
  same reason.
- Rewrote `.gitignore` for a Python library project (packaging, tooling,
  coverage, and mkdocs build artifacts).

### Removed

- Untracked 61 `__pycache__/*.pyc` files that had been committed by mistake
  before the pycache ignore rule took effect; they remain untracked going
  forward but are unaffected on disk.
