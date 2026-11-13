"""Shared Panel test support: a scripted ``CollectorHost`` and client fixtures.

Loaded as a pytest plugin from ``conftest.py``, so its fixtures are available
to every test module.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from plc_data_collector.api import RuntimeApiState, WebOptions, create_api_app
from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.secrets import SecretBox
from plc_data_collector.app.stores import Stores, build_stores
from plc_data_collector.domain import CollectorError
from plc_data_collector.panel import auth
from plc_data_collector.panel.host import CollectorHost
from plc_data_collector.persistence import create_database_engine, run_migrations
from plc_data_collector.protocols import DriverRegistry, default_driver_registry
from plc_data_collector.runtime.live_values import LiveValueCache

PANEL_SESSION_SECRET = b"panel-test-session-secret-32byte"


class FakeHost:
    """``CollectorHost`` over a migrated SQLite database with a scripted apply().

    Set ``stores`` to None to simulate a database that is not ready yet, and
    ``apply_error`` to make the next ``apply()`` calls raise it. With
    ``apply_falls_back`` a failed apply keeps the previous configuration
    running, as the supervisor's rollback does; without it collection stops.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.stores: Stores | None = build_stores(
            engine, SecretBox(Fernet.generate_key())
        )
        self.live_values = LiveValueCache()
        self.running_revision: int | None = None
        self.running_config: CollectorConfig | None = None
        self.last_error_code: str | None = None
        self.applying = False
        self.registry: DriverRegistry = default_driver_registry()
        self.runtime_state = RuntimeApiState()
        self.apply_calls = 0
        self.apply_error: BaseException | None = None
        self.apply_falls_back = False
        self.retention_triggers = 0

    def api_state(self) -> RuntimeApiState:
        """Return the scripted runtime snapshot."""
        return self.runtime_state

    def trigger_retention(self) -> None:
        """Count the request; retention itself is tested in test_retention."""
        self.retention_triggers += 1

    async def apply(self) -> None:
        """Record the call, then fail as scripted or adopt the stored config."""
        self.apply_calls += 1
        if self.apply_error is not None:
            if not self.apply_falls_back:
                self.running_revision = None
                self.running_config = None
            if isinstance(self.apply_error, CollectorError):
                self.last_error_code = self.apply_error.code
            raise self.apply_error
        if self.stores is None:
            raise CollectorError(
                "The collector is not running.", code="runtime_not_running"
            )
        config, revision = self.stores.config.load_active()
        self.running_config = config
        self.running_revision = revision
        self.last_error_code = None


# mypy checks that the fake really is a CollectorHost.
_HOST_TYPE_CHECK: type[CollectorHost] = FakeHost


@pytest.fixture
def fake_host(tmp_path: Path) -> Iterator[FakeHost]:
    """Return a FakeHost over a fresh migrated SQLite database."""
    url = f"sqlite:///{tmp_path / 'panel.db'}"
    run_migrations(url)
    engine = create_database_engine(url)
    try:
        yield FakeHost(engine)
    finally:
        engine.dispose()


@pytest.fixture
def panel_admin_password() -> str | None:
    """Return the admin password; parametrize this name to override it."""
    return "pw"


@pytest.fixture
def login_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the failed sign-in delay with a recorder, so tests stay fast."""
    sleeps: list[float] = []

    async def record(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(auth, "failure_sleep", record)
    return sleeps


@pytest.fixture
def panel_client(
    fake_host: FakeHost,
    panel_admin_password: str | None,
    login_sleeps: list[float],
) -> Iterator[TestClient]:
    """Return a client for an app with the Panel mounted; not signed in."""
    app = create_api_app(
        fake_host.api_state,
        web=WebOptions(
            host=fake_host,
            admin_password=panel_admin_password,
            session_secret=PANEL_SESSION_SECRET,
        ),
    )
    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def signed_in_client(
    panel_client: TestClient, panel_admin_password: str | None
) -> TestClient:
    """Return ``panel_client`` after a successful sign-in."""
    assert panel_admin_password is not None
    response = panel_client.post(
        "/panel/login", data={"password": panel_admin_password}
    )
    assert response.status_code == 303, response.text
    return panel_client
