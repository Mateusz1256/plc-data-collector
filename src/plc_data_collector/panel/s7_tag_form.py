"""The S7 Tag form: area, DB number, byte, bit and data type, plus STEP7 input.

The form takes the address in separate fields; a pasted CSV row (and the
form's "Paste address" box) takes one address in STEP7 or canonical form,
read with ``parse_step7``. A STEP7 size letter that contradicts the data type
is an error, never a silent fix. The stored address is always canonical
(``DB10.4``) and ``resolve_tag`` stays the final check.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from plc_data_collector.domain import ConfigurationError, ConnectionConfig, TagConfig
from plc_data_collector.panel.modbus_tag_form import (
    SCALE_ZERO_ERROR,
    format_number,
    optional_number,
    optional_whole,
    whole_number,
)
from plc_data_collector.panel.protocol_forms import FormErrors
from plc_data_collector.panel.tag_forms import (
    CHOICE_ERROR,
    check_name_and_group,
    metadata_with_unit,
    tag_unit,
)
from plc_data_collector.protocols.s7_tags import (
    MAX_STRING_LENGTH,
    Area,
    DataType,
    S7Address,
    S7TagOptions,
    byte_span,
    display_address,
    parse_address,
    parse_step7,
    resolve_tag,
)

_ADDRESS_SPACE = 65536
_MAX_DB = 65535
_MAX_BIT = 7
DATA_TYPES: tuple[str, ...] = tuple(data_type.value for data_type in DataType)
# The fields of the form, besides the "Read this tag" box.
FORM_FIELDS: tuple[str, ...] = (
    "name",
    "tag_group",
    "area",
    "db",
    "byte",
    "bit",
    "data_type",
    "length",
    "scale",
    "offset",
    "unit",
    "paste_address",
)

ADDRESS_REQUIRED_ERROR = "Enter an address."
DB_ERROR = f"Enter a DB number from 1 to {_MAX_DB}."
BYTE_ERROR = f"Enter a byte number from 0 to {_ADDRESS_SPACE - 1}."
BIT_RANGE_ERROR = f"Enter a bit number from 0 to {_MAX_BIT}."
BIT_REQUIRED_ERROR = "Enter the bit number (0-7) for data type bool."
BIT_UNUSED_ERROR = "A bit number is used only with data type bool."
LENGTH_RANGE_ERROR = f"Enter a length from 1 to {MAX_STRING_LENGTH} characters."
LENGTH_REQUIRED_ERROR = (
    f"Enter the length in characters (1-{MAX_STRING_LENGTH}) for data type string."
)
LENGTH_UNUSED_ERROR = "Length is used only with data type string."
ENABLED_ERROR = "Enter yes or no."
PASTE_REQUIRED_ERROR = "Enter an address, for example %DB10.DBD4 or %MW20."

_ENABLED_WORDS = {
    "": True,
    "yes": True,
    "y": True,
    "true": True,
    "1": True,
    "on": True,
    "no": False,
    "n": False,
    "false": False,
    "0": False,
    "off": False,
}
# The data type a size letter suggests when none is chosen (X, B, W, D).
_IMPLIED_TYPES = {
    0: DataType.BOOL,
    1: DataType.BYTE,
    2: DataType.WORD,
    4: DataType.DWORD,
}
_UNSCALED_TYPES = (DataType.BOOL, DataType.CHAR, DataType.STRING)
_ADVANCED_FIELDS = ("scale", "offset")


def size_letter_error(text: str, size: int | None, data_type: DataType) -> str | None:
    """Return why the address ``text`` does not fit ``data_type``, or None.

    ``size`` is what ``parse_step7`` returned: the bytes its size letter names,
    ``0`` for a bit address, None for a byte address without a letter. For
    ``string`` the letter is not compared (B is how TIA shows strings).
    """
    if data_type is DataType.BOOL:
        if size == 0:
            return None
        if size is None:
            return f"{text} has no bit; bool needs a bit address (.0-.7)."
        return f"{text} is a {size}-byte address; bool needs a bit address (.0-.7)."
    if size == 0:
        return f"{text} is a bit address; {data_type} needs {_need(data_type)}."
    if size is None or data_type is DataType.STRING:
        return None
    span = byte_span(data_type, None)
    if size != span:
        return f"{text} is a {size}-byte address; {data_type} needs {_need(data_type)}."
    return None


def _need(data_type: DataType) -> str:
    if data_type is DataType.STRING:
        return "a byte address"
    span = byte_span(data_type, None)
    return "1 byte" if span == 1 else f"{span} bytes"


def _known_type(raw: object) -> DataType | None:
    try:
        return DataType(str(raw).strip().lower())
    except ValueError:
        return None


class S7TagForm:
    """The Tag form of S7 Connections."""

    template = "_tag_form_s7.html"
    columns: tuple[str, ...] = (
        "name",
        "address",
        "data_type",
        "length",
        "scale",
        "offset",
        "unit",
        "tag_group",
        "enabled",
    )
    required_columns: tuple[str, ...] = ("name", "address", "data_type")
    read_from_column = True

    def defaults(
        self, connection: ConnectionConfig, group_choices: list[tuple[str, str]]
    ) -> dict[str, object]:
        """Return the values a new Tag form starts with: a data block, no type."""
        values: dict[str, object] = dict.fromkeys(FORM_FIELDS, "")
        values["tag_group"] = group_choices[0][0] if group_choices else ""
        values["area"] = Area.DB.value
        values["enabled"] = True
        return values

    def values(self, tag: TagConfig, connection: ConnectionConfig) -> dict[str, object]:
        """Return the form values of a saved Tag."""
        values: dict[str, object] = dict.fromkeys(FORM_FIELDS, "")
        values["name"] = tag.name
        values["tag_group"] = tag.tag_group_id
        values["unit"] = tag_unit(tag)
        values["enabled"] = tag.enabled
        try:
            parsed = parse_address(tag.address)
        except ConfigurationError:
            values["paste_address"] = tag.address
        else:
            values["area"] = parsed.area.value
            values["db"] = "" if parsed.db is None else str(parsed.db)
            values["byte"] = str(parsed.byte)
            values["bit"] = "" if parsed.bit is None else str(parsed.bit)
        options = tag.protocol_options
        values["data_type"] = str(options.get("data_type", ""))
        length = options.get("length")
        values["length"] = "" if length is None else str(length)
        for name in ("scale", "offset"):
            values[name] = format_number(options.get(name))
        return values

    def values_from_data(self, data: Mapping[str, str]) -> dict[str, object]:
        """Return submitted Tag values to show again."""
        values: dict[str, object] = {name: data.get(name, "") for name in FORM_FIELDS}
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
        """Build an S7 Tag from submitted ``data``.

        With an ``address`` key (a pasted row) the address is read in STEP7 or
        canonical form and ``enabled`` may come from the data; otherwise from
        the Area, DB number, Byte and Bit fields. Metadata other than the unit
        is kept from ``existing``. Raises ``FormErrors`` keyed by field name.
        """
        errors: dict[str, str] = {}
        name, group_id = check_name_and_group(data, allowed_group_ids, errors)
        data_type = _known_type(data.get("data_type", ""))
        if data_type is None:
            errors["data_type"] = CHOICE_ERROR
        from_text = "address" in data
        if from_text:
            address = _address_from_text(data, data_type, errors)
        else:
            address = _address_from_fields(data, data_type, errors)

        length = optional_whole(
            data, "length", 1, MAX_STRING_LENGTH, LENGTH_RANGE_ERROR, errors
        )
        if "length" not in errors and data_type is not None:
            if data_type is DataType.STRING and length is None:
                errors["length"] = LENGTH_REQUIRED_ERROR
            elif data_type is not DataType.STRING and length is not None:
                errors["length"] = LENGTH_UNUSED_ERROR
        scale = optional_number(data, "scale", errors)
        if scale == 0:
            errors["scale"] = SCALE_ZERO_ERROR
        offset = optional_number(data, "offset", errors)
        if data_type in _UNSCALED_TYPES:
            if scale is not None and "scale" not in errors:
                errors["scale"] = f"Scale isn't used with {data_type}."
            if offset is not None and "offset" not in errors:
                errors["offset"] = f"Offset isn't used with {data_type}."
        if enabled is None:
            enabled = _ENABLED_WORDS.get(data.get("enabled", "").strip().lower())
            if enabled is None:
                errors["enabled"] = ENABLED_ERROR

        if errors or tag_id is None or address is None:
            raise FormErrors(errors)
        assert data_type is not None  # an unknown type is an error above
        options = S7TagOptions(
            data_type=data_type, length=length, scale=scale, offset=offset
        ).to_options()
        stored_address = str(address)
        try:
            plan = resolve_tag(stored_address, options)
        except ConfigurationError as error:
            field = "address" if from_text else "byte"
            raise FormErrors(_resolve_errors(error, field)) from None
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

        The byte moves on by the Tag's span; a ``bool`` moves to the next bit,
        bit 7 rolling over to bit 0 of the next byte. Past the end of the
        address space the byte is left empty.
        """
        values = self.values(tag, connection)
        values["name"] = ""
        values["paste_address"] = ""
        try:
            plan = resolve_tag(tag.address, tag.protocol_options)
        except ConfigurationError:
            values["byte"] = ""
            return values
        byte = plan.byte + plan.span
        if plan.bit is not None:
            byte, bit = plan.byte, plan.bit + 1
            if bit > _MAX_BIT:
                byte, bit = plan.byte + 1, 0
            values["bit"] = str(bit)
        values["byte"] = str(byte) if byte < _ADDRESS_SPACE else ""
        return values

    def table_cells(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> tuple[str, str, str]:
        """Return the area, the STEP7 address for the data type, and data type."""
        data_type = str(tag.protocol_options.get("data_type", tag.value_type.value))
        known = _known_type(data_type)
        try:
            parsed = parse_address(tag.address)
        except ConfigurationError:
            return ("", tag.address, data_type)
        if known is None:
            return (parsed.area.label, tag.address, data_type)
        return (parsed.area.label, display_address(parsed, known), data_type)

    def register_key(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> tuple[object, ...]:
        """Return the same key for Tags at the same area, DB, byte and bit."""
        try:
            parsed = parse_address(tag.address)
        except ConfigurationError:
            return (tag.address,)
        return (parsed.area.value, parsed.db, parsed.byte, parsed.bit)

    def template_context(
        self, values: Mapping[str, object], connection: ConnectionConfig
    ) -> dict[str, object]:
        """Return the area and data type choices of the form."""
        return {
            "areas": [(area.value, f"{area.label} ({area.name})") for area in Area],
            "data_types": DATA_TYPES,
            "advanced_open": any(values.get(name) for name in _ADVANCED_FIELDS),
        }

    def fill_from_address(
        self, values: Mapping[str, object]
    ) -> tuple[dict[str, object], dict[str, str]]:
        """Fill Area, DB number, Byte and Bit from the ``paste_address`` value.

        The data type is set from the size letter only when none is chosen;
        a chosen type that the letter contradicts is an error. On an error the
        fields are left as they were and the error is keyed ``paste_address``.
        """
        filled = dict(values)
        text = str(values.get("paste_address", "")).strip()
        if not text:
            return filled, {"paste_address": PASTE_REQUIRED_ERROR}
        try:
            address, size = parse_step7(text)
        except ConfigurationError as error:
            return filled, {"paste_address": error.message}
        chosen = _known_type(values.get("data_type", ""))
        if chosen is None:
            implied = None if size is None else _IMPLIED_TYPES.get(size)
            filled["data_type"] = "" if implied is None else implied.value
        else:
            problem = size_letter_error(text, size, chosen)
            if problem is not None:
                return filled, {"paste_address": problem}
        filled["area"] = address.area.value
        filled["db"] = "" if address.db is None else str(address.db)
        filled["byte"] = str(address.byte)
        filled["bit"] = "" if address.bit is None else str(address.bit)
        filled["paste_address"] = ""
        return filled, {}


def _address_from_text(
    data: Mapping[str, str], data_type: DataType | None, errors: dict[str, str]
) -> S7Address | None:
    raw = data.get("address", "").strip()
    if not raw:
        errors["address"] = ADDRESS_REQUIRED_ERROR
        return None
    try:
        address, size = parse_step7(raw)
    except ConfigurationError as error:
        errors["address"] = error.message
        return None
    if data_type is not None:
        problem = size_letter_error(raw, size, data_type)
        if problem is not None:
            errors["address"] = problem
            return None
    return address


def _address_from_fields(
    data: Mapping[str, str], data_type: DataType | None, errors: dict[str, str]
) -> S7Address | None:
    try:
        area: Area | None = Area(data.get("area", "").strip().lower())
    except ValueError:
        area = None
        errors["area"] = CHOICE_ERROR
    db = None
    if area is Area.DB:
        db = whole_number(data.get("db", ""), 1, _MAX_DB)
        if db is None:
            errors["db"] = DB_ERROR
    byte = whole_number(data.get("byte", ""), 0, _ADDRESS_SPACE - 1)
    if byte is None:
        errors["byte"] = BYTE_ERROR
    bit = optional_whole(data, "bit", 0, _MAX_BIT, BIT_RANGE_ERROR, errors)
    if "bit" not in errors and data_type is not None:
        if data_type is DataType.BOOL and bit is None:
            errors["bit"] = BIT_REQUIRED_ERROR
        elif data_type is not DataType.BOOL and bit is not None:
            errors["bit"] = BIT_UNUSED_ERROR
    if area is None or byte is None or (area is Area.DB and db is None):
        return None
    return S7Address(area, db, byte, bit)


def _resolve_errors(error: ConfigurationError, address_field: str) -> dict[str, str]:
    """Put a ``resolve_tag`` error on the address, or on the form."""
    problem = error.message.split(": ", 1)[-1]
    problem = problem[:1].upper() + problem[1:]
    if error.code == "s7_invalid_address":
        return {address_field: problem}
    return {"form": problem}
