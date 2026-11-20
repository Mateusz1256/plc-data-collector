# Protocol-specific Tag settings are structured options

A Tag has a `protocol_options` JSON column, like the one a Connection already
has. The Tag's `address` says only where to read (`hr:120`); how to read it
(data type, byte and word order, unit ID, scale, offset) is structured data in
`protocol_options`. Each protocol registers its own rules that validate a Tag
against its Connection, and they run on every save path: the Panel, the JSON
import and the driver. We chose this because Modbus needs several settings per
Tag, and S7 and M-Bus will need their own.

## Considered Options

- **Encode the settings in the address string**, for example
  `hr:120:float32:word=lo:scale=0.1`. Rejected because the settings would be
  hidden from SQL and API consumers, who could not filter by data type or unit
  ID, and every new option would mean extending a parser with its own grammar.

## Consequences

- Migration 0004 adds `tags.protocol_options` (JSON, not null, default `{}`).
  Existing Tags, OPC UA Tags included, get `{}`. The downgrade drops the
  column, so Modbus Tags lose their settings.
- Tag objects in the REST API and in the JSON export carry
  `protocol_options`. On import the field is optional, so 0.3.0 exports still
  import unchanged.
- Protocol rules live in a registry beside the driver registry
  (`protocols/rules.py`). A new protocol adds its rules and its option model
  without touching the configuration code. OPC UA's rules accept any non-empty
  address and empty options, as before.
- The Panel's Tag form and the CSV paste take their fields from a per-protocol
  Tag form spec, so a new protocol gets both by describing its fields.
- A change to a Connection (for example switching an OPC UA Connection to
  Modbus) or a move of a Tag Group is refused when it would make existing
  Tags invalid.
