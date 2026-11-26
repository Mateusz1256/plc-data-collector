"""The Modbus Tag form: Read from, address in the Connection's numbering, data type.

The form shows and takes addresses in the Connection's ``address_numbering``
(zero-based, one-based or Modicon); the stored address is always zero-based
(``hr:120``). ``resolve_tag`` stays the final check, and its errors are shown
on the field they concern.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping

from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.panel.protocol_forms import FormErrors, form_for
from plc_data_collector.panel.tag_forms import (
    CHOICE_ERROR,
    check_name_and_group,
    metadata_with_unit,
    tag_unit,
)
from plc_data_collector.protocols.modbus_tags import (
    MAX_REGISTERS,
    MODBUS_PROTOCOL,
    AddressNumbering,
    Area,
    ByteOrder,
    DataType,
    ModbusAddress,
    ModbusConnectionOptions,
    ModbusTagOptions,
    WordOrder,
    display_address,
    parse_address,
    parse_display_address,
    resolve_tag,
)

_ADDRESS_SPACE = 65536
BIT_TYPES: tuple[str, ...] = (DataType.BOOL.value,)
REGISTER_TYPES: tuple[str, ...] = (
    DataType.INT16.value,
    DataType.UINT16.value,
    DataType.INT32.value,
    DataType.UINT32.value,
    DataType.INT64.value,
    DataType.UINT64.value,
    DataType.FLOAT32.value,
    DataType.FLOAT64.value,
    DataType.BIT.value,
    DataType.STRING.value,
)

ADDRESS_REQUIRED_ERROR = "Enter an address."
BIT_RANGE_ERROR = "Enter a bit number from 0 to 15."
BIT_REQUIRED_ERROR = "Enter the bit number (0-15) for data type bit."
BIT_UNUSED_ERROR = "A bit number is used only with data type bit."
LENGTH_RANGE_ERROR = f"Enter a whole number of registers from 1 to {MAX_REGISTERS}."
LENGTH_REQUIRED_ERROR = "Enter the length in registers for data type string."
LENGTH_UNUSED_ERROR = "Length is used only with data type string."
UNIT_ID_ERROR = "Enter a whole number from 0 to 255."
NUMBER_ERROR = "Enter a number, for example 0.1."
SCALE_ZERO_ERROR = "Scale must not be 0."
BOOL_IN_REGISTERS_ERROR = (
    "bool is only for coils and discrete inputs. For one bit of a register, "
    "choose bit and enter the bit number."
)

_NUMBER_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_AREA_NOUN = {
    Area.COILS: "coil",
    Area.DISCRETE_INPUTS: "input",
    Area.HOLDING_REGISTERS: "register",
    Area.INPUT_REGISTERS: "register",
}


def parse_read_from(text: str) -> Area | None:
    """Return the area named by ``text``, or None.

    Takes ``co``/``di``/``hr``/``ir``, ``FC01`` to ``FC04`` and the area
    labels such as ``Holding registers (FC03)``, ignoring case.
    """
    wanted = text.strip().lower()
    for area in Area:
        names = (area.value, f"fc{area.function_code:02d}", area.label.lower())
        if wanted in names:
            return area
    return None


def data_types_for(area: Area | None) -> tuple[str, ...]:
    """Return the data types a Tag in ``area`` can have (all when unknown)."""
    if area is None:
        return (*BIT_TYPES, *REGISTER_TYPES)
    return BIT_TYPES if area.is_bit_area else REGISTER_TYPES


def connection_options(connection: ConnectionConfig) -> ModbusConnectionOptions:
    """Return the Connection's Modbus options, or the defaults if invalid."""
    try:
        return ModbusConnectionOptions.from_options(connection.protocol_options)
    except ConfigurationError:
        return ModbusConnectionOptions()


def address_help(area: Area | None, numbering: AddressNumbering) -> str:
    """Return the help text of the Address field for this numbering."""
    noun = _AREA_NOUN[area] if area is not None else "register"
    if numbering is AddressNumbering.ZERO:
        return f"Zero-based {noun} number."
    if numbering is AddressNumbering.ONE:
        return f"One-based {noun} number."
    digit = area.modicon_digit if area is not None else "4"
    return f"Modicon address, for example {digit}0001."


def span_hint(
    area: Area | None,
    address: str,
    data_type: str,
    length: str,
    options: ModbusConnectionOptions,
) -> str | None:
    """Return "float32 uses 2 registers: 40121 and 40122." or None.

    Shown only for register areas and multi-bit types whose address parses.
    """
    if area is None or area.is_bit_area or data_type not in REGISTER_TYPES:
        return None
    if data_type == DataType.BIT.value:
        return None
    numbering = options.address_numbering
    try:
        start = parse_display_address(area, address, numbering)
        tag_options: dict[str, object] = {"data_type": data_type}
        if data_type == DataType.STRING.value:
            tag_options["length"] = whole_number(length, 1, MAX_REGISTERS)
        plan = resolve_tag(str(ModbusAddress(area, start)), tag_options, options)
    except ConfigurationError:
        return None
    first = display_address(area, start, numbering)
    last = display_address(area, start + plan.span - 1, numbering)
    if plan.span == 1:
        return f"{data_type} uses 1 register: {first}."
    joiner = "and" if plan.span == 2 else "to"
    return f"{data_type} uses {plan.span} registers: {first} {joiner} {last}."


class ModbusTagForm:
    """The Tag form of Modbus Connections."""

    template = "_tag_form_modbus.html"
    columns: tuple[str, ...] = (
        "name",
        "tag_group",
        "read_from",
        "address",
        "data_type",
        "length",
        "bit",
        "byte_order",
        "word_order",
        "unit_id",
        "scale",
        "offset",
        "unit",
    )
    required_columns: tuple[str, ...] = ("name", "read_from", "address", "data_type")
    read_from_column = True

    def defaults(
        self, connection: ConnectionConfig, group_choices: list[tuple[str, str]]
    ) -> dict[str, object]:
        """Return the values a new Tag form starts with."""
        values: dict[str, object] = dict.fromkeys(self.columns, "")
        values["tag_group"] = group_choices[0][0] if group_choices else ""
        values["read_from"] = Area.HOLDING_REGISTERS.value
        values["data_type"] = DataType.INT16.value
        values["enabled"] = True
        return values

    def values(self, tag: TagConfig, connection: ConnectionConfig) -> dict[str, object]:
        """Return the form values of a saved Tag, in the Connection's numbering."""
        values: dict[str, object] = dict.fromkeys(self.columns, "")
        values["name"] = tag.name
        values["tag_group"] = tag.tag_group_id
        values["unit"] = tag_unit(tag)
        values["enabled"] = tag.enabled
        try:
            parsed = parse_address(tag.address)
        except ConfigurationError:
            values["address"] = tag.address
        else:
            numbering = connection_options(connection).address_numbering
            values["read_from"] = parsed.area.value
            values["address"] = display_address(parsed.area, parsed.address, numbering)
            values["bit"] = "" if parsed.bit is None else str(parsed.bit)
        options = tag.protocol_options
        values["data_type"] = str(options.get("data_type", ""))
        for name in ("length", "byte_order", "word_order", "unit_id"):
            value = options.get(name)
            values[name] = "" if value is None else str(value)
        for name in ("scale", "offset"):
            values[name] = format_number(options.get(name))
        return values

    def values_from_data(self, data: Mapping[str, str]) -> dict[str, object]:
        """Return submitted Tag values to show again."""
        values: dict[str, object] = {name: data.get(name, "") for name in self.columns}
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
        """Build a Modbus Tag from submitted ``data``.

        The address is read in the Connection's numbering and stored
        zero-based; empty override fields mean "use the Connection setting".
        Metadata other than the unit is kept from ``existing``. Raises
        ``FormErrors`` keyed by field name.
        """
        errors: dict[str, str] = {}
        name, group_id = check_name_and_group(data, allowed_group_ids, errors)
        try:
            connection_opts = ModbusConnectionOptions.from_options(
                connection.protocol_options
            )
        except ConfigurationError as error:
            raise FormErrors({"form": error.message}) from None

        area = parse_read_from(data.get("read_from", ""))
        if area is None:
            errors["read_from"] = CHOICE_ERROR
        data_type = _choice(data, "data_type", DataType, errors)
        if area is not None and data_type is not None:
            if area.is_bit_area and data_type is not DataType.BOOL:
                errors["data_type"] = f"{area.label} hold single bits; choose bool."
            elif not area.is_bit_area and data_type is DataType.BOOL:
                errors["data_type"] = BOOL_IN_REGISTERS_ERROR

        address: int | None = None
        raw_address = data.get("address", "").strip()
        if not raw_address:
            errors["address"] = ADDRESS_REQUIRED_ERROR
        elif area is not None:
            try:
                address = parse_display_address(
                    area, raw_address, connection_opts.address_numbering
                )
            except ConfigurationError as error:
                errors["address"] = error.message

        bit = optional_whole(data, "bit", 0, 15, BIT_RANGE_ERROR, errors)
        if "bit" not in errors and data_type is not None:
            if data_type is DataType.BIT and bit is None:
                errors["bit"] = BIT_REQUIRED_ERROR
            elif data_type is not DataType.BIT and bit is not None:
                errors["bit"] = BIT_UNUSED_ERROR
        length = optional_whole(
            data, "length", 1, MAX_REGISTERS, LENGTH_RANGE_ERROR, errors
        )
        if "length" not in errors and data_type is not None:
            if data_type is DataType.STRING and length is None:
                errors["length"] = LENGTH_REQUIRED_ERROR
            elif data_type is not DataType.STRING and length is not None:
                errors["length"] = LENGTH_UNUSED_ERROR
        unit_id = optional_whole(data, "unit_id", 0, 255, UNIT_ID_ERROR, errors)
        byte_order = _choice(data, "byte_order", ByteOrder, errors, optional=True)
        word_order = _choice(data, "word_order", WordOrder, errors, optional=True)
        scale = optional_number(data, "scale", errors)
        if scale == 0:
            errors["scale"] = SCALE_ZERO_ERROR
        offset = optional_number(data, "offset", errors)
        if data_type in _UNSCALED_TYPES:
            if scale is not None and "scale" not in errors:
                errors["scale"] = f"Scale isn't used with {data_type}."
            if offset is not None and "offset" not in errors:
                errors["offset"] = f"Offset isn't used with {data_type}."

        if errors or tag_id is None or area is None or address is None:
            raise FormErrors(errors)
        assert data_type is not None  # an unknown type is an error above
        options = ModbusTagOptions(
            data_type=data_type,
            length=length,
            byte_order=byte_order,
            word_order=word_order,
            unit_id=unit_id,
            scale=scale,
            offset=offset,
        ).to_options()
        stored_address = str(ModbusAddress(area, address, bit))
        try:
            plan = resolve_tag(stored_address, options, connection_opts)
        except ConfigurationError as error:
            raise FormErrors(_resolve_errors(error)) from None
        try:
            return TagConfig(
                id=tag_id,
                tag_group_id=group_id,
                name=name,
                address=stored_address,
                value_type=plan.value_type,
                enabled=True if enabled is None else enabled,
                metadata=metadata_with_unit(existing, data),
                protocol_options=options,
            )
        except ConfigurationError as error:
            raise FormErrors({"form": error.message}) from None

    def next_values(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object] | None:
        """Return the next new-Tag form: same settings, the following address.

        The address moves on by the Tag's span (a ``bit`` Tag moves to the
        next bit); past the end of the address space it is left empty.
        """
        values = self.values(tag, connection)
        values["name"] = ""
        values["address"] = ""
        options = connection_options(connection)
        try:
            plan = resolve_tag(tag.address, tag.protocol_options, options)
        except ConfigurationError:
            return values
        address, bit = plan.address + plan.span, plan.bit
        if plan.bit is not None:
            address, bit = (plan.address, plan.bit + 1)
            if bit > 15:
                address, bit = plan.address + 1, 0
            values["bit"] = str(bit)
        if address < _ADDRESS_SPACE:
            values["address"] = display_address(
                plan.area, address, options.address_numbering
            )
        return values

    def table_cells(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> tuple[str, str, str]:
        """Return Read from, the address in the current numbering, and data type."""
        data_type = str(tag.protocol_options.get("data_type", tag.value_type.value))
        try:
            parsed = parse_address(tag.address)
        except ConfigurationError:
            return ("", tag.address, data_type)
        numbering = connection_options(connection).address_numbering
        shown = display_address(parsed.area, parsed.address, numbering)
        if parsed.bit is not None:
            shown = f"{shown}.{parsed.bit}"
        return (parsed.area.label, shown, data_type)

    def register_key(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> tuple[str, int]:
        """Return the address and the Modbus unit the Tag reads from."""
        unit = tag.protocol_options.get("unit_id")
        if isinstance(unit, int):
            return (tag.address, unit)
        return (tag.address, connection_options(connection).unit_id)

    def template_context(
        self, values: Mapping[str, object], connection: ConnectionConfig
    ) -> dict[str, object]:
        """Return the choices, help texts and span hint of the form."""
        options = connection_options(connection)
        area = parse_read_from(str(values.get("read_from", "")))
        fields = {field.name: field for field in form_for(MODBUS_PROTOCOL).fields}
        byte_labels = dict(fields["byte_order"].choices)
        word_labels = dict(fields["word_order"].choices)
        return {
            "areas": [(a.value, a.label) for a in Area],
            "selected_area": area.value if area is not None else "",
            "data_types": data_types_for(area),
            "address_help": address_help(area, options.address_numbering),
            "span_hint": span_hint(
                area,
                str(values.get("address", "")),
                str(values.get("data_type", "")),
                str(values.get("length", "")),
                options,
            ),
            "byte_orders": [
                ("", f"Connection setting ({byte_labels[options.byte_order.value]})"),
                *byte_labels.items(),
            ],
            "word_orders": [
                ("", f"Connection setting ({word_labels[options.word_order.value]})"),
                *word_labels.items(),
            ],
            "connection_unit_id": options.unit_id,
            "advanced_open": any(values.get(name) for name in _ADVANCED_FIELDS),
        }


_UNSCALED_TYPES = (DataType.BOOL, DataType.BIT, DataType.STRING)
_ADVANCED_FIELDS = ("byte_order", "word_order", "unit_id", "scale", "offset")


def _choice[E: (DataType, ByteOrder, WordOrder)](
    data: Mapping[str, str],
    name: str,
    enum: type[E],
    errors: dict[str, str],
    *,
    optional: bool = False,
) -> E | None:
    raw = data.get(name, "").strip().lower()
    if not raw and optional:
        return None
    try:
        return enum(raw)
    except ValueError:
        errors[name] = CHOICE_ERROR
        return None


def whole_number(raw: str, low: int, high: int) -> int | None:
    """Return ``raw`` as a whole number in ``low``-``high``, or None."""
    text = raw.strip()
    if not text or len(text) > 6 or not (text.isascii() and text.isdigit()):
        return None
    number = int(text)
    return number if low <= number <= high else None


def optional_whole(
    data: Mapping[str, str],
    name: str,
    low: int,
    high: int,
    message: str,
    errors: dict[str, str],
) -> int | None:
    """Parse an optional whole number field; record ``message`` when invalid."""
    raw = data.get(name, "").strip()
    if not raw:
        return None
    number = whole_number(raw, low, high)
    if number is None:
        errors[name] = message
    return number


def optional_number(
    data: Mapping[str, str], name: str, errors: dict[str, str]
) -> float | None:
    """Parse a decimal number; ``.`` and ``,`` both work as decimal separator."""
    raw = data.get(name, "").strip()
    if not raw:
        return None
    if len(raw) > 64 or not _NUMBER_RE.fullmatch(raw):
        errors[name] = NUMBER_ERROR
        return None
    number = float(raw.replace(",", "."))
    if not math.isfinite(number):
        errors[name] = NUMBER_ERROR
        return None
    return number


def format_number(value: object) -> str:
    """Return a stored number as the form shows it (``0.1``, ``2``)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return ""
    text = repr(float(value))
    return text.removesuffix(".0")


def _resolve_errors(error: ConfigurationError) -> dict[str, str]:
    """Put a ``resolve_tag`` error on the field it concerns."""
    problem = error.message.split(": ", 1)[-1]
    problem = problem[:1].upper() + problem[1:]
    lowered = problem.lower()
    if "length" in lowered:
        field = "length"
    elif "bit address" in lowered:
        field = "bit"
    elif "scale" in lowered or "offset" in lowered:
        field = "scale"
    elif "data_type" in lowered:
        field = "data_type"
    else:
        field = "address"
    return {field: problem}
