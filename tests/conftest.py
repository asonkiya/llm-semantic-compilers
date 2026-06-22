"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def python_sample_repo() -> Path:
    return Path(__file__).parent / "fixtures" / "python_sample"
