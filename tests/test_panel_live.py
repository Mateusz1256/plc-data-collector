"""Tests for the Panel's Live tags page, pending-changes banner and Apply."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost

from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    CollectorError,
    ConnectionConfig,
    PollExecution,
    PollStatus,
    Quality,
    TagConfig,
    TagGroupConfig,
    TagResult,
    TagValue,
    ValueType,
)
from plc_data_collector.panel import routes_live
from plc_data_collector.panel.routes_live import (
    format_value,
    freshness_bar,
    pending_changes,
)
from plc_data_collector.runtime.connection_worker import WorkerPollResult

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
CROSS_SITE = {"Origin": "https://evil.example"}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_live, "utc_now", lambda: NOW)


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _connection(
    connection_id: str = "line1_opcua",
    *,
    name: str = "Line 1 filler",
    enabled: bool = True,
) -> ConnectionConfig:
    return ConnectionConfig(
        id=connection_id,
        name=name,
        protocol="opcua",
        endpoint="opc.tcp://plc-sim:4840",
        enabled=enabled,
    )


def _tag(
    tag_id: str,
    group_id: str = "line1_fast",
    *,
    name: str | None = None,
    unit: str | None = None,
    value_type: ValueType = ValueType.NUMERIC,
) -> TagConfig:
    return TagConfig(
        id=tag_id,
        tag_group_id=group_id,
        name=name or tag_id.replace("_", " ").capitalize(),
        address=f"ns=2;s=Line1.{tag_id}",
        value_type=value_type,
        metadata={"unit": unit} if unit else {},
    )


def _seed(host: FakeHost) -> Stores:
    """Two Connections; the second is disabled. Collection runs the saved config."""
    stores = _stores(host)
    stores.config.upsert_connection(_connection())
    stores.config.upsert_tag_group(
        TagGroupConfig(id="line1_fast", connection_id="line1_opcua", interval_ms=1000)
    )
    stores.config.upsert_tag_group(
        TagGroupConfig(id="line1_slow", connection_id="line1_opcua", interval_ms=60000)
    )
    stores.config.upsert_tag(_tag("temperature", unit="°C"))
    stores.config.upsert_tag(_tag("pressure", unit="bar"))
    stores.config.upsert_tag(_tag("energy_total", "line1_slow", unit="kWh"))
    stores.config.upsert_connection(
        _connection("packer_opcua", name="Packer", enabled=False)
    )
    stores.config.upsert_tag_group(
        TagGroupConfig(id="packer_fast", connection_id="packer_opcua", interval_ms=500)
    )
    stores.config.upsert_tag(_tag("packer_count", "packer_fast"))
    stores.config.upsert_tag(_tag("packer_state", "packer_fast"))
    _run_saved(host)
    return stores


def _run_saved(host: FakeHost) -> None:
    config, revision = _stores(host).config.load_active()
    host.running_config = config
    host.running_revision = revision


def _poll(
    host: FakeHost,
    group_id: str,
    values: dict[str, TagValue | None],
    *,
    age_s: float,
    connection_id: str = "line1_opcua",
    status: PollStatus = PollStatus.SUCCESS,
) -> None:
    received = NOW - timedelta(seconds=age_s)
    results = tuple(
        TagResult.success(
            tag_id=tag_id,
            value=value,
            source_timestamp=received,
            received_at=received,
        )
        if value is not None
        else TagResult(
            tag_id=tag_id,
            quality=Quality.BAD,
            received_at=received,
            error_code="opcua_bad_node_id",
            error_message="The node ID is not known.",
        )
        for tag_id, value in values.items()
    )
    host.live_values.update(
        WorkerPollResult(
            execution=PollExecution(
                execution_id=f"poll-{group_id}",
                connection_id=connection_id,
                tag_group_id=group_id,
                status=status,
                started_at=received - timedelta(milliseconds=10),
                finished_at=received,
                requested_tags=len(results),
            ),
            results=results,
        )
    )


def _tag_row(html: str, tag_id: str) -> str:
    match = re.search(
        rf'<tr[^>]*data-tag="{re.escape(tag_id)}"[^>]*>.*?</tr>', html, re.DOTALL
    )
    assert match is not None, tag_id
    return match.group(0)


def _section(html: str, connection_id: str) -> str:
    match = re.search(
        rf'<section[^>]*id="{re.escape(connection_id)}"[^>]*>.*?</section>',
        html,
        re.DOTALL,
    )
    assert match is not None, connection_id
    return match.group(0)


def _text(html: str) -> str:
    """Return the page text with tags removed, to match copy across markup."""
    return re.sub(r"<[^>]+>", "", html)


# Live tags


def test_live_groups_by_connection_and_group(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _poll(
        fake_host,
        "line1_fast",
        {"temperature": TagValue.numeric(71.234), "pressure": None},
        age_s=0.4,
    )

    response = signed_in_client.get("/panel/live")

    assert response.status_code == 200
    html = response.text
    assert "<h1" in html and "Live tags" in html
    assert (
        "Collecting 3 of 5 tags from 2 connections. Values refresh every second."
        in html
    )
    line1 = _section(html, "line1_opcua")
    assert "Line 1 filler" in line1
    assert "line1_fast" in line1 and "Every 1 s" in line1
    assert "Last poll 0.4 s ago, 1 of 2 tags good" in line1
    assert "line1_slow" in line1 and "Every 1 min" in line1
    assert line1.index("line1_fast") < line1.index("line1_slow")
    assert "ns=2;s=Line1.temperature" in _tag_row(line1, "temperature")
    packer = _section(html, "packer_opcua")
    assert "Not collecting. Its 2 tags are kept in the configuration." in packer
    assert 'action="/panel/connections/packer_opcua/enable"' in packer
    assert "Enable connection" in packer
    assert 'data-tag="packer_count"' not in packer
    assert html.index('id="line1_opcua"') < html.index('id="packer_opcua"')


def test_live_body_fragment_has_values_and_freshness_width(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _poll(
        fake_host,
        "line1_fast",
        {"temperature": TagValue.numeric(12345.678), "pressure": None},
        age_s=1.0,
    )

    response = signed_in_client.get("/panel/live/body", headers={"HX-Request": "true"})

    assert response.status_code == 200
    html = response.text
    assert "<html" not in html
    assert 'hx-get="/panel/live/body"' in html
    assert 'hx-trigger="every 1s"' in html
    row = _tag_row(html, "temperature")
    assert "12\u2009345.68" in row
    assert "°C" in row
    assert "Updated 1 s ago" in row
    # 1 s old at a 1 s interval: 1 - 1 / 4 = 75 % of the bar.
    assert re.search(r'class="bar"[^>]*style="width: 75%"', row)
    bad = _tag_row(html, "pressure")
    assert "—" in bad
    assert "state-failed" in bad and "Bad" in bad
    assert "opcua_bad_node_id" in bad


def test_live_body_requires_sign_in(panel_client: TestClient) -> None:
    response = panel_client.get("/panel/live/body", headers={"HX-Request": "true"})

    assert response.status_code == 401
    assert response.headers["HX-Redirect"].startswith("/panel/login")


def test_htmx_swaps_outer_html_by_default(signed_in_client: TestClient) -> None:
    html = signed_in_client.get("/panel/live").text

    assert '"defaultSwapStyle":"outerHTML"' in html


def test_stale_value_gets_amber_marker(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _poll(
        fake_host,
        "line1_fast",
        {"temperature": TagValue.numeric(1.0), "pressure": TagValue.numeric(2.0)},
        age_s=2.5,
    )
    _poll(fake_host, "line1_slow", {"energy_total": TagValue.numeric(3.0)}, age_s=0.2)

    html = signed_in_client.get("/panel/live/body").text

    stale = _tag_row(html, "temperature")
    assert "state-degraded" in stale and "Stale" in stale
    assert 'class="bar stale"' in stale
    fresh = _tag_row(html, "energy_total")
    assert "state-normal" in fresh and "Good" in fresh
    assert "stale" not in fresh.lower()
    assert "Updated under a second ago" in fresh


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (TagValue.numeric(3.14159), "3.14"),
        (TagValue.numeric(4812.3), "4\u2009812.30"),
        (TagValue.integer(1234567), "1\u2009234\u2009567"),
        (TagValue.integer(-1200), "-1\u2009200"),
        (TagValue.boolean(True), "True"),
        (TagValue.boolean(False), "False"),
        (TagValue.binary(b"\x00\x01\x02"), "3 bytes"),
        (TagValue.text("Running"), "Running"),
        (None, "—"),
    ],
)
def test_format_value(value: TagValue | None, expected: str) -> None:
    assert format_value(value) == expected


def test_freshness_inputs_are_clamped() -> None:
    # Clock skew: a reading from the future is fresh, never wider than the track.
    assert freshness_bar(-5.0, 1000) == (100, False)
    # An interval of 0 cannot come from the store, but must not crash the page.
    width, stale = freshness_bar(1.0, 0)
    assert 0 <= width <= 100
    assert stale is True
    assert freshness_bar(10.0, 1000) == (0, True)


# Banner


def test_banner_hidden_when_revision_matches(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    for path in ("/panel/live", "/panel/connections"):
        html = signed_in_client.get(path).text
        assert "not running yet" not in html, path
        assert 'action="/panel/apply"' not in html, path
    assert pending_changes(fake_host) is None


def test_banner_counts_changes(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("speed"))
    stores.config.archive_tag("pressure")

    for path in ("/panel/live", "/panel/connections", "/panel/connections/line1_opcua"):
        html = signed_in_client.get(path).text
        assert "2 changes are saved but not running yet." in html, path
        assert (
            "Applying restarts collection for a few seconds. "
            "Readings waiting to be stored are kept."
        ) in html
        assert 'href="/panel/changes"' in html and "Review changes" in html
        assert 'action="/panel/apply"' in html and "Apply changes" in html


def test_banner_wording_for_one_change_and_for_no_diff(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("speed"))
    assert "1 change is saved but not running yet." in (
        signed_in_client.get("/panel/live").text
    )

    stores.config.archive_tag("speed")

    html = signed_in_client.get("/panel/live").text
    assert "Saved changes are not running yet." in html


def test_banner_is_not_computed_for_htmx_fragments(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(fake_host)
    calls: list[object] = []
    monkeypatch.setattr(routes_live, "pending_changes", calls.append)

    signed_in_client.get("/panel/live/body", headers={"HX-Request": "true"})
    assert calls == []
    signed_in_client.get("/panel/live")
    assert calls == [fake_host]


def test_review_changes_lists_diff(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("speed", name="Line speed"))
    stores.config.upsert_tag(_tag("temperature", name="Oven temperature"))
    stores.config.archive_tag("pressure")

    response = signed_in_client.get("/panel/changes")

    assert response.status_code == 200
    html = response.text
    added = html.index("Added")
    changed = html.index("Changed")
    removed = html.index("Removed")
    assert added < html.index("Line speed") < changed
    assert changed < html.index("Oven temperature") < removed
    assert removed < html.index("Pressure")


def test_review_changes_with_nothing_pending(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    html = signed_in_client.get("/panel/changes").text

    assert "Nothing to apply. The saved configuration is running." in html


def test_banner_shows_stopped_collection(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    fake_host.running_revision = None
    fake_host.running_config = None
    fake_host.last_error_code = "database_unavailable"

    html = signed_in_client.get("/panel/connections").text

    assert (
        "Collection is stopped: database_unavailable. "
        "Fix the problem and apply changes again."
    ) in _text(html)
    assert 'action="/panel/apply"' in html
    assert "not running yet" not in html


def test_banner_hidden_while_applying(fake_host: FakeHost) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("speed"))
    fake_host.applying = True

    assert pending_changes(fake_host) is None


# Apply


def test_apply_calls_host_and_flashes(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(_tag("speed"))

    response = signed_in_client.post("/panel/apply")

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/live"
    assert fake_host.apply_calls == 1
    assert fake_host.running_revision == stores.config.revision()
    html = signed_in_client.get("/panel/live").text
    assert "Changes applied. Collection restarted." in html
    assert "not running yet" not in html


def test_apply_in_progress_message(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    revision = fake_host.running_revision
    fake_host.apply_error = CollectorError(
        "Changes are already being applied.", code="apply_in_progress"
    )

    response = signed_in_client.post("/panel/apply")

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/live"
    # The scripted fake drops the revision on any error; a real in-progress
    # apply leaves the runtime alone, so restore it for the page check.
    fake_host.running_revision = revision
    fake_host.last_error_code = None
    html = signed_in_client.get("/panel/live").text
    assert "Changes are already being applied. Wait a few seconds." in html


def test_apply_failure_shows_stopped_banner(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    fake_host.apply_error = CollectorError(
        "Database went away.", code="database_unavailable"
    )

    response = signed_in_client.post("/panel/apply")

    assert response.status_code == 303
    html = signed_in_client.get("/panel/live").text
    assert "Collection is stopped: database_unavailable." in html
    assert "Changes applied" not in html


def test_apply_requires_sign_in(panel_client: TestClient, fake_host: FakeHost) -> None:
    response = panel_client.post("/panel/apply")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/panel/login")
    assert fake_host.apply_calls == 0


# Enable connection


def test_enable_connection_from_live_bumps_revision(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    before = stores.config.revision()

    response = signed_in_client.post("/panel/connections/packer_opcua/enable")

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/live#packer_opcua"
    assert stores.config.revision() > before
    connection = stores.config.get_connection("packer_opcua")
    assert connection is not None and connection.enabled is True
    html = signed_in_client.get("/panel/live").text
    assert "1 change is saved but not running yet." in html


def test_enable_unknown_connection_is_not_found(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    response = signed_in_client.post("/panel/connections/nope/enable")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "path", ["/panel/apply", "/panel/connections/packer_opcua/enable"]
)
def test_cross_site_posts_are_refused(
    signed_in_client: TestClient, fake_host: FakeHost, path: str
) -> None:
    stores = _seed(fake_host)
    before = stores.config.revision()

    response = signed_in_client.post(path, headers=CROSS_SITE)

    assert response.status_code == 403
    assert fake_host.apply_calls == 0
    assert stores.config.revision() == before
