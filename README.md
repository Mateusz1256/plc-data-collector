# PLC Data Collector

An asyncio service that polls industrial controllers over OPC UA, stores the Tag readings in PostgreSQL, and has a web Panel and a REST API.

<!--
Demo GIF placeholder. To record it: start the stack (Quick start below), sign in
to the Panel and open the Live page at http://localhost:8080/panel/live. Record
10 to 15 seconds of values updating with ScreenToGif (Windows) or Peek (Linux),
at about 1280 px wide, save it as docs/demo.gif, and replace this comment with:
![Live page of the Panel with values updating](docs/demo.gif)
-->

## Problem

Production data lives in controllers that are awkward to query: values are addressed by node IDs, links drop, and a slow database must never stall the polling. PLC Data Collector reads configured Tags on a fixed interval, keeps one broken Connection from affecting the others, and writes every reading to a relational table that ordinary SQL tools can query. Nothing is lost when the database is briefly unavailable, because Poll Results wait in a durable Spool.

## Features

- Panel (web UI) with admin sign-in: live Tag values with quality and freshness, Connections, Tag Groups and Tags, "Test connection", configuration export and import, API keys and retention.
- Configuration stored in the database. Saved changes take effect with "Apply changes", which restarts collection inside the running process.
- Read-only REST API (`/api/v1`) with current values and history per Tag, authenticated with per-application API keys.
- Documented PostgreSQL schema for SQL consumers such as Grafana.
- Optional retention that deletes readings older than a set number of days.
- Connection secrets (for example OPC UA passwords) encrypted in the database.
- OPC UA driver (`asyncua`) with batched reads, source timestamps and per-Tag quality.
- Mock driver for local development and deterministic tests.
- One isolated worker per Connection, with automatic reconnect.
- Fixed-interval scheduler per Tag Group that never overlaps its own cycles.
- Bounded Reading Queue between polling and storage.
- Database Writer that stores Poll Results in batches, with retry and backoff.
- Durable on-disk Spool that holds Poll Results while PostgreSQL is down and replays them afterwards.
- PostgreSQL storage with Alembic migrations applied at startup (SQLite is the fallback when no database URL is set).
- HTTP endpoints for liveness, readiness, runtime state and build information.
- Built-in OPC UA simulator and a Docker Compose demo stack.
- Read-only toward controllers: the collector never writes to a PLC.

## Tech stack

Python 3.12, asyncio, `asyncua` (OPC UA), SQLAlchemy 2 with `pg8000` (PostgreSQL), Alembic, FastAPI with uvicorn, Jinja2 and htmx (Panel), `cryptography` (Fernet), Pydantic, pytest, ruff, mypy, Docker and GitHub Actions.

## Architecture

```mermaid
flowchart LR
    PLC[OPC UA server] --> W[ConnectionWorker]
    W --> S[PollScheduler]
    S --> Q[Reading Queue]
    Q --> D[Database Writer]
    D --> P[(PostgreSQL)]
    D -. database unavailable .-> SP[Spool]
    SP -. replay .-> D
    S --> L[Live value cache]
    L --> UI[Panel /panel]
    L --> API[Data API /api/v1]
    P --> API
    P -- configuration --> SUP[RuntimeSupervisor]
    UI -- saved changes --> P
    UI -- Apply changes --> SUP
    SUP -- starts and replaces --> R[CollectorRuntime]
    R --> W
```

`RuntimeSupervisor` owns the database. At startup it waits for the database, applies migrations, imports the seed file once if the database has no configuration, loads the active configuration and starts a `CollectorRuntime`. The runtime creates one worker per enabled Connection and one scheduled cycle per Tag Group. Each cycle reads the group's Tags through the worker, puts the Poll Result on the Reading Queue, and records the values in an in-memory live value cache. The Database Writer takes Poll Results from the queue and stores them in PostgreSQL.

The Panel, the data API (`/api/v1`) and the operational endpoints (`/health/*`, `/api/runtime/*`, `/api/about`) run in the same process on one FastAPI app. The Panel saves configuration changes to the database; "Apply changes" asks the supervisor to replace the runtime with one built from the saved configuration, while the web server keeps serving. Current values come from the live value cache, and history comes from `tag_readings`.

## Quick start

You need Docker with Compose. The stack starts PostgreSQL 17, an OPC UA simulator of a production line, and the collector.

```bash
git clone https://github.com/Mateusz1256/plc-data-collector.git
cd plc-data-collector
cp .env.example .env
docker compose up
```

Replace the clone URL with the address of your copy of this repository. The `.env` file holds demo credentials; change the passwords for anything beyond a local demo.

When the log shows the collector polling, open the Panel at `http://localhost:8080/` and sign in with the password set in `PLC_DATA_COLLECTOR_ADMIN_PASSWORD` in `.env`. The Live page shows the four Tags of the demo Connection `line1_opcua`, updated every second. The port comes from `COLLECTOR_API_PORT` in `.env`, default `8080`, and is published on 127.0.0.1 only; the comment in `docker-compose.yml` explains how to expose it to other hosts deliberately.

Other endpoints to check:

- `http://127.0.0.1:8080/health/ready` returns `{"status": "ready", ...}` once the database is migrated and the workers run.
- `http://127.0.0.1:8080/api/runtime/workers` lists the worker of `line1_opcua` in state `running`.
- `http://127.0.0.1:8080/docs` is the interactive OpenAPI UI, including `/api/v1`.

Look at the stored readings:

```bash
docker compose exec postgres psql -U collector -d plc_data_collector -c \
  "select tag_id, quality, numeric_value, integer_value, text_value, source_timestamp
   from tag_readings order by received_at desc limit 8;"
```

You should see fresh rows for `line1_temperature`, `line1_pressure`, `line1_part_count` and `line1_machine_state`, one set per second. Stop the stack and remove its volumes with `docker compose down -v`.

## Configuration

Configuration is three lists: Connections, Tag Groups and Tags. A Tag Group belongs to one Connection and is read at one interval. A Tag belongs to one Tag Group. The database is the source of truth for all three ([ADR 0001](docs/adr/0001-database-is-configuration-source-of-truth.md)).

### In the Panel

Sign in at `/panel/login` with the admin password. Then:

1. On **Connections**, choose "Add connection", pick a protocol and enter the endpoint. "Test connection" checks that the controller answers.
2. On the Connection's page, add a Tag Group with its read interval, then add Tags to it with an address and a value type (`numeric`, `integer`, `boolean`, `text` or `binary`).
3. Saved changes do not affect collection yet: a banner shows what is pending. Choose "Apply changes" to restart collection with the saved configuration.

Deleting a Connection, Tag Group or Tag archives it: it is no longer polled or shown, and its stored readings stay ([ADR 0002](docs/adr/0002-archive-instead-of-delete.md)).

### JSON seed, export and import

**Settings** has "Export configuration", which downloads the configuration as JSON, and "Review import", which shows what a file would add, change and remove before it replaces the configuration. Passwords and other secrets are left out of exports; Connections that already exist keep theirs on import.

The same JSON format can seed an empty database. Set `PLC_DATA_COLLECTOR_CONFIG` (or `--config`) to a file, and it is imported on the first start, when the database has no Connections at all, archived ones included. After that the file is ignored; edit configuration in the Panel or import a file. The demo seed is `deploy/demo/collector.config.json`; a second example is `docs/examples/collector.config.example.json`.

A seed file with one Connection, Tag Group and Tag:

```json
{
  "connections": [
    {
      "id": "press_line",
      "protocol": "opcua",
      "endpoint": "opc.tcp://192.0.2.20:4840",
      "timeout_ms": 5000,
      "protocol_options": {}
    }
  ],
  "tag_groups": [
    {
      "id": "press_fast",
      "connection_id": "press_line",
      "interval_ms": 1000
    }
  ],
  "tags": [
    {
      "id": "press_temperature",
      "tag_group_id": "press_fast",
      "name": "Press temperature",
      "address": "ns=2;s=Press.Temperature",
      "value_type": "numeric"
    }
  ]
}
```

Items can be switched off with `"enabled": false`; disabled Connections, and Tag Groups without enabled Tags, are skipped. Use `"protocol": "mock"` for development without a controller; the Panel offers only production protocols. While the seed file is read, a field of a Connection can be overridden with `PLC_DATA_COLLECTOR_CONNECTIONS__<CONNECTION_ID>__<FIELD>`, for example `PLC_DATA_COLLECTOR_CONNECTIONS__PRESS_LINE__ENDPOINT`.

### Secrets

Protocol options whose names contain `password`, `passwd`, `secret`, `token`, `key` or `credential` are stored encrypted (Fernet) in the database, and the Panel and the API never show them. The key comes from `PLC_DATA_COLLECTOR_SECRET_KEY`. Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

If the variable is empty, a key is generated once into `secret.key` in the data directory and a `secret_key_generated` warning is logged. Back that file up: without it, stored protocol passwords cannot be decrypted and have to be entered again. A key derived from it signs the Panel session cookie.

### Environment variables

Settings come from environment variables with the prefix `PLC_DATA_COLLECTOR_`:

| Variable | Meaning |
| --- | --- |
| `PLC_DATA_COLLECTOR_ADMIN_PASSWORD` | Password for the Panel. When unset, the Panel is off and the sign-in page says so |
| `PLC_DATA_COLLECTOR_SECRET_KEY` | Fernet key for Connection secrets (default: generated into `<data dir>/secret.key`) |
| `PLC_DATA_COLLECTOR_CONFIG` | Optional JSON seed file, imported only into an empty database |
| `PLC_DATA_COLLECTOR_DATABASE_URL` | `postgresql+pg8000://user:password@host:5432/db` (default: SQLite file in the data directory) |
| `PLC_DATA_COLLECTOR_DATA_DIR` | Directory for the Spool, the generated secret key and runtime files |
| `PLC_DATA_COLLECTOR_API_HOST`, `PLC_DATA_COLLECTOR_API_PORT` | Listen address of the Panel and API (default `127.0.0.1:8080`) |
| `PLC_DATA_COLLECTOR_DATABASE_WAIT_TIMEOUT_S` | How long to wait for the database at startup (default 60) |
| `PLC_DATA_COLLECTOR_QUEUE_MAX_SIZE` | Reading Queue capacity (default 1000) |
| `PLC_DATA_COLLECTOR_WRITER_BATCH_SIZE`, `PLC_DATA_COLLECTOR_WRITER_FLUSH_INTERVAL_S` | Database Writer batching (defaults 100 and 1.0) |
| `PLC_DATA_COLLECTOR_STORAGE_TIMEOUT_S` | Storage operation timeout (default 5) |
| `PLC_DATA_COLLECTOR_SPOOL_MAX_ITEMS` | Spool capacity (default 100000) |
| `PLC_DATA_COLLECTOR_LOG_DIR`, `_RUN_DIR`, `_PID_FILE`, `_LOG_FILE`, `_LOG_LEVEL` | Service process paths and logging |

Secrets never appear in logs, API responses, the Panel or exports. Do not commit production endpoints or passwords.

Run the service (collector, Panel and API in one process) outside Docker with `plc-data-collector --run-service`, adding `--config <file>` to seed an empty database. Service-manager examples are in `docs/deployment/service-packaging.md`.

## Reading the data

There are two ways to read collected data.

**SQL.** The PostgreSQL schema is the main contract for consumers. [docs/database-schema.md](docs/database-schema.md) documents `tag_readings`, `tags`, `tag_groups` and `connections`, the value-type columns, quality values and indexes. Timestamps are stored as ISO 8601 text in UTC; cast them with `::timestamptz`.

**REST API.** Create an API key in the Panel under Settings ("Create key"). The key is shown once; only its SHA-256 hash is stored. Send it in the `X-API-Key` header:

```bash
export PDC_API_KEY="<the key from the Panel>"
curl -s -H "X-API-Key: $PDC_API_KEY" http://127.0.0.1:8080/api/v1/tags
curl -s -H "X-API-Key: $PDC_API_KEY" \
  "http://127.0.0.1:8080/api/v1/tags/line1_temperature/history?from=2026-11-13T08:00:00Z&to=2026-11-13T09:00:00Z"
```

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/tags` | Every active Tag with its current value (`null` before the first poll) |
| `GET /api/v1/tags/{tag_id}` | One active Tag with its current value |
| `GET /api/v1/tags/{tag_id}/history` | Stored readings in `[from, to)`, oldest first; archived Tags included. `from` and `to` are ISO 8601 with a timezone (default: the last hour); `limit` is 1 to 10000 (default 1000), and `truncated` is `true` when the range holds more |

Binary values are returned as base64. A missing, unknown or revoked key gets `401`. Revoke a key in Settings when an application no longer needs it.

**Grafana.** Add a PostgreSQL data source pointing at the collector database, preferably with a read-only role. An example time-series query for one Tag:

```sql
select r.received_at::timestamptz as time,
       r.numeric_value as value,
       t.name as metric
from tag_readings r
join tags t on t.id = r.tag_id
where r.tag_id = 'line1_temperature'
  and r.quality = 'good'
  and $__timeFilter(r.received_at::timestamptz)
order by 1;
```

## Design decisions

**The database is the configuration source of truth.** Connections, Tag Groups and Tags live in the database and are edited in the Panel; a JSON file is only a one-time seed and an import and export format. Two equal sources would drift apart ([ADR 0001](docs/adr/0001-database-is-configuration-source-of-truth.md)).

**Removed configuration is archived.** Deleting a Connection, Tag Group or Tag sets `archived_at` instead of deleting the row, because `tag_readings` has foreign keys to all three and Poll Results for a removed Tag may still wait in the Reading Queue or the Spool ([ADR 0002](docs/adr/0002-archive-instead-of-delete.md)).

**Apply changes is an in-process restart.** Saving in the Panel only writes to the database and bumps a configuration revision. "Apply changes" stops the current runtime, which drains its Reading Queue into the database or the Spool, then starts a new runtime from the saved configuration (`app/supervisor.py`). The web server keeps serving throughout. If the new runtime cannot start, `/health/ready` reports the error code, the Panel banner says collection is stopped, and Apply can be retried.

**API keys are hashed.** A key is `pdc_` plus 48 hex characters (192 random bits). Only its SHA-256 hash and a short prefix are stored (`persistence/api_keys.py`); because the key is random and long, an unsalted fast hash is enough and lets each request look the key up through a unique index.

**Reading Queue and backpressure.** Polling and storage are decoupled by a bounded `asyncio` queue (`runtime/reading_queue.py`). When storage lags, a producer waits up to the storage timeout and then fails with `reading_queue_full`; the cycle is counted as failed and the queue never grows without limit. `put_timeouts` and occupancy are exposed as metrics.

**Database Writer batching and retry.** The Database Writer (`persistence/writer.py`) collects Poll Results and flushes them by batch size or interval. A failed batch is retried with capped exponential backoff and jitter (`runtime/retry.py`). Inserts are idempotent: each reading has an `event_id` and `ON CONFLICT DO NOTHING` (`persistence/repositories.py`), so a replayed batch cannot create duplicates.

**Spool.** If a batch still fails after its retries, the writer moves it to a durable SQLite Spool on disk (`persistence/spool.py`), bounded by `SPOOL_MAX_ITEMS`, and replays it when the database recovers. On shutdown, Poll Results still in the Reading Queue are moved to the Spool as well (`app/runtime.py`).

**Worker isolation and reconnect.** Each Connection has its own worker and driver instance (`runtime/connection_worker.py`), so a dead controller degrades only its own worker. When a poll fails for every Tag, the worker calls the driver's `health_check()` and reconnects if it fails; Tags then report `connection_unavailable` until the link is back. Reconnects are counted in the `reconnect_attempts` metric. The runtime starts even when a controller is unreachable, and the supervisor waits for PostgreSQL up to a bounded timeout before exiting non-zero (`app/supervisor.py`).

**Driver contract.** Protocols plug in through one async `CommunicationDriver` interface with `connect`, `disconnect`, `read` and `health_check` (`protocols/driver.py`), created per Connection by a registry (`protocols/registry.py`). Adding a protocol means implementing that interface; the OPC UA (`protocols/opcua.py`) and mock (`protocols/mock.py`) drivers are the two implementations today.

**PostgreSQL and the `tag_readings` row format.** PostgreSQL is the storage target because readings are relational, queried with SQL and joined to configuration. Each Tag reading is one row (`persistence/schema.py`) with `source_timestamp`, `received_at`, `quality`, `value_type`, one typed column per value kind (`numeric_value`, `integer_value`, `boolean_value`, `text_value`, `binary_value`) and `error_code` / `error_message`. Failed reads are stored too, as rows with bad quality, so gaps are visible. Migrations ship inside the package (`persistence/migrations`) and run at startup (`persistence/migrate.py`).

## Known limitations and roadmap

Known limitations of 0.3.0:

- OPC UA is the only real protocol; the mock driver is for development.
- The Panel has one admin password and no user accounts. Serve it over HTTPS (for example behind a reverse proxy) beyond a trusted network.
- `/health/*`, `/api/runtime/*` and `/api/about` have no authentication; they carry operational state, not collected data.
- Every API key can read every Tag.
- Retention is global: one number of days for all Tags.
- A Poll Result that cannot enter a full Reading Queue within the timeout is dropped and counted as a failed cycle.
- If PostgreSQL is down and the Spool is full or cannot be written, the affected batch is dropped and counted (`spool_full_failures`).

Roadmap:

- 0.4.0: Modbus TCP.
- 0.5.0: Siemens S7.
- 0.6.0: M-Bus.
- 1.0.0: stabilization.
- Later: MQTT, TimescaleDB, OPC UA browse, writes to PLC, API key permissions per Connection, retention per Connection or Tag.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -c constraints.txt -e ".[dev]"

pytest -q
ruff check .
ruff format --check .
mypy
```

On Windows, activate with `.\.venv\Scripts\Activate.ps1`.

Tests marked `postgres` need a real PostgreSQL. Point them at one with `PLC_DATA_COLLECTOR_TEST_DATABASE_URL`, for example:

```bash
export PLC_DATA_COLLECTOR_TEST_DATABASE_URL="postgresql+pg8000://test:test@127.0.0.1:5432/plc_data_collector_test"
pytest -q
```

Without it those tests are skipped. `tools/smoke_test.sh` runs the whole Docker stack end to end, including the Panel sign-in and the data API, and `python tools/generate_credits.py` regenerates the third-party notices.

## License

MIT. See `LICENSE`. Third-party notices are in `THIRD_PARTY_NOTICES.md`; `plc-data-collector licenses` prints them.
