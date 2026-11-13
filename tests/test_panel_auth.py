"""Tests for the Panel shell: mounting, admin sign-in, throttling and layout."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost

from plc_data_collector.panel.auth import LoginThrottle

WRONG_PASSWORD_COPY = (
    "That password doesn't match. Check "
    '<span class="addr">PLC_DATA_COLLECTOR_ADMIN_PASSWORD</span> '
    "in the server's .env file."
)
PANEL_OFF_COPY = (
    "The Panel is turned off. Set "
    '<span class="addr">PLC_DATA_COLLECTOR_ADMIN_PASSWORD</span> '
    "in the server's .env file and restart the collector."
)
THROTTLED_COPY = "Too many failed sign-ins. Wait 5 minutes and try again."


def _text(html: str) -> str:
    """Return the page text with tags removed and entities for quotes decoded."""
    without_tags = re.sub(r"<[^>]+>", "", html)
    return without_tags.replace("&#39;", "'").replace("&amp;", "&")


def test_root_redirects_to_live(panel_client: TestClient) -> None:
    response = panel_client.get("/")

    assert response.status_code in (303, 307)
    assert response.headers["location"] == "/panel/live"


def test_panel_requires_login(panel_client: TestClient) -> None:
    response = panel_client.get("/panel/live")

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/login?next=/panel/live"


def test_htmx_request_without_session_gets_hx_redirect(
    panel_client: TestClient,
) -> None:
    response = panel_client.get("/panel/live", headers={"HX-Request": "true"})

    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/panel/login?next=/panel/live"
    assert "location" not in response.headers


def test_login_page_shows_mockup_copy(panel_client: TestClient) -> None:
    response = panel_client.get("/panel/login?next=/panel/live")

    assert response.status_code == 200
    text = _text(response.text)
    assert "PLC Data Collector" in text
    assert "Sign in to manage connections and see live tags." in text
    assert "Admin password" in text
    assert 'type="password"' in response.text
    assert 'autocomplete="current-password"' in response.text
    assert 'name="next" value="/panel/live"' in response.text
    assert "/panel/static/panel.css" in response.text


def test_login_success_sets_session_and_redirects_to_next(
    panel_client: TestClient,
) -> None:
    response = panel_client.post(
        "/panel/login", data={"password": "pw", "next": "/panel/live"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/live"
    assert "pdc_session" in panel_client.cookies
    live = panel_client.get("/panel/live")
    assert live.status_code == 200
    assert "Live tags" in live.text


def test_login_ignores_next_outside_the_panel(panel_client: TestClient) -> None:
    for unsafe in ("https://evil.example/", "//evil.example/panel/", "/api/about"):
        panel_client.cookies.clear()
        response = panel_client.post(
            "/panel/login", data={"password": "pw", "next": unsafe}
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/panel/live"


def test_login_wrong_password_shows_mockup_error(
    panel_client: TestClient, login_sleeps: list[float]
) -> None:
    response = panel_client.post(
        "/panel/login", data={"password": "nope", "next": "/panel/live"}
    )

    assert response.status_code == 401
    assert WRONG_PASSWORD_COPY in response.text.replace("&#39;", "'")
    assert 'role="alert"' in response.text
    assert "pdc_session" not in panel_client.cookies
    assert login_sleeps == [1.0]
    # The submitted password is never echoed back.
    assert "nope" not in response.text


def test_login_throttled_after_five_failures(
    panel_client: TestClient, login_sleeps: list[float]
) -> None:
    for _ in range(5):
        response = panel_client.post("/panel/login", data={"password": "nope"})
        assert response.status_code == 401

    throttled = panel_client.post("/panel/login", data={"password": "pw"})

    assert throttled.status_code == 429
    assert THROTTLED_COPY in throttled.text
    assert "pdc_session" not in panel_client.cookies
    assert login_sleeps == [1.0] * 5


def test_login_throttle_window_expires() -> None:
    now = [1000.0]
    throttle = LoginThrottle(max_failures=5, window_s=300, clock=lambda: now[0])
    for _ in range(5):
        throttle.record_failure("10.0.0.1")

    assert throttle.blocked("10.0.0.1")
    assert not throttle.blocked("10.0.0.2")
    now[0] += 300.5
    assert not throttle.blocked("10.0.0.1")


def test_login_throttle_resets_after_success() -> None:
    throttle = LoginThrottle(max_failures=5, window_s=300, clock=lambda: 0.0)
    for _ in range(4):
        throttle.record_failure("10.0.0.1")

    throttle.reset("10.0.0.1")
    throttle.record_failure("10.0.0.1")

    assert not throttle.blocked("10.0.0.1")


@pytest.mark.parametrize("panel_admin_password", [None])
def test_panel_off_without_admin_password(
    panel_client: TestClient, login_sleeps: list[float]
) -> None:
    page = panel_client.get("/panel/login")

    assert page.status_code == 200
    assert PANEL_OFF_COPY in page.text.replace("&#39;", "'")
    assert 'type="password"' not in page.text

    refused = panel_client.post("/panel/login", data={"password": ""})

    assert refused.status_code == 403
    assert PANEL_OFF_COPY in refused.text.replace("&#39;", "'")
    assert "pdc_session" not in panel_client.cookies
    assert login_sleeps == []
    assert panel_client.get("/panel/live").status_code == 303


def test_sign_out_clears_session(signed_in_client: TestClient) -> None:
    live = signed_in_client.get("/panel/live")
    assert live.status_code == 200
    assert "Sign out" in live.text
    assert 'action="/panel/sign-out"' in live.text

    response = signed_in_client.post("/panel/sign-out")

    assert response.status_code == 303
    assert response.headers["location"] == "/panel/login"
    assert "pdc_session" not in signed_in_client.cookies
    assert signed_in_client.get("/panel/live").status_code == 303


def test_session_from_another_password_is_rejected(
    fake_host: FakeHost, login_sleeps: list[float]
) -> None:
    from panel_support import PANEL_SESSION_SECRET

    from plc_data_collector.api import WebOptions, create_api_app

    def client_for(password: str) -> TestClient:
        return TestClient(
            create_api_app(
                fake_host.api_state,
                web=WebOptions(
                    host=fake_host,
                    admin_password=password,
                    session_secret=PANEL_SESSION_SECRET,
                ),
            ),
            follow_redirects=False,
        )

    with client_for("old") as old_client:
        assert (
            old_client.post("/panel/login", data={"password": "old"}).status_code == 303
        )
        cookie = old_client.cookies["pdc_session"]
    with client_for("new") as new_client:
        new_client.cookies.set("pdc_session", cookie)

        assert new_client.get("/panel/live").status_code == 303


def test_cookie_is_samesite_strict_httponly(panel_client: TestClient) -> None:
    response = panel_client.post("/panel/login", data={"password": "pw"})

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("pdc_session=")
    assert "samesite=strict" in cookie.lower()
    assert "httponly" in cookie.lower()
    assert "max-age=43200" in cookie.lower()
    assert "path=/" in cookie.lower()


def test_panel_503_while_database_not_ready(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    fake_host.stores = None

    response = signed_in_client.get("/panel/live")

    assert response.status_code == 503
    assert "Starting: waiting for the database." in response.text
    # Same layout, without the navigation and sign-out actions.
    assert "PLC Data Collector" in response.text
    assert 'aria-label="Main"' not in response.text
    assert "Sign out" not in response.text


def test_signed_in_layout_has_nav_with_current_item(
    signed_in_client: TestClient,
) -> None:
    html = signed_in_client.get("/panel/live").text

    assert 'aria-label="Main"' in html
    assert re.search(r'<a href="/panel/live"[^>]*aria-current="page"', html)
    assert '<a href="/panel/connections"' in html
    assert '<a href="/panel/settings"' in html
    assert ">admin<" in html
    assert "/panel/static/htmx.min.js" in html
    assert "fonts.googleapis.com" in html


def test_static_files_are_served(panel_client: TestClient) -> None:
    css = panel_client.get("/panel/static/panel.css")
    js = panel_client.get("/panel/static/htmx.min.js")

    assert css.status_code == 200
    assert "--action: #2457a6" in css.text.lower()
    assert "prefers-reduced-motion" in css.text
    assert js.status_code == 200
    assert "htmx 2." in js.text.splitlines()[0]


def test_health_still_open_without_session(panel_client: TestClient) -> None:
    assert panel_client.get("/health/live").status_code == 200
    assert panel_client.get("/health/ready").status_code == 200
    assert panel_client.get("/api/runtime/components").status_code == 200
    assert panel_client.get("/api/about").status_code == 200


def test_existing_api_unchanged_without_web() -> None:
    from plc_data_collector.api import create_api_app

    app = create_api_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert not any(str(path).startswith("/panel") for path in paths)
    assert "/" not in paths
    client = TestClient(app, follow_redirects=False)
    assert client.get("/").status_code == 404
    assert client.get("/panel/live").status_code == 404

    # tests/test_health_api.py covers the rest of the 0.2.0 API and is untouched.


def test_state_marker_macro_renders_shape_and_word() -> None:
    from plc_data_collector.panel.rendering import ENVIRONMENT, state_badge

    macro = ENVIRONMENT.get_template("_state.html").module.state_marker  # type: ignore[attr-defined]

    running = str(macro(state_badge("running")))
    stale = str(macro(state_badge("stale")))
    bad = str(macro(state_badge("bad")))
    disabled = str(macro(state_badge("disabled")))

    assert 'class="state state-normal"' in running and "<circle" in running
    assert "Running" in running
    assert "state-degraded" in stale and "<path" in stale and "Stale" in stale
    assert "state-failed" in bad and "<rect" in bad and "Bad" in bad
    assert "state-disabled" in disabled and "Disabled" in disabled
    assert state_badge("something-new").kind == "failed"
