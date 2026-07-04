# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
