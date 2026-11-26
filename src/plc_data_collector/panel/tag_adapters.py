"""Protocol-aware Tag forms: one adapter per protocol, chosen by Connection.

An adapter turns submitted form fields (or one pasted CSV row, keyed by the
same names) into a ``TagConfig``, and a saved Tag back into form values and
Tags table cells. ``columns`` are the CSV columns; for the Modbus and generic
forms they are also the form field names, while the S7 form takes its address
in separate fields and a pasted row as one ``address``.
"""

from __future__ import annotations

from collections.abc import Collection, Hashable, Mapping
from typing import Protocol

from plc_data_collector.domain import ConnectionConfig, TagConfig
from plc_data_collector.panel.modbus_tag_form import ModbusTagForm
from plc_data_collector.panel.s7_tag_form import S7TagForm
from plc_data_collector.panel.tag_forms import GenericTagForm
from plc_data_collector.protocols.modbus_tags import MODBUS_PROTOCOL
from plc_data_collector.protocols.s7_tags import S7_PROTOCOL


class TagFormAdapter(Protocol):
    """The Tag form of one protocol."""

    # The template with the protocol's fields, included by ``_tag_form.html``.
    template: str
    # The CSV columns (for Modbus and generic forms, also the form fields).
    columns: tuple[str, ...]
    required_columns: tuple[str, ...]
    # Whether the Tags table shows a Read from column.
    read_from_column: bool
    # What two Tags with the same ``register_key`` share, in duplicate errors:
    # "Same register as line 3." for Modbus, "Same address as ..." otherwise.
    duplicate_noun: str

    def defaults(
        self, connection: ConnectionConfig, group_choices: list[tuple[str, str]]
    ) -> dict[str, object]:
        """Return the values a new Tag form starts with."""
        ...

    def values(self, tag: TagConfig, connection: ConnectionConfig) -> dict[str, object]:
        """Return the form values of a saved Tag."""
        ...

    def values_from_data(self, data: Mapping[str, str]) -> dict[str, object]:
        """Return submitted values to show again."""
        ...

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
        """Build a Tag from submitted ``data``; raise ``FormErrors`` by field.

        ``data`` needs only the keys in ``columns``. ``enabled`` None means
        enabled; the form passes whether its checkbox was ticked.

        With ``tag_id`` None, ``build`` runs only the field checks and always
        raises ``FormErrors`` (empty when the fields are fine): the domain
        model, and for Modbus ``resolve_tag``, are not checked. Callers that
        want full validation before choosing an ID pass a placeholder ID.
        """
        ...

    def next_values(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> dict[str, object] | None:
        """Return the next new-Tag form after creating ``tag``; None closes it."""
        ...

    def table_cells(
        self, tag: TagConfig, connection: ConnectionConfig
    ) -> tuple[str, str, str]:
        """Return the Read from, Address and Type cells of a Tags table row."""
        ...

    def register_key(self, tag: TagConfig, connection: ConnectionConfig) -> Hashable:
        """Return what makes two Tags read the same place (pasted duplicates,
        also against the Connection's existing Tags)."""
        ...

    def template_context(
        self, values: Mapping[str, object], connection: ConnectionConfig
    ) -> dict[str, object]:
        """Return what the form template needs besides the values."""
        ...


def tag_adapter_for(protocol: str) -> TagFormAdapter:
    """Return the Tag form of ``protocol``; the generic one if it has none."""
    if protocol == MODBUS_PROTOCOL:
        return ModbusTagForm()
    if protocol == S7_PROTOCOL:
        return S7TagForm()
    return GenericTagForm()
