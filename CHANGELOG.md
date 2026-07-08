# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
