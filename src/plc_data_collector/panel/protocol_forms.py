"""Per-protocol Connection form specs and turning a submitted form into config.

A protocol's form is data: adding a protocol to the Panel means adding a
``ProtocolForm`` here, not a template.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from plc_data_collector.app.config import _mask_endpoint
from plc_data_collector.domain import ConfigurationError, ConnectionConfig

FieldKind = Literal["text", "url", "int", "bool", "password", "select"]
FieldTarget = Literal["endpoint", "option", "secret"]

# IDs of Connections, Tag Groups and Tags created in the Panel.
ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")
ID_ERROR = "Use letters, digits, dot, dash or underscore (up to 128)."
ID_HELP = (
    "Used in the database and API. It can't be changed after the connection is created."
)
DEFAULT_TIMEOUT_MS = 3000
TIMEOUT_ERROR = "Enter a whole number of milliseconds, 1 or more."
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


@dataclass(frozen=True)
class ProtocolForm:
    """The Connection form of one protocol, and how the picker shows it."""

    protocol: str
    label: str
    summary: str
    fields: tuple[FormField, ...]
    available: bool = True
    available_in: str | None = None

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
        fields=(),
        available=False,
        available_in="0.4.0",
    ),
    ProtocolForm(
        protocol="s7",
        label="S7",
        summary="Host, rack and slot",
        fields=(),
        available=False,
        available_in="0.5.0",
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
    for field in form.fields:
        raw = data.get(field.name, "")
        if field.target == "endpoint":
            endpoint = _parse_endpoint(field, raw, existing, errors)
        elif field.target == "secret":
            _apply_secret(field, raw, data, options)
        else:
            _apply_option(field, raw, data, options, errors)

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


def _parse_timeout(raw: str, errors: dict[str, str]) -> int:
    text = raw.strip()
    if not text:
        return DEFAULT_TIMEOUT_MS
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
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
        try:
            parts = urlsplit(value)
            valid = bool(parts.scheme and parts.hostname)
            _ = parts.port  # Raises for a port that is not a number.
        except ValueError:
            valid = False
        if not valid:
            errors[field.name] = (
                f"Enter a full URL with a scheme, host and port. {field.help}."
            )
        elif "@" in parts.netloc:
            errors[field.name] = ENDPOINT_CREDENTIALS_ERROR
    return value


def _apply_secret(
    field: FormField,
    raw: str,
    data: Mapping[str, str],
    options: dict[str, object],
) -> None:
    if data.get(f"remove_{field.name}"):
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
        try:
            options[field.name] = int(value)
        except ValueError:
            errors[field.name] = "Enter a whole number."
        return
    if field.kind == "select" and value not in {key for key, _ in field.choices}:
        errors[field.name] = "Choose one of the listed options."
        return
    options[field.name] = value
