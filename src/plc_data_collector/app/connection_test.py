"""Test connection: connect once with a new driver and read one probe Tag.

M-Bus is tested on the Connection's one bus instead (see ``mbus_access``):
the running worker's, or a temporary one, never a second link.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from plc_data_collector.app.mbus_access import BusAccessRefused, borrow_bus
from plc_data_collector.domain import (
    CollectorError,
    ConfigurationError,
    ConnectionConfig,
    Quality,
    TagConfig,
    TagRequest,
    TagResult,
)
from plc_data_collector.protocols import (
    MBUS_PROTOCOL,
    CommunicationDriver,
    DescribesConnection,
    DriverRegistry,
)
from plc_data_collector.protocols.mbus import read_tag
from plc_data_collector.protocols.mbus_bus import BusTransportError, MBusBus

if TYPE_CHECKING:
    from plc_data_collector.app.mbus_access import BusHost

LOGGER = logging.getLogger(__name__)

TIMEOUT_CODE = "connection_test_timeout"
# Extra time on top of the Connection timeout before the whole test gives up.
DEADLINE_MARGIN_S = 1.0
# The longest an M-Bus Test connection may take, whatever its settings.
MBUS_TEST_LIMIT_S = 60.0


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
    host: BusHost | None = None,
) -> ConnectionTestResult:
    """Connect with a new driver, read ``probe_tag`` if given, then disconnect.

    The runtime's worker for this Connection is not touched. The whole test
    ends after ``connection.timeout_ms / 1000 + 1`` seconds, and the driver
    is always disconnected, also after a failure or timeout.

    With ``host``, an M-Bus Connection is tested on its bus instead
    (``borrow_bus``): success when the bus is open, and the probe Tag's meter
    is read in one turn, the way a poll reads it.
    """
    if host is not None and connection.protocol == MBUS_PROTOCOL:
        return await _probe_mbus(connection, host, probe_tag)
    deadline_s = connection.timeout_ms / 1000 + DEADLINE_MARGIN_S
    started = time.monotonic()

    def elapsed_ms() -> int:
        return round((time.monotonic() - started) * 1000)

    try:
        driver = registry.create_driver(connection)
    except CollectorError as error:
        return _finish(connection, elapsed_ms(), _failure_message(error))
    except Exception as error:
        return _finish(connection, elapsed_ms(), _unexpected(error))

    try:
        try:
            async with asyncio.timeout(deadline_s):
                ok, suffix, code = await _connect_and_probe(
                    driver, connection, probe_tag
                )
        finally:
            await _disconnect(driver, connection)
    except TimeoutError:
        return _finish(connection, elapsed_ms(), _timed_out(deadline_s))
    except CollectorError as error:
        return _finish(connection, elapsed_ms(), _failure_message(error))
    except Exception as error:
        return _finish(connection, elapsed_ms(), _unexpected(error))

    total_ms = elapsed_ms()
    return _finish(
        connection, total_ms, (ok, f"Connected in {total_ms} ms{suffix}", code)
    )


_Outcome = tuple[bool, str, str | None]


async def _probe_mbus(
    connection: ConnectionConfig, host: BusHost, probe_tag: TagConfig | None
) -> ConnectionTestResult:
    started = time.monotonic()

    def elapsed_ms() -> int:
        return round((time.monotonic() - started) * 1000)

    loop = asyncio.get_running_loop()
    opened_at = loop.time()
    deadline_s = MBUS_TEST_LIMIT_S
    try:
        async with (
            asyncio.timeout(MBUS_TEST_LIMIT_S) as limit,
            borrow_bus(host, connection) as bus,
        ):
            # The bus actually used sets the deadline: a borrowed running bus
            # keeps its own timeout and telegram limit.
            deadline_s = mbus_deadline_s(bus)
            limit.reschedule(opened_at + deadline_s)
            ok, suffix, code = await _mbus_probe_on(bus, connection, probe_tag)
    except BusAccessRefused as error:
        return _finish(connection, elapsed_ms(), (False, error.message, error.code))
    except TimeoutError:
        return _finish(connection, elapsed_ms(), _timed_out(deadline_s))
    except CollectorError as error:
        return _finish(connection, elapsed_ms(), _failure_message(error))
    except Exception as error:
        return _finish(connection, elapsed_ms(), _unexpected(error))
    total_ms = elapsed_ms()
    return _finish(
        connection, total_ms, (ok, f"Connected in {total_ms} ms{suffix}", code)
    )


def mbus_deadline_s(bus: MBusBus) -> float:
    """Waiting for one meter read on ``bus``, then the probe Tag's read.

    A meter read is at most a SND_NKE and ``max_telegrams`` telegrams, each
    tried twice, at the bus's timeout; never more than ``MBUS_TEST_LIMIT_S``.
    """
    meter_read_s = (1 + bus.max_telegrams) * 2 * bus.timeout_s
    deadline_s = bus.timeout_s + 2 * meter_read_s + DEADLINE_MARGIN_S
    return min(deadline_s, MBUS_TEST_LIMIT_S)


async def _mbus_probe_on(
    bus: MBusBus, connection: ConnectionConfig, probe_tag: TagConfig | None
) -> _Outcome:
    description = bus.describe()
    if description is None:  # a running worker whose bus is closed
        raise BusTransportError("mbus_not_connected")
    if probe_tag is None:
        return True, f" ({description}).", None
    result = await read_tag(bus, connection, _probe_request(probe_tag, connection))
    ok, suffix, code = _probe_outcome(probe_tag.address, result)
    return ok, f" ({description}){suffix}", code


async def _connect_and_probe(
    driver: CommunicationDriver,
    connection: ConnectionConfig,
    probe_tag: TagConfig | None,
) -> _Outcome:
    """Return ``(ok, text after "Connected in N ms", error code)``."""
    await driver.connect()
    ok, suffix, code = await _probe(driver, connection, probe_tag)
    details = (
        driver.connection_details() if isinstance(driver, DescribesConnection) else None
    )
    if details:
        suffix = f" ({details}){suffix}"
    return ok, suffix, code


async def _probe(
    driver: CommunicationDriver,
    connection: ConnectionConfig,
    probe_tag: TagConfig | None,
) -> _Outcome:
    if probe_tag is None:
        return True, ".", None
    (result,) = await driver.read([_probe_request(probe_tag, connection)])
    return _probe_outcome(probe_tag.address, result)


def _probe_request(probe_tag: TagConfig, connection: ConnectionConfig) -> TagRequest:
    return TagRequest(
        tag_id=probe_tag.id,
        address=probe_tag.address,
        value_type=probe_tag.value_type,
        timeout_ms=connection.timeout_ms,
        protocol_options=probe_tag.protocol_options,
    )


def _probe_outcome(address: str, result: TagResult) -> _Outcome:
    if result.value is None:
        code = result.error_code or "read_failed"
        # The driver's message explains the failure (for S7 for example the
        # PUT/GET setting), so it is shown before the generic hint.
        explanation = f"{result.error_message} " if result.error_message else ""
        return (
            False,
            f", but reading {address} failed ({code}). {explanation}"
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


def _timed_out(deadline_s: float) -> _Outcome:
    return (
        False,
        f"No answer within {deadline_s:g} s ({TIMEOUT_CODE}). Check that the "
        "endpoint is reachable from the collector and that the timeout is long "
        "enough.",
        TIMEOUT_CODE,
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


def _unexpected(error: Exception) -> _Outcome:
    # The exception text may quote the endpoint with its credentials, so only
    # the type is shown.
    code = type(error).__name__
    return (
        False,
        f"The test failed with an unexpected error ({code}). Check the "
        "connection settings and the collector log.",
        code,
    )


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
