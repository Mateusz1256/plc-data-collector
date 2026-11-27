"""Read meter and Scan bus for the Panel: record rows, names and the save check.

Read meter reads one meter once and keeps the reading in memory
(``ReadingStore``, lost on restart), so choosing records, renaming them by
their prefix and saving never touch the bus again. Every record becomes a
row: supported records can be ticked; unsupported ones are shown greyed with
their reason. A Tag made from a row stores the meter address as it was read
(primary or secondary) and always the meter's ID as ``meter_id`` (R7).

Names follow ``<prefix>_<quantity>[_s<storage>][_t<tariff>][_u<subunit>]
[_<function>][_<qualifiers>]``; a name is made unique against the page's
other names and every Tag ID in use or archived by ``_2``, ``_3`` ...; the
Tag ID comes from the name as in the other Panel imports
(``generate_tag_id``). A save refuses rather than renames: when an ID shown
on the page has been taken meanwhile, the page is shown again with the new
names.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ValidationError

from plc_data_collector.app.mbus_scan import FoundMeter
from plc_data_collector.domain import ConfigurationError, TagConfig
from plc_data_collector.panel.tag_forms import generate_tag_id
from plc_data_collector.panel.tag_paste import MAX_PASTE_ROWS
from plc_data_collector.protocols.mbus_bus import MeterReading
from plc_data_collector.protocols.mbus_codec import (
    MBusRecord,
    MeterHeader,
    power_low,
    status_problems,
)
from plc_data_collector.protocols.mbus_tags import (
    Function,
    MBusTagOptions,
    Quantity,
    RecordKey,
    parse_meter_address,
    register_key,
    units_for,
    value_type_of,
)

MAX_SAVE_ROWS = MAX_PASTE_ROWS
READING_MAX_AGE_S = 30 * 60
MAX_READINGS = 32
_NAME_ATTEMPTS = 1000
_METER_ID_DIGITS = 8

EXPIRED_MESSAGE = "This reading expired. Read the meter again."
GROUP_MESSAGE = "Choose a tag group."
NOTHING_SELECTED_MESSAGE = "Select at least one record."
ROW_LIMIT_MESSAGE = f"Save at most {MAX_SAVE_ROWS} tags at a time."
UNKNOWN_SELECTION_MESSAGE = (
    "Some selected rows are not in this reading. Read the meter again."
)
ROW_PROBLEMS_MESSAGE = "Some selected records have a problem; see the Problem column."
NAMES_CHANGED_MESSAGE = (
    "The names changed since they were shown (the prefix changed, or another "
    "tag took one of the IDs). Check the names below and save again."
)
ALREADY_USED = "Already read by tag {tag_id}."
UNIT_CHOICE = "Choose a unit of {quantity}: {units}."


# Readings kept between the read and the save


@dataclass(frozen=True, slots=True)
class StoredReading:
    """One meter read from the Read meter page."""

    connection_id: str
    meter: str  # canonical, as typed or scanned
    reading: MeterReading
    created: float


class ReadingStore:
    """The latest meter readings, in memory, for ``READING_MAX_AGE_S``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Start empty."""
        self._clock = clock
        self._readings: dict[str, StoredReading] = {}

    def save(
        self, connection_id: str, meter: str, reading: MeterReading
    ) -> tuple[str, StoredReading]:
        """Keep ``reading``; return its token. The oldest go beyond the limit."""
        self._expire()
        while len(self._readings) >= MAX_READINGS:
            oldest = min(self._readings, key=lambda t: self._readings[t].created)
            del self._readings[oldest]
        token = secrets.token_urlsafe(16)
        stored = StoredReading(connection_id, meter, reading, self._clock())
        self._readings[token] = stored
        return token, stored

    def load(self, token: str, connection_id: str) -> StoredReading | None:
        """The reading of ``token`` if it is the Connection's and not expired."""
        self._expire()
        stored = self._readings.get(token)
        if stored is None or stored.connection_id != connection_id:
            return None
        return stored

    def delete(self, token: str) -> None:
        """Forget a reading (after its Tags are saved)."""
        self._readings.pop(token, None)

    def _expire(self) -> None:
        limit = self._clock() - READING_MAX_AGE_S
        for token in [t for t, r in self._readings.items() if r.created < limit]:
            del self._readings[token]


# Rows


@dataclass(frozen=True, slots=True)
class RecordRow:
    """One record of a reading as the Read meter table shows it.

    Unsupported records have ``supported`` False, ``reason`` set and no name.
    A record whose value cannot be decoded now is supported (a Tag can read
    it) but unticked, with the reason. ``existing_tag`` is the ID of a Tag
    that already reads the record.
    """

    index: int
    supported: bool
    description: str
    quantity: str = ""
    value: str = ""
    natural_unit: str = ""
    units: tuple[str, ...] = ()
    unit: str = ""
    storage: int = 0
    tariff: int = 0
    subunit: int = 0
    function: str = ""
    qualifiers: tuple[str, ...] = ()
    occurrence: int = 1
    name: str = ""
    tag_id: str = ""
    reason: str | None = None
    existing_tag: str | None = None
    selected: bool = False


def default_prefix(meter: str, header: MeterHeader) -> str:
    """``m<primary>`` for a meter read by primary address, else ``m<ID>``."""
    address = parse_meter_address(meter)
    if address.primary is not None:
        return f"m{address.primary}"
    return f"m{header.id_digits}"


def suggested_name(prefix: str, key: RecordKey) -> str:
    """The spec's default name of a record: only non-default parts appear."""
    parts = [prefix, str(key.quantity)]
    if key.storage:
        parts.append(f"s{key.storage}")
    if key.tariff:
        parts.append(f"t{key.tariff}")
    if key.subunit:
        parts.append(f"u{key.subunit}")
    if key.function is not Function.INSTANTANEOUS:
        parts.append(str(key.function))
    parts.extend(key.qualifiers)
    return "_".join(parts)


def meter_id_of(header: MeterHeader) -> str | None:
    """The header ID when it is an 8-digit identification number."""
    digits = header.id_digits
    if len(digits) == _METER_ID_DIGITS and digits.isdigit():
        return digits
    return None


def record_rows(
    reading: MeterReading,
    existing: Iterable[TagConfig],
    prefix: str,
    *,
    meter: str,
    connection_id: str,
    taken_ids: Collection[str] = (),
) -> list[RecordRow]:
    """The table rows of ``reading``, read at ``meter`` (canonical address).

    ``existing`` are the Connection's Tags: a record one of them reads is
    marked and unticked, and their names and IDs are not suggested again.
    ``taken_ids`` are every Tag ID in use or archived. A record whose key
    the meter repeats gets the next ``occurrence``.
    """
    existing = list(existing)
    holders = _holders(existing)
    taken = set(taken_ids) | {tag.id for tag in existing}
    used_names = {tag.name for tag in existing}
    meter_id = meter_id_of(reading.header)
    seen: dict[RecordKey, int] = {}
    rows: list[RecordRow] = []
    for index, record in enumerate(reading.records):
        key = record.key
        if key is None:
            rows.append(
                RecordRow(
                    index=index,
                    supported=False,
                    description=record.description,
                    reason=record.unsupported_reason,
                )
            )
            continue
        occurrence = seen.get(key, 0) + 1
        seen[key] = occurrence
        units = _unit_choices(record, key)
        unit = record.unit if record.unit in units else (units[0] if units else "")
        name, tag_id = _unique_name(
            suggested_name(prefix, key), connection_id, taken, used_names
        )
        taken.add(tag_id)
        used_names.add(name)
        holder = holders.get(
            _key_of(meter, key, unit, occurrence, meter_id, connection_id)
        )
        rows.append(
            RecordRow(
                index=index,
                supported=True,
                description=record.description,
                quantity=str(key.quantity),
                value=_value_text(record),
                natural_unit=record.unit or "",
                units=units,
                unit=unit,
                storage=key.storage,
                tariff=key.tariff,
                subunit=key.subunit,
                function=str(key.function),
                qualifiers=key.qualifiers,
                occurrence=occurrence,
                name=name,
                tag_id=tag_id,
                reason=record.unsupported_reason,
                existing_tag=holder,
                selected=holder is None and record.value_error is None,
            )
        )
    return rows


def _unit_choices(record: MBusRecord, key: RecordKey) -> tuple[str, ...]:
    """The Tag units of the record's own family (durations: seconds only)."""
    family = record.family
    if family is None:
        return ()
    return tuple(u for u in units_for(key.quantity) if u in family.factors)


def _value_text(record: MBusRecord) -> str:
    value = record.exact_value
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:f}"
    return str(value)


def _unique_name(
    base: str, connection_id: str, taken: Collection[str], used: Collection[str]
) -> tuple[str, str]:
    """``base``, else ``base_2``, ``base_3`` ...: the first name free as a
    name on the page, as a Tag ID and as the source of a free Tag ID."""
    name = base
    for number in range(1, _NAME_ATTEMPTS + 1):
        name = base if number == 1 else f"{base}_{number}"
        tag_id = generate_tag_id(connection_id, name, ())
        if name not in used and name not in taken and tag_id not in taken:
            return name, tag_id
    # Only for absurdly long names whose IDs are cut short: the ID gets the suffix.
    return name, generate_tag_id(connection_id, name, taken)


def _holders(existing: Iterable[TagConfig]) -> dict[tuple[object, ...], str]:
    found: dict[tuple[object, ...], str] = {}
    for tag in existing:
        try:
            found.setdefault(register_key(tag), tag.id)
        except ConfigurationError:
            continue
    return found


def _key_of(
    meter: str,
    key: RecordKey,
    unit: str,
    occurrence: int,
    meter_id: str | None,
    connection_id: str,
) -> tuple[object, ...]:
    """The register key of the Tag a row would create."""
    probe = _tag(
        tag_id=f"{connection_id}_probe",
        name="probe",
        group_id="probe",
        meter=meter,
        key=key,
        unit=unit,
        occurrence=occurrence,
        meter_id=meter_id,
    )
    return register_key(probe)


def _tag(
    *,
    tag_id: str,
    name: str,
    group_id: str,
    meter: str,
    key: RecordKey,
    unit: str,
    occurrence: int,
    meter_id: str | None,
) -> TagConfig:
    """The Tag of a record; raises ``ValidationError`` for invalid options."""
    options = MBusTagOptions(
        quantity=key.quantity,
        unit=unit or None,
        storage=key.storage,
        tariff=key.tariff,
        subunit=key.subunit,
        function=key.function,
        qualifiers=key.qualifiers,
        occurrence=occurrence,
        meter_id=meter_id,
    )
    return TagConfig(
        id=tag_id,
        tag_group_id=group_id,
        name=name,
        address=meter,
        value_type=value_type_of(key.quantity),
        metadata={"unit": unit} if unit else {},
        protocol_options=options.to_options(),
    )


# The save


@dataclass
class SaveCheck:
    """What a save may create: ``tags``, or ``error`` and the row ``problems``.

    ``conflict`` is set when names shown on the page differ from the names a
    save would use now.
    """

    tags: list[TagConfig] | None
    error: str | None = None
    problems: dict[int, str] = field(default_factory=dict)
    conflict: bool = False


def check_save(
    rows: Sequence[RecordRow],
    *,
    selected: Collection[str],
    units: Mapping[str, str],
    shown_ids: Mapping[int, str],
    group_id: str,
    group_ids: Collection[str],
    meter: str,
    meter_id: str | None,
) -> SaveCheck:
    """Check the ticked rows and build their Tags, all or nothing.

    ``selected`` are the posted row indexes, ``units`` the posted unit per
    row index and ``shown_ids`` the Tag IDs the page showed; a ticked row
    whose ID differs now refuses the save (nothing is renamed silently).
    """
    by_index = {str(row.index): row for row in rows if row.supported}
    chosen = [by_index[i] for i in sorted(set(selected) & set(by_index), key=int)]
    if group_id not in group_ids:
        return SaveCheck(None, GROUP_MESSAGE)
    if len(chosen) < len(set(selected)):
        return SaveCheck(None, UNKNOWN_SELECTION_MESSAGE)
    if not chosen:
        return SaveCheck(None, NOTHING_SELECTED_MESSAGE)
    if len(chosen) > MAX_SAVE_ROWS:
        return SaveCheck(None, ROW_LIMIT_MESSAGE)
    problems: dict[int, str] = {}
    tags: list[TagConfig] = []
    for row in chosen:
        unit = units.get(str(row.index), row.unit)
        if row.existing_tag is not None:
            problems[row.index] = ALREADY_USED.format(tag_id=row.existing_tag)
            continue
        if unit not in row.units and (row.units or unit):
            problems[row.index] = UNIT_CHOICE.format(
                quantity=row.quantity, units=", ".join(row.units) or "(none)"
            )
            continue
        try:
            tags.append(
                _tag(
                    tag_id=row.tag_id,
                    name=row.name,
                    group_id=group_id,
                    meter=meter,
                    key=_row_key(row),
                    unit=unit,
                    occurrence=row.occurrence,
                    meter_id=meter_id,
                )
            )
        except (ValidationError, ConfigurationError) as error:
            problems[row.index] = str(error)
    if problems:
        return SaveCheck(None, ROW_PROBLEMS_MESSAGE, problems)
    if any(shown_ids.get(row.index) != row.tag_id for row in chosen):
        return SaveCheck(None, NAMES_CHANGED_MESSAGE, conflict=True)
    return SaveCheck(tags)


def _row_key(row: RecordRow) -> RecordKey:
    return RecordKey(
        quantity=Quantity(row.quantity),
        storage=row.storage,
        tariff=row.tariff,
        subunit=row.subunit,
        function=Function(row.function),
        qualifiers=row.qualifiers,
    )


def parse_shown_ids(raw: str) -> dict[int, str]:
    """``"0=heat_a 2=heat_b"`` (the page's hidden field) as a mapping."""
    shown: dict[int, str] = {}
    for part in raw.split():
        index, _, tag_id = part.partition("=")
        if index.isdigit():
            shown[int(index)] = tag_id
    return shown


def shown_ids_text(rows: Iterable[RecordRow]) -> str:
    """The hidden field that carries the Tag IDs the page shows."""
    return " ".join(f"{row.index}={row.tag_id}" for row in rows if row.supported)


# The meter header


def status_text(header: MeterHeader) -> str:
    """The status byte decoded: ``OK``, or the problems and power low."""
    parts = list(status_problems(header.status))
    if power_low(header.status):
        parts.append("power low")
    return ", ".join(parts) if parts else "OK"


# Scan results


@dataclass(frozen=True, slots=True)
class ScanRow:
    """One meter a scan found, as the results table shows it."""

    primary: str
    id_digits: str
    manufacturer: str
    version: str
    medium: str
    has_tags: bool


def scan_rows(
    meters: Iterable[FoundMeter], existing: Iterable[TagConfig]
) -> list[ScanRow]:
    """The results table; a meter is marked when a Tag of the Connection
    reads it (by its primary address, its ID or its ``meter_id``)."""
    primaries: set[int] = set()
    ids: set[str] = set()
    for tag in existing:
        try:
            address = parse_meter_address(tag.address)
        except ConfigurationError:
            continue
        if address.primary is not None:
            primaries.add(address.primary)
        elif address.secondary is not None:
            ids.add(address.secondary.id_digits)
        meter_id = tag.protocol_options.get("meter_id")
        if isinstance(meter_id, str):
            ids.add(meter_id)
    return [
        ScanRow(
            primary="—" if meter.primary is None else str(meter.primary),
            id_digits=meter.header.id_digits,
            manufacturer=meter.header.manufacturer,
            version=f"{meter.header.version:02X}",
            medium=meter.header.medium_name,
            has_tags=meter.header.id_digits in ids
            or (meter.primary is not None and meter.primary in primaries),
        )
        for meter in meters
    ]
