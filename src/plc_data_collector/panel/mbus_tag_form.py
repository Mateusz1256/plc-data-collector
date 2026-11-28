"""The M-Bus Tag form: meter, quantity, unit and the rest of the record key.

The form and a pasted CSV row use the same field names; a pasted row gives
its qualifiers as one ``qualifiers`` cell joined with ``+``
(``forward+per_hour``), the form as one checkbox per qualifier
(``qualifier_forward``). The meter address is stored in canonical form and
the unit is mirrored into ``metadata["unit"]`` so the Panel's unit displays
work unchanged.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from pydantic import ValidationError

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
)
from plc_data_collector.panel.protocol_forms import FormErrors, _parse_digits
from plc_data_collector.panel.s7_tag_form import ENABLED_ERROR, ENABLED_WORDS
from plc_data_collector.panel.tag_forms import check_name_and_group, metadata_with_unit
from plc_data_collector.protocols.mbus_tags import (
    MBUS_PROTOCOL,
    QUALIFIERS,
    Function,
    MBusTagOptions,
    Quantity,
    canonical_meter_address,
    display_meter_address,
    parse_meter_address,
    register_key,
    units_for,
    value_type_of,
)

_MAX_STORAGE = 2**40
_MAX_TARIFF = 2**20
_MAX_SUBUNIT = 2**10
_MAX_OCCURRENCE = 32
BATTERY_INTERVAL_MS = 60_000
QUALIFIER_NAMES: tuple[str, ...] = tuple(sorted(QUALIFIERS))
QUALIFIER_SEPARATOR = "+"
QUANTITIES: tuple[str, ...] = tuple(quantity.value for quantity in Quantity)
FUNCTIONS: tuple[str, ...] = tuple(function.value for function in Function)
# The fields of the form; qualifiers are one checkbox each, ``qualifier_<name>``.
FORM_FIELDS: tuple[str, ...] = (
    "name",
    "tag_group",
    "meter",
    "quantity",
    "unit",
    "storage",
    "tariff",
    "subunit",
    "function",
    "occurrence",
    "meter_id",
)

METER_REQUIRED_ERROR = "Enter the meter address."
QUANTITY_ERROR = f"Choose a quantity: {', '.join(QUANTITIES)}."
FUNCTION_ERROR = f"Choose a function: {', '.join(FUNCTIONS)}."
QUALIFIER_ERROR = "Unknown qualifier {names}; use {allowed}."
QUALIFIER_REPEATED_ERROR = "Each qualifier may appear once."
METER_ID_ERROR = "Enter the meter's 8-digit identification number."
RANGE_ERROR = "Enter a whole number from {low} to {high}."
BATTERY_WARNING = (
    "This group polls M-Bus meters every {seconds} s. Battery-powered meters may "
    "drain or stop answering when read more often than once a minute."
)


def qualifier_field(name: str) -> str:
    """Return the form field (checkbox) name of a qualifier."""
    return f"qualifier_{name}"


def battery_warning(group: TagGroupConfig, connection: ConnectionConfig) -> str | None:
    """Return the warning for an M-Bus group read more often than once a minute.

    Callers show it only when the group has Tags; it never blocks saving.
    """
    if connection.protocol != MBUS_PROTOCOL or group.interval_ms >= BATTERY_INTERVAL_MS:
        return None
    return BATTERY_WARNING.format(seconds=f"{group.interval_ms / 1000:g}")


def group_battery_warning(
    config: CollectorConfig, connection: ConnectionConfig, group_id: str | None
) -> str | None:
    """Return ``battery_warning`` for a saved Tag Group that has Tags."""
    group = next((g for g in config.tag_groups if g.id == group_id), None)
    if group is None or group.connection_id != connection.id:
        return None
    if not any(tag.tag_group_id == group.id for tag in config.tags):
        return None
    return battery_warning(group, connection)


def _known_quantity(raw: object) -> Quantity | None:
    try:
        return Quantity(str(raw).strip().lower())
    except ValueError:
        return None


class MBusTagForm:
    """The Tag form of M-Bus Connections."""

    template = "_tag_form_mbus.html"
    columns: tuple[str, ...] = (
        "name",
        "meter",
        "quantity",
        "unit",
        "storage",
        "tariff",
        "subunit",
        "function",
        "qualifiers",
        "occurrence",
        "meter_id",
        "tag_group",
        "enabled",
    )
    required_columns: tuple[str, ...] = ("name", "meter", "quantity", "unit")
    # The Read from column shows the meter.
    read_from_column = True
    duplicate_noun = "record"

    def defaults(
        self, connection: ConnectionConfig, group_choices: list[tuple[str, str]]
    ) -> dict[str, object]:
        """Return the values a new Tag form starts with: no quantity chosen."""
        values: dict[str, object] = dict.fromkeys(FORM_FIELDS, "")
        values["tag_group"] = group_choices[0][0] if group_choices else ""
        values.update(storage="0", tariff="0", subunit="0", occurrence="1")
        values["function"] = Function.INSTANTANEOUS.value
        for name in QUALIFIER_NAMES:
            values[qualifier_field(name)] = False
        values["enabled"] = True
        return values

    def values(self, tag: TagConfig, connection: ConnectionConfig) -> dict[str, object]:
        """Return the form values of a saved Tag."""
        values = self.defaults(connection, [])
        values["name"] = tag.name
        values["tag_group"] = tag.tag_group_id
        values["meter"] = tag.address
        values["enabled"] = tag.enabled
        try:
            options = MBusTagOptions.model_validate(dict(tag.protocol_options))
        except ValidationError:
            raw = tag.protocol_options
            for name in ("quantity", "unit", "meter_id"):
                values[name] = str(raw.get(name) or "")
            return values
        values["quantity"] = options.quantity.value
        values["unit"] = options.unit or ""
        values["storage"] = str(options.storage)
        values["tariff"] = str(options.tariff)
        values["subunit"] = str(options.subunit)
        values["function"] = options.function.value
        values["occurrence"] = str(options.occurrence)
        values["meter_id"] = options.meter_id or ""
        for name in options.qualifiers:
            values[qualifier_field(name)] = True
        return values

    def values_from_data(self, data: Mapping[str, str]) -> dict[str, object]:
        """Return submitted Tag values to show again."""
        values: dict[str, object] = {name: data.get(name, "") for name in FORM_FIELDS}
        for name in QUALIFIER_NAMES:
            values[qualifier_field(name)] = bool(data.get(qualifier_field(name)))
        values["enabled"] = bool(data.get("enabled"))
        return values

    def build(
        self,
        data: Mapping[str, str],
        *,
        connection: ConnectionConfig,
        tag_id: str | None,
        allowed_group_ids: Collection[str],
        existing: TagConfig | None,
        enabled: bool | None = None,
    ) -> TagConfig:
        """Build an M-Bus Tag from submitted ``data``.

        With a ``qualifiers`` key (a pasted row) the qualifiers come from it,
        joined with ``+``; otherwise from the ``qualifier_<name>`` checkboxes.
        Blank Storage, Tariff, Subunit, Function and Occurrence take their
        defaults. Metadata other than the unit is kept from ``existing``.
        Raises ``FormErrors`` keyed by field name.
        """
        errors: dict[str, str] = {}
        name, group_id = check_name_and_group(data, allowed_group_ids, errors)
        meter = _meter(data, errors)
        quantity = _known_quantity(data.get("quantity", ""))
        if quantity is None:
            errors["quantity"] = QUANTITY_ERROR
            unit = None
        else:
            unit = _unit(quantity, data, errors)
        storage = _whole(data, "storage", 0, _MAX_STORAGE, 0, errors)
        tariff = _whole(data, "tariff", 0, _MAX_TARIFF, 0, errors)
        subunit = _whole(data, "subunit", 0, _MAX_SUBUNIT, 0, errors)
        occurrence = _whole(data, "occurrence", 1, _MAX_OCCURRENCE, 1, errors)
        function = _function(data, errors)
        qualifiers = _qualifiers(data, errors)
        meter_id = data.get("meter_id", "").strip() or None
        if meter_id is not None and not (
            len(meter_id) == 8 and _parse_digits(meter_id) is not None
        ):
            errors["meter_id"] = METER_ID_ERROR
        if enabled is None:
            enabled = ENABLED_WORDS.get(data.get("enabled", "").strip().lower())
            if enabled is None:
                errors["enabled"] = ENABLED_ERROR

        if errors or tag_id is None or meter is None or quantity is None:
            raise FormErrors(errors)
        try:
            options = MBusTagOptions(
                quantity=quantity,
                unit=unit,
                storage=storage,
                tariff=tariff,
                subunit=subunit,
                function=function,
                qualifiers=qualifiers,
                occurrence=occurrence,
                meter_id=meter_id,
            ).to_options()
        except ValidationError as error:
            raise FormErrors({"form": str(error)}) from None
        try:
            return TagConfig(
                id=tag_id,
                tag_group_id=group_id,
                name=name,
                address=meter,
                value_type=value_type_of(quantity),
                enabled=True if enabled is None else enabled,
                metadata=metadata_with_unit(existing, {"unit": unit or ""}),
                protocol_options=options,
            )
        except ConfigurationError as error:
            raise FormErrors({"form": error.message}) from None

    def next_values(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object] | None:
        """Return the next new-Tag form: the same meter, a record to choose."""
        values = self.defaults(connection, [])
        current = self.values(tag, connection)
        for name in ("tag_group", "meter", "meter_id", "enabled"):
            values[name] = current[name]
        return values

    def table_cells(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> tuple[str, str, str]:
        """Return the meter, the record (quantity, storage, tariff...) and unit."""
        try:
            meter = display_meter_address(parse_meter_address(tag.address))
            options = MBusTagOptions.model_validate(dict(tag.protocol_options))
        except (ConfigurationError, ValidationError):
            raw = tag.protocol_options
            return (tag.address, str(raw.get("quantity", "")), str(raw.get("unit", "")))
        parts = [options.quantity.value]
        for label, value, default in (
            ("storage", options.storage, 0),
            ("tariff", options.tariff, 0),
            ("subunit", options.subunit, 0),
        ):
            if value != default:
                parts.append(f"{label} {value}")
        if options.function is not Function.INSTANTANEOUS:
            parts.append(options.function.value)
        if options.qualifiers:
            parts.append(QUALIFIER_SEPARATOR.join(options.qualifiers))
        if options.occurrence != 1:
            parts.append(f"occurrence {options.occurrence}")
        return (meter, ", ".join(parts), options.unit or "")

    def register_key(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> tuple[object, ...]:
        """Return the meter and record key: Tags that read the same record."""
        try:
            return register_key(tag)
        except ConfigurationError:
            return (tag.address,)

    def template_context(
        self, values: Mapping[str, object], connection: ConnectionConfig
    ) -> dict[str, object]:
        """Return the choices of the form; units follow the chosen quantity."""
        quantity = _known_quantity(values.get("quantity", ""))
        advanced_open = (
            str(values.get("subunit", "")).strip() not in ("", "0")
            or str(values.get("function", "")).strip()
            not in ("", Function.INSTANTANEOUS.value)
            or str(values.get("occurrence", "")).strip() not in ("", "1")
            or bool(str(values.get("meter_id", "")).strip())
            or any(values.get(qualifier_field(name)) for name in QUALIFIER_NAMES)
        )
        return {
            "quantities": QUANTITIES,
            "quantity_known": quantity is not None,
            "units": units_for(quantity) if quantity is not None else (),
            "functions": FUNCTIONS,
            "qualifiers": [(name, qualifier_field(name)) for name in QUALIFIER_NAMES],
            "advanced_open": advanced_open,
        }


def _meter(data: Mapping[str, str], errors: dict[str, str]) -> str | None:
    raw = data.get("meter", "").strip()
    if not raw:
        errors["meter"] = METER_REQUIRED_ERROR
        return None
    try:
        return canonical_meter_address(parse_meter_address(raw))
    except ConfigurationError as error:
        errors["meter"] = error.message
        return None


def _unit(
    quantity: Quantity, data: Mapping[str, str], errors: dict[str, str]
) -> str | None:
    """Return the unit in its own spelling (``mwh`` gives ``MWh``)."""
    raw = data.get("unit", "").strip()
    units = units_for(quantity)
    if not units:
        if raw:
            errors["unit"] = f"{quantity} has no unit; leave Unit empty."
        return None
    unit = {u.lower(): u for u in units}.get(raw.lower())
    if unit is None:
        errors["unit"] = f"Choose a unit of {quantity}: {', '.join(units)}."
    return unit


def _whole(
    data: Mapping[str, str],
    name: str,
    low: int,
    high: int,
    default: int,
    errors: dict[str, str],
) -> int:
    raw = data.get(name, "").strip()
    if not raw:
        return default
    number = _parse_digits(raw) if len(raw) <= 16 else None
    if number is None or not low <= number <= high:
        errors[name] = RANGE_ERROR.format(low=low, high=high)
        return default
    return number


def _function(data: Mapping[str, str], errors: dict[str, str]) -> Function:
    raw = data.get("function", "").strip().lower()
    if not raw:
        return Function.INSTANTANEOUS
    try:
        return Function(raw)
    except ValueError:
        errors["function"] = FUNCTION_ERROR
        return Function.INSTANTANEOUS


def _qualifiers(data: Mapping[str, str], errors: dict[str, str]) -> tuple[str, ...]:
    if "qualifiers" not in data:
        return tuple(
            name for name in QUALIFIER_NAMES if data.get(qualifier_field(name))
        )
    names = [
        part.strip().lower()
        for part in data["qualifiers"].split(QUALIFIER_SEPARATOR)
        if part.strip()
    ]
    unknown = [name for name in names if name not in QUALIFIERS]
    if unknown:
        errors["qualifiers"] = QUALIFIER_ERROR.format(
            names=", ".join(unknown), allowed=", ".join(QUALIFIER_NAMES)
        )
        return ()
    if len(set(names)) != len(names):
        errors["qualifiers"] = QUALIFIER_REPEATED_ERROR
        return ()
    return tuple(sorted(names))
