"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest

TEST_DATABASE_URL_ENV = "PLC_DATA_COLLECTOR_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """Return the PostgreSQL test URL or skip when it is not configured."""
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip("PostgreSQL not configured")
    return url
