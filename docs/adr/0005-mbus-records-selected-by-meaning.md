# M-Bus records are selected by meaning and stored in a declared unit

An M-Bus meter answers a read with a list of data records, each with its own
quantity, storage number, tariff, subunit, function and power of ten. The
list is the meter's choice: firmware updates add or reorder records, a
replacement meter of another make sends the same values in another order or
another unit, and many meters split the list over several telegrams. Meters
are read for billing as well as monitoring, so a stored total must be the
number on the meter's display, and a Tag must never silently start reading a
different value.

A Tag therefore selects its record by **meaning**: the record key
(`quantity`, `storage`, `tariff`, `subunit`, `function`, `qualifiers`, and an
`occurrence` for meters that repeat a key), matched against every record of
the meter's reply, never by position. The Tag also **declares the unit** it
is stored in (`kWh`, `m3`). The driver converts the meter's value into that
unit in decimal, inside one unit family only: Wh and J, and W and J/h, are
separate families, and a record in another family than the Tag's unit is a
bad reading (`mbus_unit_mismatch`), not a conversion. The value is computed
from the raw integer or BCD digits and the VIF's power of ten in decimal, and
converted to the float64 column once, at the end. There is no scale or
offset. The frames are decoded by the project's own codec for the subset of
EN 13757-3 the collector supports.

## Considered Options

- **Select records by index in the reply.** Rejected because the index
  changes with firmware, meter model and telegram split, and nothing would
  report it: the Tag would keep storing good readings of another value. The
  record key survives reordering, and a missing key is a visible bad reading
  (`mbus_record_not_found`).
- **Use pyMeterBus at runtime to decode frames.** Rejected because it scales
  values with floats (a BCD volume of 12345678 litres decodes as
  `12345.677999999999883584…` m3), its exact parts are reachable only through
  private methods, and it pulls in `pyaml`, `pycryptodome` and `simplejson`.
  It is kept as a test-only dependency: a test decodes every fixture and
  simulator frame with both decoders and checks that they agree.
- **Store the meter's own unit and leave conversion to consumers.** Rejected
  because the unit could change under a Tag (a replaced meter that counts in
  GJ instead of MWh) and SQL consumers would sum numbers of different units.

## Consequences

- Every M-Bus Tag names its unit, and the stored number is in that unit
  whatever unit the meter sends within the family. Conversions are exact
  powers of ten (and ×60 or ×3600 for flow per time).
- A meter that changes its records is noticed: a missing key gives
  `mbus_record_not_found`, another family gives `mbus_unit_mismatch`, and an
  unknown VIF extension or an additive correction gives
  `mbus_record_unsupported`. Nothing is mapped to the nearest known thing.
- The record key does not identify the meter. An optional `meter_id`, which
  Read meter always fills, makes a reply from another meter at the same
  primary address a bad reading (`mbus_wrong_meter`).
- Read meter shows every record of a reply, the unsupported ones greyed with
  the reason, so the user picks records by meaning and the Panel fills in the
  key, the unit and `occurrence`.
- The codec owns the VIF and VIFE tables, and a new quantity or unit is a
  table row plus a family entry in `protocols/mbus_tags.py`.
- 0.6.0 M-Bus support has not been tested on real hardware; the guide lists
  what to check, including a billing total compared digit by digit with the
  meter's display.
