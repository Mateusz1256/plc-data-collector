# M-Bus

The M-Bus driver reads heat, water, gas and electricity meters on a wired
M-Bus (EN 13757-2 and EN 13757-3), through a TCP gateway or a serial level
converter. It never writes to a meter. A Tag selects one record of one meter
by what the record means (energy at storage 1, forward energy at tariff 2),
never by its position in the telegram, and stores the value in a unit the Tag
declares. The Panel can scan the bus for meters and create Tags from one
reading of a meter.

> **Not tested on real hardware.** 0.6.0 M-Bus support has been verified only
> against the bundled simulator, byte-level telegram fixtures, a serial
> pseudo-terminal and pyMeterBus as an independent decoder in the tests. It
> has not been run against a real gateway, level converter or meter. Before
> you bill from it, compare a total digit by digit with the meter's display
> and work through the [hardware validation checklist](#hardware-validation-checklist).
> Please report any meter that behaves differently.

Not supported: wireless M-Bus, encrypted telegrams and OMS; writing to meters
(setting primary addresses, baud rates, the clock, resets); changing a
meter's or a gateway's baud rate; manufacturer-specific records; date and
time records (types F, G, I and J), plain-text and variable-length records as
Tags; compact profiles and "selection for readout"; more than one collector
process on the same bus.

## Connection

The endpoint names the transport:

| Transport | Endpoint | Example |
| --- | --- | --- |
| TCP gateway | `tcp://<host>:<port>` | `tcp://192.0.2.30:10001` |
| Serial port | `serial://<device>` | `serial:///dev/ttyUSB0`, `serial://COM3` |

A TCP gateway is a transparent TCP-to-M-Bus converter: the driver sends M-Bus
frames over the socket and the gateway puts them on the bus. A serial port is
a USB or RS-232 M-Bus level converter. The Panel shows **Transport** (TCP
gateway or Serial port) and then Host and Port (pre-filled with 10001, common
for transparent gateways), or Device. The endpoint parser has no default port.
The device is not checked for existence when you save.

`timeout_ms` is the reply timeout for one request, and the timeout for
opening a TCP connection. The Panel pre-fills 3000: a long telegram at 2400
baud takes about 1.2 seconds.

`protocol_options` of a Connection (all optional, unknown options refused):

| Option | Panel label | Values | Default | Notes |
| --- | --- | --- | --- | --- |
| `baud_rate` | Baud rate | 300, 600, 1200, 2400, 4800, 9600, 19200, 38400 | 2400 | Serial port only. Refused on `tcp://`: the gateway sets its own baud rate |
| `scan_timeout_ms` | Scan reply timeout | 50 to 5000 | 500 | Advanced. How long a scan waits for each probe |
| `max_telegrams` | Telegrams per meter | 1 to 16 | 8 | Advanced. The most telegrams read from a meter that sends its data in parts |

Serial framing is fixed at 8 data bits, even parity and 1 stop bit
(EN 13757-2). The serial transport uses `pyserial`, so it works on Linux,
macOS and Windows.

**One Connection per bus.** A serial port cannot be opened twice, and many
gateways accept one client. Two active M-Bus Connections with the same
endpoint (the same host and port, or the same device) are refused on save, on
import and in the seed file, with `invalid_mbus_connection`. Put every meter
of a bus on one Connection and use Tag Groups to poll them at different
intervals.

"Test connection" opens the bus (or borrows the running one, see
[Sharing the bus with the Panel](#sharing-the-bus-with-the-panel)) and
reports success when the transport is open, for example
`Connected in 12 ms (TCP gateway).` or `(Serial 2400 baud)`. With a probe Tag
it also reads that Tag's meter, the way a poll reads it. It does not scan.
The whole test ends after at most 60 seconds.

## Meter address

A Tag's `address` names the meter. It is stored in one of three forms, whose
lengths never overlap:

| Form | Example | Meaning |
| --- | --- | --- |
| 1 to 3 digits, 0 to 250 | `5` | Primary address |
| 8 digits | `12345678` | Secondary address: the identification number only; manufacturer, version and medium are wildcards |
| 16 hex digits | `123456782C2D0104` | Full secondary address: ID (8 BCD digits), manufacturer (4 hex), version (2) and medium (2), as libmbus writes it |

`F` is a wildcard: in an ID digit, or as `FFFF` and `FF` for the
manufacturer, version and medium. A full address whose manufacturer, version
and medium are all wildcards is stored as its 8-digit ID. Input is trimmed and
upper-cased. Prefixes such as `P5` or `S12345678` are not accepted. An
invalid address is refused with `mbus_invalid_address`.

The Panel shows a primary address as `5` and a secondary one as `12345678`,
or with the manufacturer code and medium when the address gives them, for
example `12345678 PDC heat`.

**Primary or secondary?** A primary address (1 to 250) is short and fast, but
it is set in the meter and often left at 0 by the factory; two meters with the
same primary address answer at once and garble each other. A secondary
address is the meter's identification number, unique per manufacturer, so it
is the safer choice on a bus with many meters. Reading by secondary address
takes one extra exchange (select, then deselect).

## Tag options

`protocol_options` of a Tag. Unknown keys are refused with
`mbus_invalid_options`.

| Key | Values | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `quantity` | A name from the [quantity table](#quantities-and-units) | yes | | What the record measures |
| `unit` | A unit of the quantity | yes, except `hca_units` | | The unit the value is stored in |
| `storage` | 0 to 2^40 | no | 0 | 0 is the current value; 1 and up are stored values, such as the value on the billing date |
| `tariff` | 0 to 2^20 | no | 0 | |
| `subunit` | 0 to 2^10 | no | 0 | The DIFE device or subunit number |
| `function` | `instantaneous`, `maximum`, `minimum`, `error` | no | `instantaneous` | The DIF function field |
| `qualifiers` | A list of `backward`, `forward`, `per_hour`, `per_minute`, `per_second` | no | `[]` | From recognised VIF extensions; stored sorted, each at most once |
| `occurrence` | 1 to 32 | no | 1 | The n-th record that matches everything above, for meters that repeat a record |
| `meter_id` | 8 digits | no | none | The meter's identification number, checked on every read (see [Meter identity](#meter-identity)) |

The Tag's `value_type` follows from the quantity: `integer` for `on_time` and
`operating_time`, `numeric` for every other quantity. A Tag whose
`value_type` does not match is refused on save and on import.

There is no scale or offset for M-Bus: the unit option already converts
exactly, and a free factor would undermine billing values.

## Record key

A meter's reply is a list of data records. A Tag finds its record by the
**record key**: `quantity`, `storage`, `tariff`, `subunit`, `function`,
`qualifiers` and `occurrence`. The record's position in the telegram plays
no part, so a meter that sends its records in another order, or adds a
record, still gives every Tag the right value. When the key is not in the
reply, the Tag is bad `mbus_record_not_found`: it never takes a neighbouring
record instead.

`occurrence` is for meters that send two records with the same key. Read
meter sets it automatically, counting in telegram order.

Two Tags with the same meter and record key read the same value. The CSV
paste flags such a row ("Same record as ..."), and Read meter marks a record
an existing Tag already reads. The meter is compared as written: a Tag at primary `1` and
one at secondary `12345678` are different addresses, even when they are the
same meter.

## Quantities and units

Each quantity comes from the record's VIF (EN 13757-3, the primary table and
the listed `0xFB` and `0xFD` extensions). Units in one family convert into
each other exactly, in decimal (powers of ten, and ×60 or ×3600 between flow
per second, minute and hour).

| `quantity` | VIF | `unit` values | Value type |
| --- | --- | --- | --- |
| `energy` | `E000 0nnn` Wh; `0xFB` `0000 000n` MWh | `Wh`, `kWh`, `MWh`, `GWh` | numeric |
| `energy` | `E000 1nnn` J; `0xFB` `0000 100n` GJ | `J`, `kJ`, `MJ`, `GJ` | numeric |
| `volume` | `E001 0nnn` | `m3`, `L` | numeric |
| `mass` | `E001 1nnn` | `kg`, `t` | numeric |
| `on_time` | `E010 00nn` | `s` (integer seconds) | integer |
| `operating_time` | `E010 01nn` | `s` (integer seconds) | integer |
| `power` | `E010 1nnn` W | `W`, `kW`, `MW` | numeric |
| `power` | `E011 0nnn` J/h | `J/h`, `kJ/h`, `MJ/h`, `GJ/h` | numeric |
| `volume_flow` | `E011 1nnn`, `E100 0nnn`, `E100 1nnn` | `m3/h`, `L/h`, `m3/min`, `m3/s` | numeric |
| `mass_flow` | `E101 0nnn` | `kg/h`, `t/h` | numeric |
| `flow_temperature` | `E101 10nn` | `C` | numeric |
| `return_temperature` | `E101 11nn` | `C` | numeric |
| `temperature_difference` | `E110 00nn` | `K` | numeric |
| `external_temperature` | `E110 01nn` | `C` | numeric |
| `pressure` | `E110 10nn` | `bar` | numeric |
| `hca_units` | `E110 1110` | none (heat cost allocator units carry no unit; omit `unit`) | numeric |
| `voltage` | `0xFD` `0100 nnnn` | `V`, `kV` | numeric |
| `current` | `0xFD` `0101 nnnn` | `A` | numeric |

**Families do not mix.** Wh and J are separate families, and so are W and
J/h. A Tag stored in `kWh` never takes a value the meter sends in `GJ`: the
reading is bad `mbus_unit_mismatch`. A meter replaced by one that counts in
another family is noticed, not converted with a factor nobody checked.

**Durations** (`on_time`, `operating_time`) are stored as integer seconds.
Meters send seconds, minutes, hours or days, and the driver converts them to
seconds. A duration with a fraction of a second is unsupported.

**The meter's natural unit.** The codec turns the VIF's power of ten into the
largest unit of the family that does not exceed it, so values read as on the
display: an energy record with VIF `0xFB 01` (MWh) is in whole MWh, VIF
`0x13` (litres) is in `L`, and VIF `0x14` (tens of litres) is in `L` as well. Read meter shows this unit
and proposes it for the Tag. Any unit of the same family can be chosen
instead.

## Billing exactness

Billing values must be the number on the meter's display. The driver
therefore never scales with floats:

- An integer or BCD value is multiplied by its power of ten in decimal, then
  converted to the Tag's unit in decimal. 12345678 litres in BCD with VIF
  `0x13` stored in `m3` is exactly `12345.678`.
- The decimal is converted to the stored float64 once, at the end
  (`numeric_value` is a float64 column). Every decimal with up to 15
  significant digits survives that round trip, which is more than any
  8-digit BCD or 12-digit display, so the stored value prints as the display
  does.
- A 32-bit real is stored as the shortest decimal that gives back the same
  float32. A meter that sends 2345.6 as a real stores `2345.6`, not
  `2345.60009765625`.
- Live tags shows an M-Bus value with every digit it has (`12 345.678`), not
  rounded to 2 decimals as for the other protocols, so you can compare it
  with the display there.

Two VIF extensions change a value exactly: the multiplicative correction
factors 10^(nnn-6) (`E111 0nnn`) and 10^3 (`E111 1101`). The additive
corrections (`E111 10nn`, codes `0x78` to `0x7B`), `0x7E` and every VIF
extension the driver does not know make the record unsupported: the value is
not guessed.

## Meter identity

A primary address says nothing about which meter answers. A meter swapped for
another, or a second meter set to the same address, would answer with the
same kind of records, and billing Tags would go on reading a different
counter as good. A Tag can therefore carry `meter_id`, the meter's 8-digit
identification number. When it is set, a reply whose header ID differs makes
the Tag bad `mbus_wrong_meter`, with both IDs in the message.

Read meter fills `meter_id` for every Tag it creates when the meter's ID is 8
decimal digits, whatever addressing is used. The Tag form and the CSV paste accept it as an optional field. It is not
part of the record key.

For a secondary address the check is built in: a reply whose ID,
manufacturer, version or medium differs from what the address gives makes the
meter's Tags bad `mbus_wrong_meter`.

## What the driver decodes

- Frames: single character `E5`, short frame and long frame, checked for
  length, checksum and stop byte. A reply that fails a check is garbled, which
  is different from no reply.
- Requests: SND_NKE (reset), REQ_UD2 (class 2 data) with the frame count bit,
  and the secondary-address selection (SND_UD, CI `0x52`). The selection is
  not a write: it only picks the meter that answers the next request.
- Replies: variable-data RSP_UD with CI `0x72`, and CI `0x76`, which has the
  same layout with multi-byte header and data fields most significant byte
  first (EN 13757-3 mode 2). Fill bytes `0x2F` are skipped. DIF `0x1F`
  means "more records follow": the driver requests the next telegram, up to
  `max_telegrams`.
- Data fields: 8, 16, 24, 32, 48 and 64-bit signed integers, 32-bit real,
  and 2, 4, 6, 8 and 12-digit BCD. A high nibble `F` in the most significant
  BCD digit is a minus sign. "No data", variable length and "selection for
  readout" are unsupported.
- Storage number, tariff and subunit over the DIF and up to ten DIFEs.
- Qualifiers from VIF extensions: `forward` (`0x3B`, positive contributions
  only), `backward` (`0x3C`, negative contributions only), and `per_second`,
  `per_minute` and `per_hour` (`0x20` to `0x22`). A per-time extension on a
  quantity that already holds a time (`power`, `volume_flow`, `mass_flow`)
  would be a rate of change, not the quantity, so that record is
  unsupported.

A record the driver cannot decode still appears in Read meter, greyed, with
the reason, for example "date/time records are not supported" or
"VIFE 0x7E is not supported". A Tag pointed at such a record is bad
`mbus_record_unsupported`.

## Meter status and uncertain readings

The status byte of the reply header is checked on every read. When it says
"application busy", "application error", "permanent error" or "temporary
error", the values of that meter are stored as `uncertain`, with the code
`mbus_meter_status` and the decoded bits in the message, for example
"Meter 1 reports: permanent error." The value is still stored.

"Power low" alone keeps the readings `good`. It is logged once per meter at
WARNING (`mbus_meter_power_low`), and again only after a reading without it.

Uncertain readings with a reason are new in 0.6.0: `error_code` and
`error_message` may now come with a value. See
[docs/database-schema.md](../database-schema.md) for the stored columns and
the README for the REST API. Only M-Bus produces such readings in 0.6.0.

## Polling and the time budget

Each poll groups the Tags of the Tag Group by meter and reads each meter once,
in address order (primary addresses by number, then secondary addresses). A
meter read is one turn on the bus: a reset or a selection, REQ_UD2 and its
follow-up telegrams, and a deselection after a secondary read. Every Tag of
the meter then takes its record from that one reading.

- A meter that does not answer, after one retry, makes only its own Tags bad
  `mbus_no_reply`. A garbled reply, after one retry, makes them bad
  `mbus_garbled_reply`. The other meters' Tags are read as usual, and the bus
  is not reconnected.
- A missing or unsupported record makes only that Tag bad.
- A transport failure (socket closed, serial port gone) fails the whole poll
  with `mbus_connection_lost`, `mbus_timeout` or `mbus_not_connected`, and
  the worker reconnects as for the other protocols.

M-Bus is slow. At 2400 baud one meter read takes about 0.3 to 2.5 seconds,
more for meters with several telegrams. The rule of thumb:

> **meters × 2.5 s should stay below the Tag Group interval.**

Twenty meters in one Tag Group need an interval of at least 50 seconds. A
cycle that takes longer than the interval is handled by the scheduler's
overlap policy (the next cycle waits, it never runs on top), but values then
arrive less often than configured. Two Tag Groups on the same Connection take
turns on the bus.

M-Bus has no source timestamp; the time of the reply is used. The meter's own
clock is not read.

## Battery-powered meters

Many heat and water meters run on a battery, and every read costs energy. The
Tag Group form, the Tags page and the Read meter save show a warning, not a
refusal, when an M-Bus Tag Group with Tags polls more often than once a
minute:

> This group polls M-Bus meters every 10 s. Battery-powered meters may drain
> or stop answering when read more often than once a minute.

Billing totals rarely need more than one read every 15 minutes. Put fast
monitoring values (power, flow, temperatures) of mains-powered meters in their
own Tag Group.

## Scan bus

"Scan bus" on an M-Bus Connection's page finds the meters on the bus. Choose
one of:

- **Primary scan**: sends SND_NKE to every address from 0 to 250 and waits
  "Scan reply timeout" for each. A meter that answers is read for its header
  (ID, manufacturer, version, medium). A garbled answer means two or more
  meters share that address: "Two or more meters share primary address N;
  use secondary addresses."
- **Secondary scan**: a wildcard search. It selects `0FFFFFFF` to
  `9FFFFFFF`; no answer skips that digit, a clean answer reads the selected
  meter's header, and a garbled answer (two or more meters) narrows down with
  the next digit fixed, down to the full ID. It finds meters with any primary
  address, including 0. Two meters with the same ID are reported: give their
  Tags the full 16-digit secondary address.

The scan runs in the background; the page shows the probes done, the meters
found and the address being probed, and updates every second. "Cancel scan"
stops it after the current probe. At 2400 baud with a 500 ms scan reply
timeout a primary scan takes about two to three minutes; a secondary scan
takes seconds to a few minutes, depending on the meters.

The results list each meter's primary address (for a primary scan), ID,
manufacturer, version and medium, mark meters that a Tag of the Connection
already reads ("Has tags"), and have a "Read meter" button.

A scan takes **one turn per probe**, so polling goes on between probes:
polls are slower during a scan, but there are no gaps. One scan runs per
Connection at a time. Scan results are kept in memory only, until the next
scan or a restart. Apply changes stops every scan ("Scan stopped: the
configuration changed."), and so does stopping the collector.

## Read meter

"Read meter" (on the Connection's page, in a scan's results, and as a link on
the Tag form) reads one meter once and lists its records:

1. Enter the meter address (pre-filled from the scan or the Tag form) and
   choose Read.
2. The page shows the meter's header: ID, manufacturer, version, medium and
   the status byte decoded (`OK`, or the problems and "power low").
3. Each record is a row with its quantity, value, unit, storage, tariff,
   subunit, function and qualifiers. Unsupported records are greyed with the
   reason. Records an existing Tag already reads are marked "Already read by
   tag ..." and unticked.
4. The Unit column is a choice limited to the record's family, set to the
   meter's own unit.
5. Names are made from a **Name prefix** (`m<primary>` for a meter read by
   primary address, else `m<ID>`) and the parts of the record key that are not
   the default: `<prefix>_<quantity>`, then `_s<storage>`, `_t<tariff>`,
   `_u<subunit>`, `_<function>` and `_<qualifiers>` where they are set, for
   example `m12345678_energy_s1`. Change the prefix and choose "Update
   names". Names are made unique with `_2`, `_3` and so on.
6. Choose a Tag Group, tick the records, and choose "Save selected tags". At
   most 1000 Tags per save, all or nothing, one audit entry per Tag.

Every Tag created this way stores the meter address as it was typed (primary
or secondary) and, when the meter's ID is 8 decimal digits, the meter's ID as
`meter_id`. The reading is kept in memory
on the server for 30 minutes, so ticking, renaming and saving do not touch
the bus again; after that the page says "This reading expired. Read the meter
again."

## Tag form

The Tag form has Meter, Quantity, Unit (the units of the chosen quantity),
Storage and Tariff, and under "Advanced" Subunit, Function, Qualifiers,
Occurrence and Meter ID. The "Read meter" link opens Read meter for the meter
in the Meter field.

## Paste tags (CSV)

"Paste tags" works as for Modbus and S7: a header line, the separator (tab,
semicolon or comma) taken from the header, at most 1000 rows, a preview, and
an all-or-nothing save.

- The columns are `name`, `meter`, `quantity`, `unit`, `storage`, `tariff`,
  `subunit`, `function`, `qualifiers`, `occurrence`, `meter_id`, `tag_group`
  and `enabled`. Only `name`, `meter`, `quantity` and `unit` are required;
  leave `unit` empty for `hca_units`.
- `qualifiers` joins several with `+`, for example `forward` or
  `backward+per_hour`.
- Two rows for the same meter and record key read "Same record as line N."; a
  row for a record an existing Tag reads gives "Same record as Tag <id>."

Example:

```text
name;meter;quantity;unit;storage;tariff;qualifiers;meter_id
Heat energy;1;energy;MWh;;;;12345678
Heat energy last billing date;1;energy;MWh;1;;;12345678
Import energy T1;22223333;energy;kWh;;1;forward;
Export energy T1;22223333;energy;kWh;;1;backward;
```

## Sharing the bus with the Panel

A Connection has **one bus**, shared by polling, Test connection, Read meter
and Scan bus. Everything that talks to the bus takes turns: a turn is one
meter read, one Test connection probe or one scan probe, and leaves the bus
clean (input flushed before, meter deselected after). A Panel action waits at
most for the meter read in progress.

- **The Connection is running.** Panel actions borrow the running bus when
  the endpoint and baud rate are the same as the running ones. When they
  differ (an edited, not yet applied form, for example), the action is
  refused: "This Connection is running with other settings. Apply your
  changes first, or disable the Connection." Testing other settings on a
  running bus would race the worker, and a serial port cannot be opened twice.
  Other option differences (timeouts, telegram limit) do not count; the
  running bus keeps its own until the next apply.
- **The Connection is not running** (disabled, or the collector is stopped):
  the action opens a temporary bus with the form's settings (Test connection)
  or the saved ones (Read meter, Scan bus), and closes it afterwards. Actions
  on the same stopped Connection share the temporary bus.
- **Refused for now**, try again shortly: while changes are being applied
  (`apply_in_progress`), and while the collector is starting or stopping
  (`collector_not_ready`).
- **Port held by another Connection**: when another Connection's worker or
  temporary bus holds the same endpoint, the action is refused (`bus_in_use`):
  "This port is in use by Connection '<id>'. Use that Connection, or disable
  it first."

Apply changes restarts every worker, so it stops every scan first and closes
the temporary buses, so the new workers can open their ports. Scan results
and meter readings are kept in memory only and are lost on restart.

## Errors

A bad meter never makes another meter's Tags bad, and a bad record never makes
the meter's other Tags bad.

| Error code | Meaning |
| --- | --- |
| `mbus_invalid_address` | The meter address is not one of the three forms, or a primary address is above 250 |
| `mbus_invalid_options` | The Tag's options do not validate (unknown key, unit not of the quantity, wrong `value_type`) |
| `invalid_mbus_connection` | The Connection's options do not validate, `baud_rate` on a TCP gateway, or two M-Bus Connections share an endpoint |
| `invalid_mbus_endpoint` | The endpoint is neither `tcp://host:port` nor `serial://<device>` |
| `invalid_serial_endpoint` | A `serial://` endpoint without a usable device name |
| `mbus_no_reply` | The meter did not answer, after one retry |
| `mbus_garbled_reply` | The reply failed the length, checksum or stop-byte check, after one retry; or it came from another primary address. Two meters on one primary address answer like this |
| `mbus_wrong_meter` | The meter's ID differs from the Tag's `meter_id`, or a secondary read was answered by a meter whose ID, manufacturer, version or medium differs from the address |
| `mbus_record_not_found` | The meter sent no record with the Tag's record key (or fewer than `occurrence` of them) |
| `mbus_unit_mismatch` | The record is in another unit family than the Tag's unit (for example GJ for a Tag in kWh) |
| `mbus_record_unsupported` | The record has an unknown VIF extension, an additive correction, a data field the driver does not read, or a duration with a fraction |
| `mbus_value_invalid` | A BCD value has a digit A to F (except a leading F, the minus sign) |
| `mbus_value_not_finite` | A real is NaN or infinite, or the value is out of range |
| `mbus_meter_status` | Not an error: the reading is `uncertain` and the message lists the status bits |
| `mbus_timeout` | The serial port did not take the request in time |
| `mbus_connection_lost` | The socket closed or the serial port went away |
| `mbus_connect_failed` | The gateway or the serial port could not be opened, including a TCP connect that timed out |
| `mbus_not_connected` | A read was attempted while the bus was closed |
| `mbus_bus_settings_differ` | A Panel action was refused: "This Connection is running with other settings. Apply your changes first, or disable the Connection.", or, while another action holds the Connection's temporary bus with other settings, "Another action is using this Connection with other settings. Try again when it has finished." |

`mbus_timeout`, `mbus_connection_lost`, `mbus_connect_failed` and
`mbus_not_connected` fail the whole poll: every Tag in that poll gets a bad
reading with the same code, and the worker reconnects.
`mbus_bus_settings_differ` refuses a Panel action and never reaches a Tag.
The other codes belong to one meter or one Tag.

The Panel refuses a bus action with `apply_in_progress`,
`collector_not_ready`, `bus_in_use` or `mbus_bus_settings_differ`, described
in [Sharing the bus with the Panel](#sharing-the-bus-with-the-panel).

## Hardware validation checklist

The driver was tested against the simulator, telegram fixtures, a serial
pseudo-terminal and pyMeterBus, not yet against real equipment. On a real bus,
check:

- A TCP gateway and a USB level converter each read a meter.
- Primary and secondary addressing, on the same meter.
- A meter that sends its data in several telegrams.
- A primary scan and a secondary scan on a bus with several meters.
- A billing total compared digit by digit with the meter's display.
- A meter's stored billing-date value (`storage` 1).
- A disconnected meter: only its Tags go bad (`mbus_no_reply`).
- A battery-powered meter's behaviour at a 60-second interval.
- A meter that does not reset its FCB on SND_NKE 253: read it by secondary
  address several times in a row and check every read is fresh.
- A gateway or converter that echoes requests.

## Simulator

`python -m plc_data_collector.simulators.mbus` serves five meters on one
simulated bus over TCP, port 10001, like a transparent gateway. The Docker
Compose demo starts it as `mbus-sim`, and the demo configuration has a
Connection `heating_mbus` with a `billing` Tag Group (60 s) and a
`monitoring` Tag Group (10 s, which shows the battery warning; fine for a
simulator). Every meter uses the fictional manufacturer code `PDC`.

| Meter | Primary | ID | Medium | Records |
| --- | --- | --- | --- | --- |
| Heat meter | 1 | 12345678 | heat | Energy in MWh (`0xFB`), energy at storage 1, volume, power, volume flow, flow and return temperature, temperature difference, operating time, and a billing date (unsupported). Two telegrams |
| Water meter | 2 | 12399999 | water | Volume in L (BCD), volume at storage 1, flow. Shares its first digits with meter 1, so the secondary scan meets a collision |
| Electricity meter | 3 | 22223333 | electricity | Energy forward and backward at tariffs 1 and 2, power (real), voltage, current (`0xFD`) |
| Heat meter | 0 | 87654321 | heat | Energy in GJ, volume, flow and return temperature. Primary address 0, so only the secondary scan finds it |
| Warm water meter | 5 | 55667788 | warm water | Volume and flow; status "power low" |

Counters go up over time, billing-date values stay fixed, and temperatures,
power, flow, voltage and current wander. When several selected meters would
answer at once, the simulator sends a garbled frame, as overlapping replies
look on a real bus. The full record tables are in the simulator's module
docstring (`src/plc_data_collector/simulators/mbus.py`).
