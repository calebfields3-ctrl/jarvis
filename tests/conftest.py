"""Shared fixtures. Every test gets an isolated JARVIS_HOME and a synthetic market."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.config import Config, reset_config  # noqa: E402
from jarvis.market.provider import SyntheticProvider  # noqa: E402
from jarvis.memory.store import Memory  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "jarvis-home"
    monkeypatch.setenv("JARVIS_HOME", str(path))
    reset_config()
    yield path
    reset_config()


@pytest.fixture
def config(home) -> Config:
    return Config(home=home)


@pytest.fixture
def memory(home) -> Memory:
    mem = Memory(home / "test.db")
    yield mem
    mem.close()


@pytest.fixture
def provider() -> SyntheticProvider:
    return SyntheticProvider(seed=42)


@pytest.fixture
def jarvis(config, provider):
    from jarvis.brain import Jarvis

    bot = Jarvis(config, provider=provider)
    yield bot
    bot.memory.close()
