"""Tests for retention: the RetentionJob, the supervisor wiring and the Panel."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost
from sqlalchemy import Engine, func, insert, select

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.retention import RetentionJob
from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.app.stores import Stores
from plc_data_collector.app.supervisor import RuntimeSupervisor
from plc_data_collector.domain import (
    CollectorError,
    ConnectionConfig,
    StorageError,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.persistence import RetentionSettings
from plc_data_collector.persistence.schema import (
    api_keys,
    app_settings,
    connections,
    poll_executions,
    tag_groups,
    tag_readings,
    tags,
)

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
CROSS_SITE = {"Origin": "https://evil.example"}
DAYS_ERROR = "Enter a number of days from 1 to 3650."


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _seed_config(stores: Stores) -> None:
    stores.config.replace_all(
        CollectorConfig(
            connections=(
                ConnectionConfig(
                    id="line1",
                    name="Line 1",
                    protocol="opcua",
                    endpoint="opc.tcp://plc:4840",
                    protocol_options={"password": "s3cret-pass"},
                ),
            ),
            tag_groups=(
                TagGroupConfig(id="fast", connection_id="line1", interval_ms=1000),
            ),
            tags=(
                TagConfig(
                    id="temp",
                    tag_group_id="fast",
                    name="Temperature",
                    address="ns=2;s=Temp",
                    value_type=ValueType.NUMERIC,
                ),
            ),
        )
    )


def _insert_readings(engine: Engine, times: list[datetime], prefix: str) -> None:
    rows = [
        {
            "event_id": f"{prefix}-{index}",
            "connection_id": "line1",
            "tag_group_id": "fast",
            "tag_id": "temp",
            "source_timestamp": at,
            "received_at": at,
            "quality": "good",
            "value_type": "numeric",
            "numeric_value": float(index),
        }
        for index, at in enumerate(times)
    ]
    with engine.begin() as connection:
        connection.execute(insert(tag_readings), rows)


def _insert_executions(engine: Engine, times: list[datetime], prefix: str) -> None:
    rows = [
        {
            "execution_id": f"{prefix}-{index}",
            "connection_id": "line1",
            "tag_group_id": "fast",
            "status": "succeeded",
            "started_at": at,
            "finished_at": at,
            "requested_tags": 1,
            "successful_tags": 1,
            "failed_tags": 0,
        }
        for index, at in enumerate(times)
    ]
    with engine.begin() as connection:
        connection.execute(insert(poll_executions), rows)


def _count(engine: Engine, table: Any) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(select(func.count()).select_from(table)).scalar_one()
        )


def _seed_mixed(host: FakeHost) -> None:
    """Five old and three recent readings and executions (cutoff: 30 days)."""
    _seed_config(_stores(host))
    old = [NOW - timedelta(days=31 + i) for i in range(5)]
    recent = [NOW - timedelta(days=29), NOW - timedelta(hours=1), NOW]
    _insert_readings(host.engine, old, "old")
    _insert_readings(host.engine, recent, "new")
    _insert_executions(host.engine, old, "old")
    _insert_executions(host.engine, recent, "new")


def _job(host: FakeHost, **kwargs: Any) -> RetentionJob:
    return RetentionJob(_stores(host), clock=lambda: NOW, **kwargs)


async def _wait_until(condition: Callable[[], bool], timeout_s: float = 10.0) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout_s)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)


# RetentionJob


def test_disabled_deletes_nothing(fake_host: FakeHost) -> None:
    _seed_mixed(fake_host)

    assert _job(fake_host).run_once() == 0
    assert _count(fake_host.engine, tag_readings) == 8
    assert _count(fake_host.engine, poll_executions) == 8


def test_enabled_deletes_only_older_readings_and_executions(
    fake_host: FakeHost, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_mixed(fake_host)
    _stores(fake_host).settings.set_retention(RetentionSettings(enabled=True, days=30))
    caplog.set_level(logging.INFO, logger="plc_data_collector.app.retention")

    assert _job(fake_host).run_once() == 5

    with fake_host.engine.connect() as connection:
        readings = set(connection.scalars(select(tag_readings.c.event_id)))
        executions = set(connection.scalars(select(poll_executions.c.execution_id)))
    assert readings == {"new-0", "new-1", "new-2"}
    assert executions == {"new-0", "new-1", "new-2"}
    [record] = [r for r in caplog.records if r.getMessage() == "retention_deleted"]
    assert record.__dict__["readings"] == 5
    assert record.__dict__["poll_executions"] == 5
    assert record.__dict__["days"] == 30


def test_batches_cover_more_than_batch_size(fake_host: FakeHost) -> None:
    _seed_config(_stores(fake_host))
    old = NOW - timedelta(days=40)
    times = [old - timedelta(seconds=i) for i in range(25_000)]
    _insert_readings(fake_host.engine, times, "old")
    _insert_executions(fake_host.engine, times, "old")
    _insert_readings(fake_host.engine, [NOW], "new")
    _stores(fake_host).settings.set_retention(RetentionSettings(enabled=True, days=30))

    assert _job(fake_host, batch_size=10_000).run_once() == 25_000
    assert _count(fake_host.engine, tag_readings) == 1
    assert _count(fake_host.engine, poll_executions) == 0


def test_delete_poll_executions_stops_between_batches(fake_host: FakeHost) -> None:
    _seed_config(_stores(fake_host))
    times = [NOW - timedelta(days=40, seconds=i) for i in range(10)]
    _insert_executions(fake_host.engine, times, "old")
    readings = _stores(fake_host).readings
    calls = 0

    def stop_after_first() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    deleted = readings.delete_poll_executions_older_than(
        NOW, batch_size=4, should_stop=stop_after_first
    )
    assert deleted == 4
    assert _count(fake_host.engine, poll_executions) == 6
    with pytest.raises(ValueError):
        readings.delete_poll_executions_older_than(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        readings.delete_poll_executions_older_than(NOW, batch_size=0)


async def test_failure_keeps_loop_running(
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_mixed(fake_host)
    settings = _stores(fake_host).settings
    settings.set_retention(RetentionSettings(enabled=True, days=30))
    original = settings.retention
    calls = 0

    def flaky() -> RetentionSettings:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StorageError("Database is gone.", code="database_unavailable")
        return original()

    monkeypatch.setattr(settings, "retention", flaky)
    caplog.set_level(logging.INFO, logger="plc_data_collector.app.retention")
    job = _job(fake_host, interval_s=0.01, first_run_delay_s=0.0)
    task = asyncio.create_task(job.run())
    try:
        await _wait_until(lambda: _count(fake_host.engine, tag_readings) == 3)
    finally:
        await _cancel(task)

    messages = [record.getMessage() for record in caplog.records]
    assert "retention_failed" in messages
    assert "retention_deleted" in messages
    [failed] = [r for r in caplog.records if r.getMessage() == "retention_failed"]
    assert failed.__dict__["error_code"] == "database_unavailable"
    assert "s3cret" not in caplog.text


async def test_trigger_runs_before_the_interval(fake_host: FakeHost) -> None:
    _seed_mixed(fake_host)
    _stores(fake_host).settings.set_retention(RetentionSettings(enabled=True, days=30))
    job = _job(fake_host, interval_s=3600.0, first_run_delay_s=3600.0)
    task = asyncio.create_task(job.run())
    try:
        await asyncio.sleep(0)
        assert _count(fake_host.engine, tag_readings) == 8
        job.trigger()
        await _wait_until(lambda: _count(fake_host.engine, tag_readings) == 3)
    finally:
        await _cancel(task)


def test_retention_never_touches_configuration(fake_host: FakeHost) -> None:
    _seed_mixed(fake_host)
    stores = _stores(fake_host)
    stores.api_keys.create("Historian")
    stores.settings.set_retention(RetentionSettings(enabled=True, days=1))
    configuration = (connections, tag_groups, tags, app_settings, api_keys)
    before = {table.name: _count(fake_host.engine, table) for table in configuration}
    revision = stores.config.revision()

    # A clock far in the future makes every timestamp in the database "old".
    job = RetentionJob(stores, clock=lambda: NOW + timedelta(days=10_000))
    assert job.run_once() == 8

    after = {table.name: _count(fake_host.engine, table) for table in configuration}
    assert after == before
    assert all(count > 0 for count in after.values())
    assert stores.config.revision() == revision
    assert stores.settings.retention() == RetentionSettings(enabled=True, days=1)


# Supervisor wiring


def _supervisor_settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings.from_env(
        {},
        data_dir=tmp_path / "data",
        database_wait_timeout_s=2.0,
        writer_batch_size=2,
        writer_flush_interval_s=0.05,
        storage_timeout_s=1.0,
    )


async def test_supervisor_runs_retention_across_applies_and_stops_it(
    tmp_path: Path,
) -> None:
    jobs: list[RetentionJob] = []

    def factory(stores: Stores) -> RetentionJob:
        job = RetentionJob(stores, interval_s=3600.0, first_run_delay_s=3600.0)
        jobs.append(job)
        return job

    supervisor = RuntimeSupervisor(
        _supervisor_settings(tmp_path), retention_job_factory=factory
    )
    supervisor.trigger_retention()  # before start: nothing to trigger
    await asyncio.wait_for(supervisor.start(), timeout=10)
    try:
        assert len(jobs) == 1
        task = supervisor.retention_task
        assert task is not None and not task.done()
        await asyncio.wait_for(supervisor.apply(), timeout=10)
        assert supervisor.retention_task is task and not task.done()
        runs = jobs[0].runs
        supervisor.trigger_retention()
        await _wait_until(lambda: jobs[0].runs == runs + 1)
    finally:
        await asyncio.wait_for(supervisor.stop(), timeout=10)
    assert task.done()
    assert supervisor.retention_task is None
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not current] == []


async def test_supervisor_stop_during_start_leaves_no_retention_task(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowRuntime:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def start(self) -> None:
            started.set()
            await release.wait()

        async def stop(self, timeout_s: float) -> None:
            return None

    supervisor = RuntimeSupervisor(
        _supervisor_settings(tmp_path),
        runtime_factory=SlowRuntime,  # type: ignore[arg-type]
    )
    start = asyncio.create_task(supervisor.start())
    await asyncio.wait_for(started.wait(), timeout=10)
    assert supervisor.retention_task is not None
    await asyncio.wait_for(supervisor.stop(), timeout=10)
    with pytest.raises(CollectorError) as aborted:
        await asyncio.wait_for(start, timeout=10)
    assert aborted.value.code == "runtime_start_aborted"
    assert supervisor.retention_task is None
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not current] == []


# Panel


def test_panel_preview_count_and_validation(
    signed_in_client: TestClient, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plc_data_collector.panel import routes_settings

    monkeypatch.setattr(routes_settings, "utc_now", lambda: NOW)
    _seed_config(_stores(fake_host))
    _insert_readings(
        fake_host.engine,
        [NOW - timedelta(days=40, seconds=i) for i in range(1234)],
        "old",
    )
    _insert_readings(fake_host.engine, [NOW], "new")

    def preview(days: str) -> str:
        response = signed_in_client.get(
            "/panel/settings/retention/preview", params={"days": days}
        )
        assert response.status_code == 200
        return str(response.text)

    assert (
        "About 1,234 readings older than 30 days will be deleted after you save."
        in preview("30")
    )
    assert "No readings are older than 50 days." in preview("50")
    for bad in ("0", "3651", "-1", "abc", "1.5", "", " ", "1e3", "٣"):
        assert DAYS_ERROR in preview(bad), bad
    assert "older than 3650 days" in preview("3650")

    page = signed_in_client.get("/panel/settings")
    assert page.status_code == 200
    for text in (
        "Delete old readings automatically to keep the database small. "
        "Connections, tags and settings are never deleted.",
        "Delete old readings",
        "Keep readings for",
        "Readings are kept until you delete them yourself.",
        "Save retention",
        'hx-get="/panel/settings/retention/preview"',
        'hx-trigger="input changed delay:500ms"',
        "About 1,234 readings older than 30 days will be deleted after you save.",
    ):
        assert text in page.text, text


def test_panel_preview_requires_sign_in(panel_client: TestClient) -> None:
    response = panel_client.get("/panel/settings/retention/preview?days=30")
    assert response.status_code == 303


def test_panel_save_persists_setting(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _stores(fake_host)

    response = signed_in_client.post(
        "/panel/settings/retention", data={"enabled": "on", "days": "14"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/panel/settings"
    assert stores.settings.retention() == RetentionSettings(enabled=True, days=14)
    assert fake_host.retention_triggers == 1
    page = signed_in_client.get("/panel/settings")
    assert "Retention saved." in page.text
    assert 'value="14"' in page.text
    assert 'id="field-retention-enabled" checked' in page.text

    response = signed_in_client.post("/panel/settings/retention", data={"days": "14"})
    assert response.status_code == 303
    assert stores.settings.retention() == RetentionSettings(enabled=False, days=14)
    assert fake_host.retention_triggers == 2

    # Turned off with a bad number: the setting is off, the old days stay.
    response = signed_in_client.post("/panel/settings/retention", data={"days": "x"})
    assert response.status_code == 303
    assert stores.settings.retention() == RetentionSettings(enabled=False, days=14)

    for bad in ("0", "3651", "abc", ""):
        response = signed_in_client.post(
            "/panel/settings/retention", data={"enabled": "on", "days": bad}
        )
        assert response.status_code == 400, bad
        assert DAYS_ERROR in response.text
        assert stores.settings.retention() == RetentionSettings(enabled=False, days=14)
    assert fake_host.retention_triggers == 3


def test_panel_cross_site_save_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    response = signed_in_client.post(
        "/panel/settings/retention",
        data={"enabled": "on", "days": "7"},
        headers=CROSS_SITE,
    )
    assert response.status_code == 403
    assert _stores(fake_host).settings.retention() == RetentionSettings()
    assert fake_host.retention_triggers == 0
