"""Shared pytest fixtures."""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# web_app creates its analytics store during import, before per-test fixtures
# can replace it. Keep that import-time database out of the working tree.
_TEST_DATABASE_DIR = tempfile.TemporaryDirectory(prefix="flights-agent-tests-")
os.environ["ANALYTICS_DB_PATH"] = str(Path(_TEST_DATABASE_DIR.name) / "analytics.sqlite3")

from config import config


def pytest_unconfigure(config):
    """Remove the shared import-time test database after the test session."""
    _TEST_DATABASE_DIR.cleanup()


@pytest.fixture
def sample_travel_date() -> str:
    """Future ISO date for search tests (UTC today + 30 days)."""
    return (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()


@pytest.fixture
def sample_travel_date_display(sample_travel_date: str) -> str:
    """Month day string matching sample_travel_date for NLU tests."""
    d = datetime.fromisoformat(sample_travel_date)
    return d.strftime("%B %d").replace(" 0", " ")


@pytest.fixture(autouse=True)
def disable_live_nim_calls(monkeypatch):
    """Keep the automated suite independent of external NVIDIA credentials/services."""
    monkeypatch.setattr(config, "nvidia_api_key", "")
