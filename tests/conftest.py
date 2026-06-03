"""Shared pytest fixtures."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from vlo.storage.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def clock():
    """A deterministic, monotonically increasing clock for now_fn injection."""
    counter = itertools.count(1000.0, 1.0)
    return lambda: next(counter)
