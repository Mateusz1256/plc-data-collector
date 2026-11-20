# Modbus TCP

The Modbus driver reads Coils, Discrete inputs, Holding registers and Input
registers (function codes 01 to 04) over Modbus TCP. It never writes to the
device. Nothing in it is specific to one device: address numbering, byte and
word order, data types and unit IDs are settings you choose in the Panel.

Not supported: Modbus RTU over serial, RTU framing over TCP, Modbus/TCP
Security (TLS), and function codes other than 01 to 04.

## Connection

The endpoint is `tcp://host:port` (IPv6 hosts in brackets). The Panel shows
Host and Port (default 502) as two fields. `timeout_ms` is the timeout for
opening the connection and the default timeout of a read.

`protocol_options` of a Connection (all optional):

| Option | Panel label | Values | Default |
| --- | --- | --- | --- |
| `unit_id` | Unit ID | 0 to 255 | 1 |
| `byte_order` | Byte order | `ab` big-endian, `ba` little-endian | `ab` |
| `word_order` | Word order | `hi` high word first, `lo` low word first | `hi` |
| `address_numbering` | Address numbering | `zero`, `one`, `modicon` | `zero` |
| `max_registers_per_read` | Max registers per request | 1 to 125 | 125 |
| `max_bits_per_read` | Max coils/inputs per request | 1 to 2000 | 2000 |
| `max_gap` | Max gap to read through | 0 to 125 | 0 |

The last three are under "Advanced" in the Panel. Unknown options are refused.

## Tag address

A Tag's `address` is `<area>:<n>`, or `<area>:<n>.<b>` for the data type `bit`.

| Area | Function code | Holds |
| --- | --- | --- |
| `co` | 01 | Coils |
| `di` | 02 | Discrete inputs |
| `hr` | 03 | Holding registers |
| `ir` | 04 | Input registers |

`n` is always the zero-based protocol address, 0 to 65535, whatever the
Connection's address numbering is. `b` is the bit, 0 to 15, with bit 0 the
least significant. Examples: `hr:120`, `hr:100.3`, `ir:10`, `co:7`.

## Address numbering

Address numbering changes only what the Panel accepts and shows (and what the
CSV paste takes). The stored address stays zero-based, so changing the
numbering never moves a Tag to another register.

| Numbering | Holding register stored as `hr:120` is typed and shown as |
| --- | --- |
| Zero-based | `120` |
| One-based | `121` |
| Modicon | `40121` |

Modicon addresses start with the area digit: 0 for Coils, 1 for Discrete
inputs, 3 for Input registers and 4 for Holding registers. The Panel refuses a
digit that does not match "Read from". Both 5-digit (`40121`) and 6-digit
(`400121`) forms are accepted and mean `hr:120`; the Panel shows 5 digits
while the register number is 9999 or lower and 6 digits above.

## Tag options

`protocol_options` of a Tag. Unknown keys are refused.

| Key | Values | Notes |
| --- | --- | --- |
| `data_type` | `bool`, `bit`, `int16`, `uint16`, `int32`, `uint32`, `int64`, `uint64`, `float32`, `float64`, `string` | Required. `bool` is for `co` and `di` only; all other types are for `hr` and `ir` only. `bit` requires an address with `.b`, and only `bit` may have one |
| `length` | 1 to 125 registers | Required for `string`, refused for other types |
| `byte_order` | `ab`, `ba` | Overrides the Connection |
| `word_order` | `hi`, `lo` | Overrides the Connection |
| `unit_id` | 0 to 255 | Overrides the Connection; several devices behind one gateway share one TCP connection |
| `scale` | number other than 0 | Value is raw times scale plus offset. Not for `bool`, `bit` and `string` |
| `offset` | number | As for `scale` |

A Tag covers 1 register (`int16`, `uint16`, `bit`), 2 (`int32`, `uint32`,
`float32`), 4 (`int64`, `uint64`, `float64`) or `length` registers (`string`).
The address plus this span must not pass 65536.

The Tag's `value_type` follows from the options and the Panel sets it: `bool`
and `bit` give `boolean`, integer types give `integer`, floats give `numeric`,
`string` gives `text`, and a Tag with `scale` or `offset` is `numeric`. A
Tag whose `value_type` does not match is refused on save and on import.

## Byte and word order

Byte order swaps the two bytes inside each register. Word order `hi` keeps the
registers in address order, most significant word first; `lo` reverses the
order of all the words of the value (64-bit values too). Byte order is applied
first.

Worked example: the `float32` value 1.5 is `3F C0 00 00`.

| Registers (address order) | Byte order | Word order | Reads as |
| --- | --- | --- | --- |
| `0x3FC0`, `0x0000` | `ab` | `hi` | 1.5 |
| `0x0000`, `0x3FC0` | `ab` | `lo` | 1.5 |
| `0xC03F`, `0x0000` | `ba` | `hi` | 1.5 |
| `0x0000`, `0xC03F` | `ba` | `lo` | 1.5 |

Bit Tags: bit numbering applies after the byte-order step, so bit N always
agrees with the register's `int16` value. Word order is ignored for `bit` and
`string`. Strings are ASCII, read in register order after the byte-order step,
cut at the first NUL byte and stripped of trailing spaces. A string with
non-ASCII bytes gives a bad reading.

## Block reads

Each poll reads neighbouring Tags in as few requests as possible. Tags are
grouped by unit and area, sorted by address, and merged into one block while
the gap between them is at most `max_gap` registers and the block stays
within `max_registers_per_read` (or `max_bits_per_read` for Coils and Discrete
inputs). Overlapping Tags share a block. Requests are sent one at a time.

`max_gap` 0 merges only contiguous or overlapping Tags, which is safe for
devices that reject reads of undefined registers. Raise it to read through
small holes in a register map with fewer requests; a device that refuses
undefined registers then refuses the whole block, and the driver falls back as
described below.

Each block is read within the smallest Tag Group timeout among its Tags. A Tag
Group timeout longer than the Connection timeout is honoured. Opening the
connection uses the Connection timeout.

Addresses and options are parsed once, and the block plan is reused while the
set of Tags is unchanged.

## Errors

A bad Tag never makes the other Tags of its Tag Group bad. Modbus has no
source timestamp; the time of the block's response is used.

When the device answers a block request with a Modbus exception, the driver
marks the Tags with the code `modbus_exception_NN`, where `NN` is the exception
code in two-digit uppercase hex (`modbus_exception_02`,
`modbus_exception_0A`, `modbus_exception_0B`), or `modbus_exception_unknown`
when the response carries no code. Exceptions 01, 0A and 0B mark every Tag
of the block bad in that one request. For any other exception, for example 02
Illegal data address, the driver reads the block's Tags one by one so that
only the Tags the device refuses go bad, and it remembers this for later polls
of the same block.

| Error code | Meaning |
| --- | --- |
| `modbus_exception_NN`, `modbus_exception_unknown` | The device answered with a Modbus exception |
| `modbus_invalid_tag` | The Tag's address or options do not resolve |
| `modbus_short_response` | The device returned fewer values than requested |
| `modbus_short_read` | Too few registers or bits to decode the Tag |
| `modbus_string_not_ascii` | A `string` Tag contains non-ASCII bytes |
| `modbus_value_not_finite` | A float is NaN or infinite, or scaling gave a non-finite number |
| `modbus_value_out_of_range` | An unscaled integer (a `uint64` above 9223372036854775807) does not fit the 64-bit signed integer column; set `scale` to 1 to store it as a float |
| `modbus_timeout` | A read timed out |
| `modbus_connection_lost` | The connection failed during a read |
| `modbus_connect_failed` | The connection could not be opened |
| `modbus_not_connected` | A read was attempted without a connection |

The last four are not stored per Tag: a timeout or a lost connection fails the
whole poll, and the worker reconnects as it does for OPC UA.

## Paste tags (CSV)

On a Connection's page, "Paste tags" creates many Tags from a register map
copied out of a spreadsheet or a manual.

- The first line is a header. Column names are case-insensitive, and an
  unknown column is an error. Empty trailing header columns are ignored.
- For Modbus the columns are `name`, `read_from`, `address`, `data_type`,
  `length`, `bit`, `byte_order`, `word_order`, `unit_id`, `scale`, `offset`,
  `unit` and `tag_group`. Only `name`, `read_from`, `address` and `data_type`
  are required. `read_from` accepts `co`, `di`, `hr`, `ir`, `FC01` to `FC04`
  and the Panel labels. Rows without a `tag_group` use the group chosen above
  the text box.
- The separator (tab, semicolon or comma) is taken from the header. Quoted
  fields, a byte order mark, CRLF or CR line ends, blank lines and decimal
  commas (with semicolon or tab separators) are handled.
- Addresses are in the Connection's numbering, as in the Tag form.
- At most 1000 rows and 1 MB of text.
- "Preview" shows every row as it will be saved, with errors marked per row,
  including duplicate registers within the paste. Nothing is saved while any
  row has an error. "Add N tags" saves all rows or none.
- Tag IDs are generated from the names, unique across the paste.

Example (semicolon separated, Modicon numbering):

```text
name;read_from;address;data_type;scale;unit
Oil temperature;hr;40103;float32;;degC
Ram position;hr;40107;int16;0,1;mm
Door closed;di;10001;bool;;
```

## Fast entry

After "Save tag" on a Modbus Connection, the Tag form opens again with the same
Tag Group, "Read from", data type and overrides, and the address set to the
next one: the address plus the Tag's span, or for `bit` the next bit, with bit
15 rolling over to bit 0 of the next register.

## Simulator

`python -m plc_data_collector.simulators.modbus` serves a press (unit 1) and an
energy meter (unit 2) on port 5020, with values that change over time. The
Docker Compose demo starts it as `modbus-sim`, and the demo configuration has
a Connection `press2_modbus` for it. Addresses below are zero-based.

Unit 1, "press":

| Address | Type | Value |
| --- | --- | --- |
| `hr:100` | `float32` | Hydraulic pressure, bar (150 plus or minus 20) |
| `hr:102` | `float32` | Oil temperature, degC (45 plus or minus 5) |
| `hr:104` | `uint32` | Cycle counter, +1 every 2 s |
| `hr:106` | `int16` | Ram position times 10; use scale 0.1 (0 to 100 mm) |
| `hr:107.0`, `.1`, `.3` | `bit` | Status word: running, fault, door closed |
| `hr:110` | `float32` | Motor current, A, low word first |
| `hr:120` | `string`, length 8 | Recipe name `RCP-0042` |
| `co:0`, `co:1` | `bool` | Pump on, alarm horn |
| `di:0`, `di:7` | `bool` | Door closed, guard closed |

Unit 2, "meter" (Input registers):

| Address | Type | Value |
| --- | --- | --- |
| `ir:0` | `float32` | Voltage L1, V (230 plus or minus 2) |
| `ir:2` | `float32` | Current L1, A |
| `ir:100` | `uint64` | Energy, Wh, low word first |

Addresses outside the defined ranges answer Modbus exception 02.
