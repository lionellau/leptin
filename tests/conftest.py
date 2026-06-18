"""Shared fixtures for the Leptin test suite."""

from __future__ import annotations

import os
import sys

import pytest

# Make ``src/`` importable without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from leptin.api import Leptin  # noqa: E402
from leptin.config import Config  # noqa: E402


class Clock:
    """A controllable clock for deterministic decay tests."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance_days(self, days: float) -> None:
        self.t += days * 86400.0

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mem(clock):
    m = Leptin(":memory:", config=Config(), clock=clock)
    yield m
    m.close()


@pytest.fixture
def make_mem(clock):
    created = []

    def _make(config=None):
        m = Leptin(":memory:", config=config or Config(), clock=clock)
        created.append(m)
        return m

    yield _make
    for m in created:
        m.close()
