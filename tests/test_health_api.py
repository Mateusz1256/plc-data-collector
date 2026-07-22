from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from plc_gateway.api import RuntimeApiState, create_api_app
from plc_gateway.domain import RuntimeComponentStatus, WorkerState
from plc_gateway.persistence import DatabaseWriterMetrics, SpoolMetrics
from plc_gateway.runtime import ReadingQueueMetrics, WorkerRuntimeMetrics


def timestamp() -> datetime:
    return datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def make_status(
    component_id: str,
    state: WorkerState = WorkerState.RUNNING,
) -> RuntimeComponentStatus:
    return RuntimeComponentStatus(
        component_id=component_id,
        state=state,
        updated_at=timestamp(),
        message="ok",
    )


def test_health_live_is_independent_from_runtime_failures() -> None:
    client = TestClient(
        create_api_app(
            RuntimeApiState(
                configuration_loaded=False,
                storage_available=False,
                critical_errors=("database unavailable",),
            )
        )
    )

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "live"


def test_health_ready_reports_critical_failures() -> None:
    client = TestClient(
        create_api_app(
            RuntimeApiState(
                configuration_loaded=True,
                storage_available=False,
                critical_errors=("database unavailable",),
            )
        )
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "ready": False,
        "degraded": False,
        "configuration_loaded": True,
        "storage_available": False,
        "critical_errors": ["database unavailable"],
    }


def test_health_ready_allows_single_degraded_worker() -> None:
    client = TestClient(
        create_api_app(
            RuntimeApiState(
                workers=(make_status("connection-worker:mock", WorkerState.DEGRADED),)
            )
        )
    )

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["degraded"] is True


def test_runtime_components_returns_status_and_metrics_without_secrets() -> None:
    client = TestClient(
        create_api_app(
            RuntimeApiState(
                components=(make_status("database-writer"),),
                worker_metrics=(
                    WorkerRuntimeMetrics(
                        component_id="connection-worker:mock",
                        connect_attempts=2,
                        connect_successes=1,
                        connect_failures=0,
                        connect_retry_attempts=1,
                        poll_attempts=3,
                        poll_successes=3,
                        poll_failures=0,
                        poll_retry_attempts=1,
                        tag_successes=6,
                        tag_failures=0,
                        last_poll_correlation_id="poll-correlation",
                        last_reconnect_correlation_id="connect-correlation",
                    ),
                ),
                queue_metrics=ReadingQueueMetrics(
                    max_size=10,
                    size=2,
                    occupancy_ratio=0.2,
                    closed=False,
                    dropped_items=0,
                    put_timeouts=1,
                ),
                writer_metrics=DatabaseWriterMetrics(
                    running=True,
                    successful_batches=3,
                    failed_batches=0,
                    successful_poll_results=5,
                    failed_poll_results=0,
                    inserted_readings=5,
                    retry_attempts=1,
                    pending_batch_size=0,
                ),
                spool_metrics=SpoolMetrics(
                    max_items=100,
                    stored_items=2,
                    full=False,
                    rejected_items=0,
                    replayed_items=1,
                    failed_replays=0,
                ),
            )
        )
    )

    response = client.get("/api/runtime/components")

    assert response.status_code == 200
    payload = response.json()
    assert payload["components"][0]["component_id"] == "database-writer"
    assert payload["worker_metrics"][0]["connect_retry_attempts"] == 1
    assert payload["queue"]["size"] == 2
    assert payload["writer"]["successful_batches"] == 3
    assert payload["spool"]["stored_items"] == 2
    assert "secret" not in str(payload).lower()


def test_runtime_workers_returns_worker_statuses() -> None:
    client = TestClient(
        create_api_app(
            RuntimeApiState(
                workers=(make_status("connection-worker:mock"),),
                worker_metrics=(
                    WorkerRuntimeMetrics(
                        component_id="connection-worker:mock",
                        connect_attempts=1,
                        connect_successes=1,
                        connect_failures=0,
                        connect_retry_attempts=0,
                        poll_attempts=0,
                        poll_successes=0,
                        poll_failures=0,
                        poll_retry_attempts=0,
                        tag_successes=0,
                        tag_failures=0,
                    ),
                ),
            )
        )
    )

    response = client.get("/api/runtime/workers")

    assert response.status_code == 200
    assert response.json()["workers"][0]["state"] == "running"
    assert response.json()["metrics"][0]["connect_successes"] == 1


def test_about_returns_version_build_and_license_information() -> None:
    client = TestClient(create_api_app())

    response = client.get("/api/about")

    assert response.status_code == 200
    payload = response.json()
    assert payload["application"] == "plc-gateway"
    assert payload["version"] == "0.1.0"
    assert payload["build"]["version"] == "0.1.0"
    assert payload["license"]["spdx_id"] == "LicenseRef-PLC-Gateway-Proprietary"
    assert payload["third_party_notices_file"] == "THIRD_PARTY_NOTICES.md"
    assert payload["dependency_count"] > 0
