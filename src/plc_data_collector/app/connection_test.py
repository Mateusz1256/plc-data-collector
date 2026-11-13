"""Test connection: connect once with a new driver and read one probe Tag."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from plc_data_collector.domain import (
    CollectorError,
    ConfigurationError,
    ConnectionConfig,
    Quality,
    TagConfig,
    TagRequest,
)
from plc_data_collector.protocols import CommunicationDriver, DriverRegistry

LOGGER = logging.getLogger(__name__)

TIMEOUT_CODE = "connection_test_timeout"
# Extra time on top of the Connection timeout before the whole test gives up.
DEADLINE_MARGIN_S = 1.0


@dataclass(frozen=True)
class ConnectionTestResult:
    """Outcome of Test connection, worded for the Panel.

    ``message`` names error codes and the probe Tag's address, never the
    endpoint, credentials or a driver's exception text.
    """

    ok: bool
    elapsed_ms: int
    message: str


async def probe_connection(
    connection: ConnectionConfig,
    registry: DriverRegistry,
    *,
    probe_tag: TagConfig | None,
) -> ConnectionTestResult:
    """Connect with a new driver, read ``probe_tag`` if given, then disconnect.

    The runtime's worker for this Connection is not touched. The whole test
    ends after ``connection.timeout_ms / 1000 + 1`` seconds, and the driver
    is always disconnected, also after a failure or timeout.
    """
    deadline_s = connection.timeout_ms / 1000 + DEADLINE_MARGIN_S
    started = time.monotonic()

    def elapsed_ms() -> int:
        return round((time.monotonic() - started) * 1000)

    try:
        driver = registry.create_driver(connection)
    except CollectorError as error:
        return _finish(connection, elapsed_ms(), _failure_message(error))

    try:
        try:
            async with asyncio.timeout(deadline_s):
                ok, suffix, code = await _connect_and_probe(
                    driver, connection, probe_tag
                )
        finally:
            await _disconnect(driver, connection)
    except TimeoutError:
        return _finish(
            connection,
            elapsed_ms(),
            (
                False,
                f"No answer within {deadline_s:g} s ({TIMEOUT_CODE}). Check that "
                "the endpoint is reachable from the collector and that the "
                "timeout is long enough.",
                TIMEOUT_CODE,
            ),
        )
    except CollectorError as error:
        return _finish(connection, elapsed_ms(), _failure_message(error))
    except Exception as error:
        # The exception text may quote the endpoint with its credentials, so
        # only the type is shown.
        code = type(error).__name__
        return _finish(
            connection,
            elapsed_ms(),
            (
                False,
                f"The test failed with an unexpected error ({code}). Check the "
                "connection settings and the collector log.",
                code,
            ),
        )

    total_ms = elapsed_ms()
    return _finish(
        connection, total_ms, (ok, f"Connected in {total_ms} ms{suffix}", code)
    )


_Outcome = tuple[bool, str, str | None]


async def _connect_and_probe(
    driver: CommunicationDriver,
    connection: ConnectionConfig,
    probe_tag: TagConfig | None,
) -> _Outcome:
    """Return ``(ok, text after "Connected in N ms", error code)``."""
    await driver.connect()
    if probe_tag is None:
        return True, ".", None
    (result,) = await driver.read(
        [
            TagRequest(
                tag_id=probe_tag.id,
                address=probe_tag.address,
                value_type=probe_tag.value_type,
                timeout_ms=connection.timeout_ms,
            )
        ]
    )
    address = probe_tag.address
    if result.value is None:
        code = result.error_code or "read_failed"
        return (
            False,
            f", but reading {address} failed ({code}). "
            "Check the Tag address and that the Tag exists on the server.",
            code,
        )
    if result.quality is not Quality.GOOD:
        return (
            True,
            f". Read {address}, but the server reports "
            f"{result.quality.value} quality. Check the Tag on the server.",
            None,
        )
    return True, f". Read {address} without errors.", None


async def _disconnect(
    driver: CommunicationDriver, connection: ConnectionConfig
) -> None:
    try:
        await asyncio.wait_for(
            driver.disconnect(),
            timeout=connection.timeout_ms / 1000 + DEADLINE_MARGIN_S,
        )
    except Exception as error:
        LOGGER.warning(
            "connection_test_disconnect_failed",
            extra={
                "connection_id": connection.id,
                "error_type": type(error).__name__,
                "error_code": getattr(error, "code", None),
            },
        )


def _failure_message(error: CollectorError) -> _Outcome:
    code = error.code or type(error).__name__
    if isinstance(error, ConfigurationError):
        hint = "Check the protocol and its settings."
        text = f"The connection settings can't be used ({code}). {hint}"
    elif code.endswith("timeout"):
        text = (
            f"The connection timed out ({code}). Check that the endpoint is "
            "reachable from the collector and that the timeout is long enough."
        )
    else:
        text = (
            f"Could not connect ({code}). Check the endpoint address and port, "
            "the credentials, and that the server is running."
        )
    return False, text, code


def _finish(
    connection: ConnectionConfig, elapsed_ms: int, outcome: _Outcome
) -> ConnectionTestResult:
    ok, message, code = outcome
    LOGGER.info(
        "connection_test_finished",
        extra={
            "connection_id": connection.id,
            "ok": ok,
            "error_code": code,
            "elapsed_ms": elapsed_ms,
        },
    )
    return ConnectionTestResult(ok=ok, elapsed_ms=elapsed_ms, message=message)
