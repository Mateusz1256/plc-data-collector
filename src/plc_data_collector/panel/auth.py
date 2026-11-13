"""Admin sign-in for the Panel: session check, password check and throttling."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import Request

if TYPE_CHECKING:
    from plc_data_collector.api.app import WebOptions

SESSION_KEY = "admin"
LOGIN_PATH = "/panel/login"
DEFAULT_NEXT = "/panel/live"
FAILURE_DELAY_S = 1.0

# Each failed sign-in waits this long before answering. A module-level seam,
# so tests can replace it instead of sleeping.
failure_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class LoginRequired(Exception):
    """Raised by ``require_admin`` when the request has no admin session."""

    def __init__(self, next_path: str) -> None:
        super().__init__("Sign-in required.")
        self.next_path = next_path

    @property
    def login_url(self) -> str:
        """Return the sign-in URL that comes back to the requested page."""
        return f"{LOGIN_PATH}?next={quote(self.next_path, safe='/')}"


class LoginThrottle:
    """Refuse sign-ins from a client after too many recent failures.

    Failures are kept per client IP for ``window_s`` seconds; with
    ``max_failures`` of them in the window, ``blocked()`` is true. Used from
    the event loop only, so it needs no locking.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window_s: float = 300,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a throttle; ``clock`` returns monotonic seconds."""
        if max_failures < 1 or window_s <= 0:
            raise ValueError("max_failures and window_s must be positive.")
        self._max_failures = max_failures
        self._window_s = window_s
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def blocked(self, client: str) -> bool:
        """Return whether ``client`` has used up its failed sign-ins."""
        failures = self._recent(client)
        return failures is not None and len(failures) >= self._max_failures

    def record_failure(self, client: str) -> None:
        """Count one failed sign-in for ``client``."""
        self._prune()
        self._failures.setdefault(client, deque()).append(self._clock())

    def reset(self, client: str) -> None:
        """Forget the failures of ``client`` after a successful sign-in."""
        self._failures.pop(client, None)

    def _recent(self, client: str) -> deque[float] | None:
        failures = self._failures.get(client)
        if failures is None:
            return None
        cutoff = self._clock() - self._window_s
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[client]
            return None
        return failures

    def _prune(self) -> None:
        # Keeps the map from growing with clients that stopped trying.
        for client in list(self._failures):
            self._recent(client)


def web_options(request: Request) -> WebOptions:
    """Return the Panel options of the app serving ``request``."""
    options: WebOptions = request.app.state.panel_web
    return options


def password_matches(expected: str | None, submitted: str) -> bool:
    """Compare passwords in constant time; no password configured never matches."""
    if expected is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), submitted.encode("utf-8"))


def session_token(options: WebOptions) -> str:
    """Return the value stored in the session for a signed-in admin.

    It is derived from the admin password, so changing the password signs
    every existing session out. The password itself never enters the cookie.
    """
    password = options.admin_password or ""
    return hmac.new(
        options.session_secret,
        b"admin:" + password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def is_signed_in(request: Request) -> bool:
    """Return whether the request carries a valid admin session."""
    options = web_options(request)
    if options.admin_password is None:
        return False
    stored = request.session.get(SESSION_KEY)
    if not isinstance(stored, str):
        return False
    return hmac.compare_digest(stored, session_token(options))


def sign_in(request: Request) -> None:
    """Start an admin session for ``request``."""
    request.session.clear()
    request.session[SESSION_KEY] = session_token(web_options(request))


def sign_out(request: Request) -> None:
    """End the admin session for ``request``."""
    request.session.clear()


def require_admin(request: Request) -> None:
    """FastAPI dependency: let signed-in admins through, send others to sign in."""
    if not is_signed_in(request):
        raise LoginRequired(request.url.path)


def safe_next(value: str | None) -> str:
    """Return ``value`` if it is a Panel path, otherwise the Live page.

    Only same-site Panel paths are allowed, so the sign-in form cannot be used
    to redirect to another site.
    """
    if (
        value
        and value.startswith("/panel/")
        and not value.startswith("//")
        and "\\" not in value
        and not value.startswith(LOGIN_PATH)
    ):
        return value
    return DEFAULT_NEXT


def client_address(request: Request) -> str:
    """Return the address used to throttle sign-ins from one client."""
    return request.client.host if request.client is not None else "unknown"
