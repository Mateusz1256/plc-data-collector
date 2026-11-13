# Database schema

This page describes the tables PLC Data Collector creates in PostgreSQL (or
SQLite, the fallback when no database URL is set). The definition lives in
`src/plc_data_collector/persistence/schema.py`, and Alembic migrations in
`src/plc_data_collector/persistence/migrations/versions/` apply it at startup.
The schema matches release 0.3.0 (migration `0003`).

## What is stable

`tag_readings`, `tags`, `tag_groups` and `connections` are the contract for
consumers: Grafana, reports, ETL jobs or your own SQL. A change that breaks
queries against them is marked **Breaking** in the CHANGELOG. Give consumers a
read-only database role.

`poll_executions`, `runtime_components`, `app_settings`, `api_keys` and
`alembic_version` are internal. They can change in any release; do not build
on them.

## Conventions

- **Timestamps are text.** Every timestamp column is `VARCHAR(32)` holding an
  ISO 8601 string in UTC with a `+00:00` offset; fractional seconds omitted
  when zero, for example `2026-11-13T09:15:02.481233+00:00` or
  `2026-11-13T09:15:03+00:00`. Because every value is UTC in the same format,
  sorting and range comparisons on the text give chronological order.
- **Filter on the text, cast for display.** In `WHERE`, compare
  `received_at` as text against ISO literals with a `+00:00` offset, for
  example `received_at >= '2026-11-13T08:00:00+00:00'`. Casting the column
  (`received_at::timestamptz`) in `WHERE` prevents index use, so a time range
  then scans the whole table. Cast it in the select list
  (`received_at::timestamptz`) to get a timestamp for display or grouping.
- **IDs are chosen by the user.** Connection, Tag Group and Tag IDs are the
  short strings entered in the Panel or the configuration file, for example
  `line1_temperature`, not generated numbers.
- **Removed means archived.** A Connection, Tag Group or Tag removed in the
  Panel, or missing from an imported file, keeps its row with `archived_at`
  set (see [ADR 0002](adr/0002-archive-instead-of-delete.md)). Rows with
  `archived_at IS NULL` are the active configuration.

## `tag_readings`

One row per Tag per poll. Failed reads are stored too, with `quality = 'bad'`
and an error code, so gaps in the data are visible.

| Column | Type | Null | Meaning |
| --- | --- | --- | --- |
| `event_id` | `VARCHAR(64)` | no | Primary key: `<execution_id>:<index>`. Inserts use `ON CONFLICT DO NOTHING`, so a replayed batch adds no duplicates. |
| `connection_id` | `VARCHAR(128)` | no | Foreign key to `connections.id`. |
| `tag_group_id` | `VARCHAR(128)` | no | Foreign key to `tag_groups.id`. |
| `tag_id` | `VARCHAR(128)` | no | Foreign key to `tags.id`. |
| `source_timestamp` | `VARCHAR(32)` | yes | When the controller says the value was produced. Empty when the read failed or the controller sent none. |
| `received_at` | `VARCHAR(32)` | no | When the collector received the value. Use this for time ranges; it is indexed. |
| `quality` | `VARCHAR(32)` | no | `good`, `uncertain` or `bad`. |
| `value_type` | `VARCHAR(32)` | no | Which value column holds the value (table below). For a failed read, the Tag's configured type. |
| `numeric_value` | `DOUBLE PRECISION` | yes | Value of a `numeric` Tag. |
| `integer_value` | `BIGINT` | yes | Value of an `integer` Tag. |
| `boolean_value` | `BOOLEAN` | yes | Value of a `boolean` Tag. |
| `text_value` | `TEXT` | yes | Value of a `text` Tag. |
| `binary_value` | `BYTEA` | yes | Value of a `binary` Tag. |
| `error_code` | `VARCHAR(128)` | yes | Error code of a failed read, for example `connection_unavailable`. |
| `error_message` | `TEXT` | yes | Error description of a failed read. |

### Value-type columns

Exactly one value column is filled when the read succeeded, and none when it
failed (`quality = 'bad'`).

| `value_type` | Column |
| --- | --- |
| `numeric` | `numeric_value` |
| `integer` | `integer_value` |
| `boolean` | `boolean_value` |
| `text` | `text_value` |
| `binary` | `binary_value` |

### Quality values

| `quality` | Meaning |
| --- | --- |
| `good` | The value was read and the controller reported it as good. A value column is filled. |
| `uncertain` | The value was read, but the controller marked it as uncertain. A value column is filled. |
| `bad` | The read failed, or the controller reported the value as bad. No value column is filled; `error_code` and `error_message` say why. |

## `tags`

| Column | Type | Null | Meaning |
| --- | --- | --- | --- |
| `id` | `VARCHAR(128)` | no | Primary key. |
| `tag_group_id` | `VARCHAR(128)` | no | Foreign key to `tag_groups.id`. |
| `name` | `VARCHAR(256)` | no | Display name, for example `Line 1 temperature`. |
| `address` | `VARCHAR(1024)` | no | Protocol address, for example `ns=2;s=Line1.Temperature` for OPC UA. |
| `value_type` | `VARCHAR(32)` | yes | `numeric`, `integer`, `boolean`, `text` or `binary`. Always set by 0.3.0. |
| `enabled` | `BOOLEAN` | no | Disabled Tags are not read. |
| `metadata_json` | `JSON` | no | Extra attributes. `unit` holds the unit shown in the Panel and the API, for example `{"unit": "C"}`. |
| `archived_at` | `VARCHAR(32)` | yes | When the Tag was archived; empty while it is active. |

## `tag_groups`

| Column | Type | Null | Meaning |
| --- | --- | --- | --- |
| `id` | `VARCHAR(128)` | no | Primary key. |
| `connection_id` | `VARCHAR(128)` | no | Foreign key to `connections.id`. |
| `interval_ms` | `INTEGER` | no | Read interval in milliseconds; greater than 0. |
| `timeout_ms` | `INTEGER` | no | Timeout of one read in milliseconds; greater than 0. |
| `overlap_policy` | `VARCHAR(32)` | no | What happens when a read is still running at the next interval. Only `skip` exists. |
| `enabled` | `BOOLEAN` | no | Disabled Tag Groups are not read. |
| `archived_at` | `VARCHAR(32)` | yes | When the Tag Group was archived; empty while it is active. |

## `connections`

| Column | Type | Null | Meaning |
| --- | --- | --- | --- |
| `id` | `VARCHAR(128)` | no | Primary key. |
| `name` | `VARCHAR(256)` | no | Display name. Connections from 0.2.0 got their ID as the name. |
| `protocol` | `VARCHAR(64)` | no | Protocol driver, for example `opcua`. |
| `endpoint` | `VARCHAR(1024)` | no | Address of the controller, for example `opc.tcp://192.0.2.20:4840`. |
| `enabled` | `BOOLEAN` | no | Disabled Connections are not polled. |
| `timeout_ms` | `INTEGER` | no | Connection timeout in milliseconds; greater than 0. |
| `protocol_options` | `JSON` | no | Protocol settings without secrets. |
| `secret_options` | `TEXT` | yes | Secret protocol settings (keys containing `password`, `passwd`, `secret`, `token`, `key` or `credential`), encrypted with Fernet. Not readable without the secret key. |
| `archived_at` | `VARCHAR(32)` | yes | When the Connection was archived; empty while it is active. |

## Internal tables

| Table | Holds |
| --- | --- |
| `poll_executions` | One row per Tag Group read: status, start and finish time, Tag counts and the error message. Primary key `execution_id`. |
| `runtime_components` | Component status rows (state, update time, error). Not written by the 0.3.0 runtime; the current state is served by `/api/runtime/components`. |
| `app_settings` | Key-value settings as JSON: `config_revision` (bumped on every configuration change, drives the pending-changes banner) and `retention` (`{"enabled": ..., "days": ...}`). |
| `api_keys` | API keys for `/api/v1`: name, the first characters of the key, the SHA-256 hash of the key, and when it was created, last used and revoked. The key itself is never stored. |
| `alembic_version` | The applied migration. |

## Indexes and constraints

| Table | Name | Columns | Purpose |
| --- | --- | --- | --- |
| `tag_readings` | primary key, `uq_tag_readings_event_id` | `event_id` | Idempotent inserts. |
| `tag_readings` | `ix_tag_readings_tag_id_received_at` | `tag_id`, `received_at` | History of one Tag over a time range. |
| `tag_readings` | `ix_tag_readings_received_at` | `received_at` | Retention deletes and time-range queries over all Tags. |
| `poll_executions` | `ix_poll_executions_started_at` | `started_at` | Retention deletes. |
| `api_keys` | unique | `key_hash` | Key lookup on every API request. |
| `connections` | `ck_connections_timeout_positive` | `timeout_ms` | `timeout_ms > 0`. |
| `tag_groups` | `ck_tag_groups_interval_positive`, `ck_tag_groups_timeout_positive` | `interval_ms`, `timeout_ms` | Both greater than 0. |
| `poll_executions` | `ck_poll_requested_non_negative`, `ck_poll_success_non_negative`, `ck_poll_failed_non_negative` | Tag counts | Not negative. |

Foreign key columns are not indexed on their own; queries by Tag use the
`(tag_id, received_at)` index.

## Retention

Retention is off by default. When it is turned on in the Panel (Settings, a
number of days from 1 to 3650), the collector deletes rows from `tag_readings`
and `poll_executions` older than that many days, in batches of 10,000 rows:
one minute after startup, then every hour, and right after the setting is
saved. Configuration tables, archived rows included, are never pruned.

## Example queries

The latest value of every active Tag:

```sql
select distinct on (r.tag_id)
       t.name, r.received_at::timestamptz as received_at, r.quality,
       r.numeric_value, r.integer_value, r.boolean_value, r.text_value
from tag_readings r
join tags t on t.id = r.tag_id
where t.archived_at is null
order by r.tag_id, r.received_at desc;
```

Hourly averages of one numeric Tag over the last day:

```sql
select date_trunc('hour', received_at::timestamptz) as hour,
       avg(numeric_value) as average
from tag_readings
where tag_id = 'line1_temperature'
  and quality = 'good'
  and received_at > to_char(
        (now() - interval '1 day') at time zone 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')
group by 1
order by 1;
```
