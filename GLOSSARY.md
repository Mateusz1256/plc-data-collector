# PLC Data Collector

Collects values from industrial controllers and meters over several protocols
and stores them in one place for viewing and analysis.

## Language

### Configuration

**Connection**:
One configured link over one protocol to one controller, or one meter bus
(all the M-Bus meters behind one gateway or serial port).
_Avoid_: Data source, device, źródło danych

**Tag Group**:
A set of Tags on one Connection that are read together at one interval.
_Avoid_: Scan class, poll group

**Tag**:
One addressed value inside a controller or meter that the collector reads.
_Avoid_: Variable, point, signal, zmienna

**Tag protocol options**:
Structured settings of one Tag that depend on its protocol, such as a Modbus
data type or byte order. The Tag's address says where to read; the options say
how.
_Avoid_: Address suffix, tag flags

**TIA import**:
The Panel feature that creates S7 Tags from a TIA Portal PLC tag table or from
data block sources, calculating each member's offset. Its preview shows the
offsets so they can be compared with TIA before saving.
_Avoid_: TIA sync, symbol import

**Meter address**:
Which meter on an M-Bus a Tag reads: a primary address (0 to 250, set in the
meter) or a secondary address (the meter's 8-digit identification number,
optionally with manufacturer, version and medium). It is the Tag's address.
_Avoid_: Slave ID, unit ID

**Record key**:
What selects one data record from an M-Bus meter's reply: quantity, storage
number, tariff, subunit, function, qualifiers and occurrence. Records are
selected by this meaning, never by their position in the telegram.
_Avoid_: Record index, register

**Address numbering**:
How a Connection's addresses are typed and shown in the Panel (zero-based,
one-based or Modicon). The stored address is always zero-based.
_Avoid_: Address offset, base

**Archived**:
A Connection, Tag Group or Tag removed from the configuration whose stored
readings are kept.
_Avoid_: Deleted

### Operation

**Panel**:
The password-protected web UI for managing Connections, Tag Groups and Tags
and for previewing current Tag values and runtime status. It has no charts.
_Avoid_: Dashboard, HMI, SCADA

**Apply changes**:
Restarting collection so saved configuration takes effect, without stopping the
Panel or losing queued readings.
_Avoid_: Deploy, reload

**Scan bus**:
The Panel action that searches an M-Bus Connection's bus for meters, by
primary or secondary address, taking turns with polling.
_Avoid_: Discovery, browse

**Read meter**:
The Panel action that reads one M-Bus meter once, lists its records with
value and unit, and creates a Tag for each record ticked.
_Avoid_: Meter import, probe

**API Key**:
A secret that lets one application read collected data through the REST API.
_Avoid_: Token, password

### Data flow

**Block read**:
One protocol request that reads several neighbouring Tags together, instead of
one request per Tag.
_Avoid_: Batch, bulk read

**Poll Result**:
The outcome of reading one Tag Group once: the values of its Tags plus the
status of the read.
_Avoid_: Sample, reading batch

**Uncertain reading reason**:
The error code and message an uncertain reading may carry with its value, to
say why the value is not fully trustworthy (for example an M-Bus meter
reporting an error in its status byte). A good reading never has one.
_Avoid_: Warning, soft error

**Reading Queue**:
The bounded in-memory buffer between polling and storage that holds Poll
Results waiting to be stored.
_Avoid_: Write queue, kolejka zapisów

**Database Writer**:
The component that takes Poll Results from the Reading Queue and stores them in
the database in batches.
_Avoid_: Write queue, kolejka zapisów, saver

**Spool**:
The durable on-disk buffer that keeps Poll Results while the database is
unavailable and replays them once it recovers.
_Avoid_: Write queue, kolejka zapisów, offline cache
