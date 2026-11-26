"""Per-protocol Connection form specs and turning a submitted form into config.

A protocol's form is data: adding a protocol to the Panel means adding a
``ProtocolForm`` here, not a template.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from plc_data_collector.app.config import _mask_endpoint
from plc_data_collector.domain import ConfigurationError, ConnectionConfig
from plc_data_collector.protocols.modbus_tags import (
    format_endpoint,
    parse_endpoint,
)

FieldKind = Literal["text", "url", "int", "bool", "password", "select"]
FieldTarget = Literal["endpoint", "endpoint_host", "endpoint_port", "option", "secret"]

# IDs of Connections, Tag Groups and Tags created in the Panel.
ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")
ID_ERROR = "Use letters, digits, dot, dash or underscore (up to 128)."
ID_HELP = (
    "Used in the database and API. It can't be changed after the connection is created."
)
DEFAULT_TIMEOUT_MS = 3000
MAX_TIMEOUT_MS = 600_000
TIMEOUT_ERROR = "Enter a whole number of milliseconds from 1 to 600000."
INT_ERROR = "Enter a whole number."
SECRET_CONFLICT_ERROR = "Clear the new {label} or untick Remove, not both."
ENDPOINT_CREDENTIALS_ERROR = (
    "Remove the user name and password from the URL and use the "
    "User name and Password fields instead."
)


class FormErrors(Exception):
    """Errors in a submitted form, keyed by field name."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__("The form has errors.")
        self.errors = dict(errors)


@dataclass(frozen=True)
class FormField:
    """One protocol-specific input.

    ``target`` says where the value goes: the Connection's endpoint, a plain
    protocol option, or a secret option (stored encrypted, never rendered).
    """

    name: str
    label: str
    kind: FieldKind
    target: FieldTarget
    required: bool = False
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    default: object = None
    # For a url field: the allowed schemes; when set, a port is required too.
    schemes: tuple[str, ...] = ()
    # For an int field: the inclusive range of accepted values.
    min_value: int | None = None
    max_value: int | None = None
    # Rendered inside a collapsed "Advanced" section.
    advanced: bool = False


@dataclass(frozen=True)
class ProtocolForm:
    """The Connection form of one protocol, and how the picker shows it."""

    protocol: str
    label: str
    summary: str
    fields: tuple[FormField, ...]
    available: bool = True
    available_in: str | None = None
    # When set, the endpoint is ``<scheme>://host:port`` built from separate
    # ``endpoint_host`` and ``endpoint_port`` fields.
    endpoint_scheme: str | None = None

    @property
    def endpoint_field(self) -> FormField | None:
        """Return the field that holds the endpoint, if any."""
        return next((f for f in self.fields if f.target == "endpoint"), None)


PROTOCOL_FORMS: tuple[ProtocolForm, ...] = (
    ProtocolForm(
        protocol="opcua",
        label="OPC UA",
        summary="Endpoint URL, optional user and certificate",
        fields=(
            FormField(
                name="endpoint",
                label="Endpoint URL",
                kind="url",
                target="endpoint",
                required=True,
                help="For example opc.tcp://plc-sim:4840",
                schemes=("opc.tcp",),
            ),
            FormField(name="username", label="User name", kind="text", target="option"),
            FormField(
                name="password", label="Password", kind="password", target="secret"
            ),
            FormField(
                name="security_string",
                label="Security string",
                kind="text",
                target="option",
                help=(
                    "asyncua security string, for example "
                    "Basic256Sha256,SignAndEncrypt,cert.pem,key.pem. "
                    "Leave empty for no security."
                ),
            ),
            FormField(
                name="auto_reconnect",
                label="Let the OPC UA client reconnect on its own",
                kind="bool",
                target="option",
                default=False,
            ),
        ),
    ),
    ProtocolForm(
        protocol="modbus",
        label="Modbus TCP",
        summary="Host, port and unit ID",
        endpoint_scheme="tcp",
        fields=(
            FormField(
                name="host",
                label="Host",
                kind="text",
                target="endpoint_host",
                required=True,
                help="Host name or IP address, for example press2-plc",
            ),
            FormField(
                name="port",
                label="Port",
                kind="int",
                target="endpoint_port",
                required=True,
                default=502,
                min_value=1,
                max_value=65535,
            ),
            FormField(
                name="unit_id",
                label="Unit ID",
                kind="int",
                target="option",
                default=1,
                min_value=0,
                max_value=255,
            ),
            FormField(
                name="byte_order",
                label="Byte order",
                kind="select",
                target="option",
                default="ab",
                choices=(("ab", "Big-endian (AB)"), ("ba", "Little-endian (BA)")),
            ),
            FormField(
                name="word_order",
                label="Word order",
                kind="select",
                target="option",
                default="hi",
                choices=(("hi", "High word first"), ("lo", "Low word first")),
                help="For 32- and 64-bit values split across registers.",
            ),
            FormField(
                name="address_numbering",
                label="Address numbering",
                kind="select",
                target="option",
                default="zero",
                choices=(
                    ("zero", "Zero-based (0, 1, …)"),
                    ("one", "One-based (1, 2, …)"),
                    ("modicon", "Modicon (40001, 30001, …)"),
                ),
            ),
            FormField(
                name="max_registers_per_read",
                label="Max registers per request",
                kind="int",
                target="option",
                default=125,
                min_value=1,
                max_value=125,
                advanced=True,
            ),
            FormField(
                name="max_bits_per_read",
                label="Max coils/inputs per request",
                kind="int",
                target="option",
                default=2000,
                min_value=1,
                max_value=2000,
                advanced=True,
            ),
            FormField(
                name="max_gap",
                label="Max gap to read through",
                kind="int",
                target="option",
                default=0,
                min_value=0,
                max_value=125,
                advanced=True,
                help=(
                    "Registers read and thrown away to join nearby tags into one "
                    "request. Raise it only if the device allows reading unused "
                    "registers."
                ),
            ),
        ),
    ),
    ProtocolForm(
        protocol="s7",
        label="S7",
        summary="Host, rack and slot",
        endpoint_scheme="tcp",
        fields=(
            FormField(
                name="host",
                label="Host",
                kind="text",
                target="endpoint_host",
                required=True,
                help="Host name or IP address, for example press3-plc",
            ),
            FormField(
                name="port",
                label="Port",
                kind="int",
                target="endpoint_port",
                required=True,
                default=102,
                min_value=1,
                max_value=65535,
            ),
            FormField(
                name="rack",
                label="Rack",
                kind="int",
                target="option",
                default=0,
                min_value=0,
                max_value=7,
            ),
            FormField(
                name="slot",
                label="Slot",
                kind="int",
                target="option",
                default=1,
                min_value=0,
                max_value=31,
                help="S7-1200/1500: 1. S7-300: usually 2.",
            ),
            FormField(
                name="max_gap",
                label="Max gap to read through (bytes)",
                kind="int",
                target="option",
                default=16,
                min_value=0,
                max_value=256,
                advanced=True,
                help=(
                    "Bytes read and thrown away to join nearby tags into one request."
                ),
            ),
        ),
    ),
    ProtocolForm(
        protocol="mbus",
        label="M-Bus",
        summary="Serial port or TCP gateway, meter address",
        fields=(),
        available=False,
        available_in="0.6.0",
    ),
)

_FORMS_BY_PROTOCOL = {form.protocol: form for form in PROTOCOL_FORMS}


def form_for(protocol: str) -> ProtocolForm:
    """Return the form of ``protocol``.

    A protocol without a spec (``mock``, or one stored by an import) gets a
    generic form with only the endpoint, marked unavailable, so its existing
    Connections can still be edited but no new ones are offered.
    """
    normalized = protocol.strip().lower()
    known = _FORMS_BY_PROTOCOL.get(normalized)
    if known is not None:
        return known
    return ProtocolForm(
        protocol=normalized,
        label=normalized,
        summary="",
        fields=(
            FormField(
                name="endpoint",
                label="Endpoint",
                kind="text",
                target="endpoint",
                required=True,
            ),
        ),
        available=False,
    )


def picker_forms(registered: tuple[str, ...]) -> tuple[ProtocolForm, ...]:
    """Return the forms the "Add connection" picker lists.

    A protocol is offered only when it has a spec and a registered driver;
    future protocols stay listed but unavailable. Test-only protocols such
    as ``mock`` have no spec, so they are never listed.
    """
    return tuple(
        form
        for form in PROTOCOL_FORMS
        if not form.available or form.protocol in registered
    )


def validate_new_id(value: str) -> str | None:
    """Return the error for a new ID, or None when it is valid."""
    return None if ID_PATTERN.fullmatch(value) else ID_ERROR


def display_endpoint(endpoint: str) -> str:
    """Return the endpoint as the Panel shows it, with credentials masked."""
    try:
        return _mask_endpoint(endpoint)
    except ValueError:
        # Unparseable; hide all of it if it might carry credentials.
        return "***" if "@" in endpoint else endpoint


def connection_from_form(
    form: ProtocolForm,
    data: Mapping[str, str],
    *,
    existing: ConnectionConfig | None,
) -> ConnectionConfig:
    """Build a Connection from submitted form ``data``.

    For a new Connection (``existing`` is None) the ID comes from ``data`` and
    must match ``ID_PATTERN``; otherwise the existing ID is kept. Options the
    form does not show are kept from ``existing``. A blank secret field keeps
    the saved secret, and a checked ``remove_<name>`` clears it. An endpoint
    submitted exactly as the Panel showed it (credentials masked) keeps the
    saved endpoint. Raises ``FormErrors`` keyed by field name.
    """
    errors: dict[str, str] = {}

    if existing is None:
        connection_id = data.get("id", "").strip()
        id_error = validate_new_id(connection_id)
        if id_error is not None:
            errors["id"] = id_error
    else:
        connection_id = existing.id

    timeout_ms = _parse_timeout(data.get("timeout_ms", ""), errors)

    same_protocol = existing is not None and existing.protocol == form.protocol
    options: dict[str, object] = (
        dict(existing.protocol_options)
        if existing is not None and same_protocol
        else {}
    )
    endpoint = ""
    host = ""
    port: int | None = None
    for field in form.fields:
        raw = data.get(field.name, "")
        if field.target == "endpoint":
            endpoint = _parse_endpoint(field, raw, existing, errors)
        elif field.target == "endpoint_host":
            host = raw.strip()
            if host.startswith("[") and host.endswith("]"):
                host = host[1:-1]
            if not host:
                errors[field.name] = f"Enter the {field.label.lower()}."
        elif field.target == "endpoint_port":
            port = _parse_int(field, raw, errors)
        elif field.target == "secret":
            _apply_secret(field, raw, data, options, errors)
        else:
            _apply_option(field, raw, data, options, errors)

    if form.endpoint_scheme is not None:
        host_field = next(f for f in form.fields if f.target == "endpoint_host")
        if host and host_field.name not in errors:
            # Check the host alone, with a placeholder port, so a bad host and a
            # bad port are both reported in one submit.
            try:
                parse_endpoint(format_endpoint(host, 502))
            except ConfigurationError:
                errors[host_field.name] = "Enter a host name or IP address."
        if not errors:
            assert port is not None  # A missing or bad port left an error.
            endpoint = format_endpoint(host, port)

    if errors:
        raise FormErrors(errors)

    try:
        return ConnectionConfig(
            id=connection_id,
            name=data.get("name", ""),
            protocol=form.protocol,
            endpoint=endpoint,
            enabled=bool(data.get("enabled")),
            timeout_ms=timeout_ms,
            protocol_options=options,
        )
    except ConfigurationError as error:
        raise FormErrors({"form": error.message}) from None


def connection_form_values(
    form: ProtocolForm, connection: ConnectionConfig
) -> dict[str, object]:
    """Return the field values the edit form shows for ``connection``."""
    values: dict[str, object] = {}
    host, port = "", None
    if form.endpoint_scheme is not None:
        with contextlib.suppress(ConfigurationError):
            host, port = parse_endpoint(connection.endpoint)
    for field in form.fields:
        if field.target == "endpoint":
            values[field.name] = display_endpoint(connection.endpoint)
        elif field.target == "endpoint_host":
            values[field.name] = host
        elif field.target == "endpoint_port":
            if port is not None:
                values[field.name] = str(port)
        elif field.target == "option":
            option = connection.protocol_options.get(field.name, field.default)
            if field.kind == "bool":
                values[field.name] = option is True
            elif option is not None:
                values[field.name] = str(option)
    return values


def _parse_int(field: FormField, raw: str, errors: dict[str, str]) -> int | None:
    """Parse an int field and check its range; record the error and return None."""
    value = raw.strip()
    if not value:
        if field.required:
            errors[field.name] = f"Enter the {field.label.lower()}."
        return None
    number = _parse_digits(value)
    low, high = field.min_value, field.max_value
    if number is not None and (
        (low is not None and number < low) or (high is not None and number > high)
    ):
        number = None
    if number is None:
        if low is not None and high is not None:
            errors[field.name] = f"Enter a whole number from {low} to {high}."
        else:
            errors[field.name] = INT_ERROR
    return number


def _parse_digits(raw: str) -> int | None:
    """Return the value of plain ASCII digits, or None for anything else.

    ``int()`` alone would also take ``1_000``, signs, spaces inside and
    non-ASCII digits.
    """
    text = raw.strip()
    if not text or not (text.isascii() and text.isdigit()):
        return None
    return int(text)


def _parse_timeout(raw: str, errors: dict[str, str]) -> int:
    if not raw.strip():
        return DEFAULT_TIMEOUT_MS
    value = _parse_digits(raw)
    if value is None or not 1 <= value <= MAX_TIMEOUT_MS:
        errors["timeout_ms"] = TIMEOUT_ERROR
        return DEFAULT_TIMEOUT_MS
    return value


def _parse_endpoint(
    field: FormField,
    raw: str,
    existing: ConnectionConfig | None,
    errors: dict[str, str],
) -> str:
    value = raw.strip()
    if existing is not None and value and value == display_endpoint(existing.endpoint):
        # Unchanged, as shown with its credentials masked.
        return existing.endpoint
    if not value:
        errors[field.name] = f"Enter the {field.label[0].lower()}{field.label[1:]}."
        return value
    if field.kind == "url":
        scheme = ""
        try:
            parts = urlsplit(value)
            scheme = parts.scheme.lower()
            valid = bool(scheme and parts.hostname)
            port = parts.port  # Raises for a port that is not a number.
        except ValueError:
            valid, port = False, None
        if field.schemes and (scheme not in field.schemes or port is None):
            valid = False
        if not valid:
            if field.schemes:
                wanted = " or ".join(f"{s}://" for s in field.schemes)
                article = "an" if wanted[0] in "aeiou" else "a"
                lead = f"Enter {article} {wanted} URL with a host and port."
            else:
                lead = "Enter a full URL with a scheme and host."
            errors[field.name] = f"{lead} {field.help}." if field.help else lead
        elif "@" in parts.netloc:
            errors[field.name] = ENDPOINT_CREDENTIALS_ERROR
    return value


def _apply_secret(
    field: FormField,
    raw: str,
    data: Mapping[str, str],
    options: dict[str, object],
    errors: dict[str, str],
) -> None:
    if data.get(f"remove_{field.name}"):
        if raw != "":
            errors[field.name] = SECRET_CONFLICT_ERROR.format(label=field.label.lower())
            return
        options.pop(field.name, None)
    elif raw != "":
        options[field.name] = raw


def _apply_option(
    field: FormField,
    raw: str,
    data: Mapping[str, str],
    options: dict[str, object],
    errors: dict[str, str],
) -> None:
    if field.kind == "bool":
        options[field.name] = bool(data.get(field.name))
        return
    value = raw.strip()
    if not value:
        if field.required:
            errors[field.name] = f"Enter the {field.label.lower()}."
        options.pop(field.name, None)
        return
    if field.kind == "int":
        number = _parse_int(field, value, errors)
        if number is not None:
            options[field.name] = number
        return
    if field.kind == "select" and value not in {key for key, _ in field.choices}:
        errors[field.name] = "Choose one of the listed options."
        return
    options[field.name] = value
