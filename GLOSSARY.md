# PLC Data Collector

Collects values from industrial controllers and meters over several protocols
and stores them in one place for viewing and analysis.

## Language

### Configuration

**Connection**:
One configured link to a single controller or meter over one protocol.
_Avoid_: Data source, device, źródło danych

**Tag Group**:
A set of Tags on one Connection that are read together at one interval.
_Avoid_: Scan class, poll group

**Tag**:
One addressed value inside a controller or meter that the collector reads.
_Avoid_: Variable, point, signal, zmienna

### Operation

**Panel**:
The password-protected web UI for managing Connections, Tag Groups and Tags
and for previewing current Tag values and runtime status. It has no charts.
_Avoid_: Dashboard, HMI, SCADA

### Data flow

**Poll Result**:
The outcome of reading one Tag Group once: the values of its Tags plus the
status of the read.
_Avoid_: Sample, reading batch

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
