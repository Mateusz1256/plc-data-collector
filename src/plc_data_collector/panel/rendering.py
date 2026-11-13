"""Jinja environment, filters and the state marker helper for Panel pages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from plc_data_collector._version import get_version

StateKind = Literal["normal", "degraded", "failed", "disabled"]
NavItem = Literal["live", "connections", "settings"]


@dataclass(frozen=True, slots=True)
class StateBadge:
    """How one state is shown: a marker shape, a word and a CSS class.

    Colour appears only for abnormal states; every state also has its own
    marker shape and word, so nothing relies on hue alone.
    """

    kind: StateKind
    word: str

    @property
    def css_class(self) -> str:
        """Return the ``state-*`` class that colours the marker and word."""
        return f"state-{self.kind}"


_STATE_KINDS: Mapping[str, StateKind] = {
    "running": "normal",
    "good": "normal",
    "starting": "degraded",
    "stopping": "degraded",
    "degraded": "degraded",
    "stale": "degraded",
    "uncertain": "degraded",
    "failed": "failed",
    "bad": "failed",
    "disabled": "disabled",
    "stopped": "disabled",
}


def state_badge(state: str, word: str | None = None) -> StateBadge:
    """Return the badge for a state name such as ``running`` or ``bad``.

    ``word`` overrides the default word, which is the state name in sentence
    case. Unknown states are shown as failed, so they are never mistaken for
    healthy ones.
    """
    key = state.lower()
    kind = _STATE_KINDS.get(key, "failed")
    return StateBadge(kind=kind, word=word or key.replace("_", " ").capitalize())


def format_interval(milliseconds: int | float) -> str:
    """Format a polling interval: ``500 ms``, ``1 s``, ``1.5 s`` or ``2 min``."""
    if milliseconds < 1000:
        return f"{milliseconds:g} ms"
    seconds = milliseconds / 1000
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g} min"
    return f"{seconds:g} s"


def _build_environment() -> Environment:
    environment = Environment(
        loader=PackageLoader("plc_data_collector.panel", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["interval"] = format_interval
    environment.globals["state_badge"] = state_badge
    environment.globals["version"] = get_version()
    return environment


ENVIRONMENT = _build_environment()


def render(
    request: Request,
    template: str,
    context: Mapping[str, object] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a Panel template into an HTML response.

    Every template gets ``request``, ``nav`` (the current top navigation
    item, or None) and ``show_nav`` (False hides navigation and sign-out).
    """
    values: dict[str, object] = {"nav": None, "show_nav": True}
    values.update(context or {})
    values["request"] = request
    html = ENVIRONMENT.get_template(template).render(values)
    return HTMLResponse(html, status_code=status_code)
