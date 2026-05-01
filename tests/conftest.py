"""Shared pytest fixtures for the teammate test suite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Resolve the actual teammate repo root from the test file location."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def template_dir(repo_root: Path) -> Path:
    return repo_root / "templates" / "team-brain-skeleton"


@pytest.fixture
def populated_brain(tmp_path: Path, template_dir: Path) -> Path:
    """A team-brain rooted at tmp_path, populated from the bundled template."""
    target = tmp_path / "team-brain"
    shutil.copytree(template_dir, target)
    return target


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip teammate env vars so tests start with a known baseline."""
    for var in (
        "TEAMMATE_BRAIN_ROOT",
        "TEAMMATE_FORCE_INIT",
        "TEAMMATE_OVERRIDE",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
