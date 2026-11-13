"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest

# Panel fixtures (fake_host, panel_client, signed_in_client) live in panel_support.
pytest_plugins = ["panel_support"]

TEST_DATABASE_URL_ENV = "PLC_DATA_COLLECTOR_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """Return the PostgreSQL test URL or skip when it is not configured."""
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip("PostgreSQL not configured")
    return url
