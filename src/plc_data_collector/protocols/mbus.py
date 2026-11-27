"""Wired M-Bus communication driver (read-only, EN 13757-2/-3).

One ``MBusBus`` per Connection, kept for the life of the driver: reconnecting
closes and reopens it, so a Panel action that borrowed ``driver.bus`` keeps a
valid bus. A poll groups the Tags by meter and reads each meter once, in
address order, in one bus turn; every Tag then takes its record from that one
reading, so a Tag never gets another meter's or another record's value.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagRequest,
    TagResult,
    TagValue,
    TransientCommunicationError,
    ValueType,
)
from plc_data_collector.protocols.driver import DriverCapabilities
from plc_data_collector.protocols.endpoints import (
    is_serial_endpoint,
    parse_endpoint,
    parse_serial_endpoint,
)
from plc_data_collector.protocols.mbus_bus import (
    MBusBus,
    MBusTransport,
    MeterError,
    MeterReading,
    SerialTransport,
    TcpTransport,
)
from plc_data_collector.protocols.mbus_codec import (
    MBusRecord,
    MeterHeader,
    power_low,
    status_problems,
)
from plc_data_collector.protocols.mbus_tags import (
    MBUS_PROTOCOL,
    MBusConnectionOptions,
    MBusTagPlan,
    MeterAddress,
    convert,
    display_meter_address,
    manufacturer_code,
    medium_name,
    resolve_tag,
)

LOGGER = logging.getLogger(__name__)

# Caches live as long as the driver; a Tag edited many times must not grow them
# without bound, so they are dropped when they get this large.
_CACHE_LIMIT = 20_000

_ResolveKey = tuple[str, str, str]
_Failure = tuple[str, str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _meter_order(address: MeterAddress) -> tuple[bool, int, str]:
    """Primary addresses by number, then secondary addresses by display."""
    return (
        address.primary is None,
        address.primary or 0,
        display_meter_address(address),
    )


def _transport_factory(
    connection: ConnectionConfig, options: MBusConnectionOptions
) -> Callable[[], MBusTransport]:
    endpoint = connection.endpoint
    if is_serial_endpoint(endpoint):
        device = parse_serial_endpoint(endpoint)
        return lambda: SerialTransport(device, options.baud_rate)
    host, port = parse_endpoint(endpoint, protocol_label="M-Bus")
    timeout_s = connection.timeout_ms / 1000
    return lambda: TcpTransport(host, port, connect_timeout_s=timeout_s)


def _secondary_mismatch(address: MeterAddress, header: MeterHeader) -> str | None:
    """Why the reply to a secondary read is not from the requested meter."""
    wanted = address.secondary
    if wanted is None:
        return None
    digits_differ = len(header.id_digits) != len(wanted.id_digits) or any(
        want not in ("F", have)
        for want, have in zip(wanted.id_digits, header.id_digits, strict=False)
    )
    if digits_differ:
        return f"meter {header.id_digits} answered"
    if wanted.manufacturer not in (None, header.manufacturer_code):
        return (
            f"meter {header.id_digits} of manufacturer "
            f"{manufacturer_code(header.manufacturer_code)} answered"
        )
    if wanted.version not in (None, header.version):
        return f"meter {header.id_digits} version {header.version:02X} answered"
    if wanted.medium not in (None, header.medium):
        return (
            f"meter {header.id_digits} with medium {medium_name(header.medium)} "
            "answered"
        )
    return None


def _record_text(plan: MBusTagPlan) -> str:
    key = plan.key
    parts = [
        str(key.quantity),
        f"storage {key.storage}",
        f"tariff {key.tariff}",
        f"subunit {key.subunit}",
        str(key.function),
        *key.qualifiers,
    ]
    if plan.occurrence > 1:
        parts.append(f"occurrence {plan.occurrence}")
    return ", ".join(parts)


def _unit_text(unit: str) -> str:
    return unit or "no unit"


class MBusDriver:
    """M-Bus driver: one bus per connection, one turn per meter and poll."""

    def __init__(
        self,
        connection: ConnectionConfig,
        *,
        transport_factory: Callable[[], MBusTransport] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create an M-Bus driver for one configured connection."""
        self._connection = connection
        options = MBusConnectionOptions.from_options(connection.protocol_options)
        factory = transport_factory or _transport_factory(connection, options)
        # Transports do no I/O until opened; one is made to name the kind.
        self._description = factory().describe()
        self._bus = MBusBus(
            factory,
            timeout_s=connection.timeout_ms / 1000,
            max_telegrams=options.max_telegrams,
        )
        self._clock = clock
        # The transient error that closed the bus; repeated by read() until
        # connect() succeeds, so a retry reports the real cause.
        self._closed_by: TransientCommunicationError | None = None
        self._plans: dict[_ResolveKey, MBusTagPlan | ConfigurationError] = {}
        # Meters whose "power low" was logged, until a reading without it.
        self._power_low: set[MeterAddress] = set()

    @property
    def bus(self) -> MBusBus:
        """The Connection's bus; the same object for the life of the driver."""
        return self._bus

    @property
    def capabilities(self) -> DriverCapabilities:
        """Return M-Bus driver capabilities."""
        return DriverCapabilities(
            protocol=MBUS_PROTOCOL,
            supports_batch_read=True,
            supports_health_check=True,
        )

    async def connect(self) -> None:
        """Reopen the bus; raises ``mbus_connect_failed`` when it cannot."""
        await self.disconnect()
        await self._bus.open()

    async def disconnect(self) -> None:
        """Close the bus (after a turn in progress)."""
        self._closed_by = None
        await self._bus.close()

    async def health_check(self) -> bool:
        """Return whether the bus is open; silent meters do not make it unhealthy."""
        return self._bus.is_open

    def connection_details(self) -> str | None:
        """``"TCP gateway"`` or ``"Serial <baud> baud"`` while open (no endpoint)."""
        return self._description if self._bus.is_open else None

    async def read(self, tags: Sequence[TagRequest]) -> list[TagResult]:
        """Read every Tag; a meter that fails marks only its own Tags.

        Raises ``TransientCommunicationError`` when the transport fails,
        discarding partial results so the worker can reconnect and retry.
        """
        if not self._bus.is_open:
            if self._closed_by is not None:
                raise self._repeat(self._closed_by)
            raise TransientCommunicationError(
                "The M-Bus connection is not open.",
                code="mbus_not_connected",
                details={"connection_id": self._connection.id},
            )

        results: dict[str, TagResult] = {}
        meters: dict[MeterAddress, list[tuple[str, MBusTagPlan]]] = {}
        for tag in tags:
            plan = self._resolve(tag)
            if isinstance(plan, ConfigurationError):
                results[tag.tag_id] = self._failure(
                    tag.tag_id,
                    (plan.code or "mbus_invalid_options", plan.message),
                    self._now(),
                )
                continue
            meters.setdefault(plan.meter, []).append((tag.tag_id, plan))

        for meter in sorted(meters, key=_meter_order):
            members = meters[meter]
            try:
                async with self._bus.turn() as turn:
                    reading = await turn.read_meter(meter)
            except MeterError as error:
                received_at = self._now()
                failure = (
                    error.code or "mbus_no_reply",
                    f"Meter {display_meter_address(meter)}: {error.message}",
                )
                LOGGER.debug(
                    "mbus_meter_failed",
                    extra={
                        "connection_id": self._connection.id,
                        "meter": display_meter_address(meter),
                        "code": failure[0],
                    },
                )
                for tag_id, _ in members:
                    results[tag_id] = self._failure(tag_id, failure, received_at)
                continue
            except TransientCommunicationError as error:
                # The bus has closed its transport; the worker reconnects.
                self._closed_by = error
                await self._bus.close()
                raise
            results.update(self._results(meter, reading, members))
        return [results[tag.tag_id] for tag in tags]

    # -- planning ---------------------------------------------------------

    def _resolve(self, tag: TagRequest) -> MBusTagPlan | ConfigurationError:
        # Options hold lists (qualifiers), so the key is their canonical JSON.
        options = json.dumps(dict(tag.protocol_options), sort_keys=True, default=repr)
        key = (tag.tag_id, tag.address, options)
        cached = self._plans.get(key)
        if cached is not None:
            return cached
        try:
            plan: MBusTagPlan | ConfigurationError = resolve_tag(tag, self._connection)
        except ConfigurationError as error:
            plan = error
        if len(self._plans) >= _CACHE_LIMIT:
            self._plans.clear()
        self._plans[key] = plan
        return plan

    # -- results ----------------------------------------------------------

    def _results(
        self,
        meter: MeterAddress,
        reading: MeterReading,
        members: list[tuple[str, MBusTagPlan]],
    ) -> dict[str, TagResult]:
        received_at = self._now()
        header = reading.header
        display = display_meter_address(meter)
        mismatch = _secondary_mismatch(meter, header)
        if mismatch is None:
            self._note_power_low(meter, header)
        problems = status_problems(header.status)
        results: dict[str, TagResult] = {}
        for tag_id, plan in members:
            outcome: TagValue | _Failure
            if mismatch is not None:
                outcome = ("mbus_wrong_meter", f"Meter {display}: {mismatch}.")
            elif plan.meter_id is not None and plan.meter_id != header.id_digits:
                outcome = (
                    "mbus_wrong_meter",
                    f"Meter {display} answered with ID {header.id_digits}, not "
                    f"the configured meter ID {plan.meter_id}.",
                )
            else:
                outcome = self._value(display, plan, reading.records)
            if isinstance(outcome, tuple):
                results[tag_id] = self._failure(tag_id, outcome, received_at)
            elif problems:
                results[tag_id] = TagResult.uncertain(
                    tag_id=tag_id,
                    value=outcome,
                    error_code="mbus_meter_status",
                    error_message=(f"Meter {display} reports: {', '.join(problems)}."),
                    source_timestamp=received_at,
                    received_at=received_at,
                )
            else:
                results[tag_id] = TagResult.success(
                    tag_id=tag_id,
                    value=outcome,
                    source_timestamp=received_at,
                    received_at=received_at,
                )
        return results

    def _value(
        self, display: str, plan: MBusTagPlan, records: Sequence[MBusRecord]
    ) -> TagValue | _Failure:
        matches = [record for record in records if record.key == plan.key]
        if len(matches) < plan.occurrence:
            return (
                "mbus_record_not_found",
                f"Meter {display} sent no record {_record_text(plan)}.",
            )
        record = matches[plan.occurrence - 1]
        if record.value_error is not None:
            return (
                record.value_error,
                f"Meter {display}, record {_record_text(plan)}: "
                f"{record.unsupported_reason}.",
            )
        if (
            record.exact_value is None
            or record.unit is None
            or record.family is None
            or record.unsupported_reason is not None
        ):
            return (
                "mbus_record_unsupported",
                f"Meter {display}, record {_record_text(plan)}: "
                f"{record.unsupported_reason or 'no value'}.",
            )
        if record.family.name != plan.family.name:
            return (
                "mbus_unit_mismatch",
                f"Meter {display} sends {_record_text(plan)} in "
                f"{_unit_text(record.unit)}, which does not convert to the "
                f"Tag's unit {_unit_text(plan.unit)}.",
            )
        value = convert(
            Decimal(record.exact_value), record.unit, plan.unit, plan.family
        )
        if plan.value_type is ValueType.INTEGER:
            if value != value.to_integral_value():
                return (
                    "mbus_record_unsupported",
                    f"Meter {display}, record {_record_text(plan)}: {value} "
                    f"{plan.unit} is not a whole number.",
                )
            return TagValue.integer(int(value))
        number = float(value)
        if not math.isfinite(number):
            return (
                "mbus_value_not_finite",
                f"Meter {display}, record {_record_text(plan)}: the value is out "
                "of range.",
            )
        return TagValue.numeric(number)

    def _note_power_low(self, meter: MeterAddress, header: MeterHeader) -> None:
        """Warn once per meter while its status says "power low"."""
        if not power_low(header.status):
            self._power_low.discard(meter)
            return
        if meter in self._power_low:
            return
        self._power_low.add(meter)
        LOGGER.warning(
            "mbus_meter_power_low",
            extra={
                "connection_id": self._connection.id,
                "meter": display_meter_address(meter),
                "meter_id": header.id_digits,
            },
        )

    # -- helpers ----------------------------------------------------------

    def _failure(
        self, tag_id: str, failure: _Failure, received_at: datetime
    ) -> TagResult:
        code, message = failure
        return TagResult.failure(
            tag_id=tag_id,
            error_code=code,
            error_message=message,
            received_at=received_at,
        )

    def _repeat(
        self, error: TransientCommunicationError
    ) -> TransientCommunicationError:
        return TransientCommunicationError(
            error.message, code=error.code, details=dict(error.details)
        )

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def create_mbus_driver(connection: ConnectionConfig) -> MBusDriver:
    """Create an M-Bus driver for registry registration."""
    return MBusDriver(connection)
