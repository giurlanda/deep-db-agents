"""Guards the GitHub Actions workflows that gate CI and publishing.

The workflows are not exercised by the test suite itself, so a YAML typo or a
silently dropped job would only surface on a push (CI) or on a release tag
(publishing), when it is most expensive. These tests parse the workflow files
and assert the pieces the release process depends on.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


@pytest.mark.parametrize("name", ["ci.yml", "release.yml", "docs.yml"])
def test_workflow_parses_and_has_jobs(name: str) -> None:
    workflow = _load(name)
    assert workflow["jobs"], f"{name} declares no jobs"


def test_ci_matrix_covers_supported_python_versions() -> None:
    """The CI matrix must test every Python version the package claims to support."""
    matrix = _load("ci.yml")["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    project = tomllib.loads(PYPROJECT.read_text())["project"]

    claimed = {
        classifier.rsplit(" :: ", 1)[1]
        for classifier in project["classifiers"]
        if classifier.startswith("Programming Language :: Python :: 3.")
    }
    assert claimed <= set(matrix)

    minimum = project["requires-python"].removeprefix(">=")
    assert minimum in matrix, f"requires-python floor {minimum} is not in the CI matrix"


def test_release_publishes_to_testpypi_before_pypi() -> None:
    """PyPI is the irreversible step: it must run only after TestPyPI succeeded."""
    jobs = _load("release.yml")["jobs"]
    assert jobs["testpypi"]["needs"] == "build"
    assert jobs["pypi"]["needs"] == "testpypi"


def test_release_publish_jobs_use_trusted_publishing() -> None:
    """OIDC id-token permission and a named environment are what replace API tokens."""
    jobs = _load("release.yml")["jobs"]
    for name in ("testpypi", "pypi"):
        job = jobs[name]
        assert job["permissions"]["id-token"] == "write"
        assert job["environment"]["name"] == name
        steps = job["steps"]
        assert any("pypa/gh-action-pypi-publish" in step.get("uses", "") for step in steps)


def test_release_is_triggered_by_version_tags() -> None:
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = _load("release.yml")[True]
    assert triggers["push"]["tags"] == ["v*"]
