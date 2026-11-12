# PLC Data Collector

An asyncio service that polls industrial controllers over OPC UA and stores the Tag readings in PostgreSQL.

## Problem

Production data lives in controllers that are awkward to query: values are addressed by node IDs, links drop, and a slow database must never stall the polling. PLC Data Collector reads configured Tags on a fixed interval, keeps one broken Connection from affecting the others, and writes every reading to a relational table that ordinary SQL tools can query. Nothing is lost when the database is briefly unavailable, because Poll Results wait in a durable Spool.

## Features

- OPC UA driver (`asyncua`) with batched reads, source timestamps and per-Tag quality.
- Mock driver for local development and deterministic tests.
- One isolated worker per Connection, with automatic reconnect.
- Fixed-interval scheduler per Tag Group that never overlaps its own cycles.
- Bounded Reading Queue between polling and storage.
- Database Writer that stores Poll Results in batches, with retry and backoff.
- Durable on-disk Spool that holds Poll Results while PostgreSQL is down and replays them afterwards.
- PostgreSQL storage with Alembic migrations applied at startup (SQLite is the fallback when no database URL is set).
- Read-only HTTP API (FastAPI) for liveness, readiness, runtime state and build information.
- Built-in OPC UA simulator and a Docker Compose demo stack.
- JSON configuration validated at load time, with environment overrides and masked secrets.
- Read-only toward controllers: the collector never writes to a PLC.

## Tech stack

Python 3.12, asyncio, `asyncua` (OPC UA), SQLAlchemy 2 with `pg8000` (PostgreSQL), Alembic, FastAPI with uvicorn, Pydantic, pytest, ruff, mypy, Docker and GitHub Actions.

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
    R[Collector runtime state] --> A[HTTP API]
    W -.-> R
    Q -.-> R
    D -.-> R
```

The runtime (`CollectorRuntime`) creates one worker per enabled Connection and one scheduled cycle per Tag Group. Each cycle reads the group's Tags through the worker and puts the Poll Result on the Reading Queue. The Database Writer takes Poll Results from the queue and stores them in PostgreSQL. The API runs in the same process and only reads runtime state.

## Quick start

You need Docker with Compose. The stack starts PostgreSQL 17, an OPC UA simulator of a production line, and the collector.

```bash
git clone https://github.com/Mateusz1256/plc-data-collector.git
cd plc-data-collector
cp .env.example .env
docker compose up
```

Replace the clone URL with the address of your copy of this repository. The `.env` file holds demo credentials; change the password for anything beyond a local demo.

When the log shows the collector polling, check it from another terminal (the API port comes from `COLLECTOR_API_PORT` in `.env`, default `8080`):

- `http://127.0.0.1:8080/health/ready` returns `{"status": "ready", ...}` once the database is migrated and the workers run.
- `http://127.0.0.1:8080/api/runtime/workers` lists the worker of the demo Connection `line1_opcua` in state `running`.
- `http://127.0.0.1:8080/docs` is the interactive OpenAPI UI.

Look at the stored readings:

```bash
docker compose exec postgres psql -U collector -d plc_data_collector -c \
  "select tag_id, quality, numeric_value, integer_value, text_value, source_timestamp
   from tag_readings order by received_at desc limit 8;"
```

You should see fresh rows for `line1_temperature`, `line1_pressure`, `line1_part_count` and `line1_machine_state`, one set per second. Stop the stack and remove its volumes with `docker compose down -v`.

## Configuration

The collector reads a JSON file with three lists: Connections, Tag Groups and Tags. A Tag Group belongs to one Connection and is read at one interval. A Tag belongs to one Tag Group. The file used by the demo is `deploy/demo/collector.config.json`; a second example is `docs/examples/collector.config.example.json`.

To add a Connection with a Tag Group and a Tag:

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

`value_type` is one of `numeric`, `integer`, `boolean`, `text` or `binary`. Items can be switched off with `"enabled": false`; disabled Connections, and Tag Groups without enabled Tags, are skipped. Use `"protocol": "mock"` for development without a controller.

Settings come from environment variables with the prefix `PLC_DATA_COLLECTOR_`:

| Variable | Meaning |
| --- | --- |
| `PLC_DATA_COLLECTOR_CONFIG` | Path to the JSON configuration (required) |
| `PLC_DATA_COLLECTOR_DATABASE_URL` | `postgresql+pg8000://user:password@host:5432/db` (default: SQLite file in the data directory) |
| `PLC_DATA_COLLECTOR_DATA_DIR` | Directory for the Spool and runtime files |
| `PLC_DATA_COLLECTOR_API_HOST`, `PLC_DATA_COLLECTOR_API_PORT` | API listen address (default `127.0.0.1:8080`) |
| `PLC_DATA_COLLECTOR_DATABASE_WAIT_TIMEOUT_S` | How long to wait for the database at startup (default 60) |
| `PLC_DATA_COLLECTOR_QUEUE_MAX_SIZE` | Reading Queue capacity (default 1000) |
| `PLC_DATA_COLLECTOR_WRITER_BATCH_SIZE`, `PLC_DATA_COLLECTOR_WRITER_FLUSH_INTERVAL_S` | Database Writer batching (defaults 100 and 1.0) |
| `PLC_DATA_COLLECTOR_STORAGE_TIMEOUT_S` | Storage operation timeout (default 5) |
| `PLC_DATA_COLLECTOR_SPOOL_MAX_ITEMS` | Spool capacity (default 100000) |
| `PLC_DATA_COLLECTOR_LOG_DIR`, `_RUN_DIR`, `_PID_FILE`, `_LOG_FILE`, `_LOG_LEVEL` | Service process paths and logging |

A field of a Connection can be overridden with `PLC_DATA_COLLECTOR_CONNECTIONS__<CONNECTION_ID>__<FIELD>`, for example:

```bash
export PLC_DATA_COLLECTOR_CONNECTIONS__PRESS_LINE__ENDPOINT="opc.tcp://192.0.2.21:4840"
export PLC_DATA_COLLECTOR_CONNECTIONS__PRESS_LINE__ENABLED="false"
```

Credentials in endpoints or `protocol_options` are masked in logs and never returned by the API. Do not commit production endpoints or passwords.

Run the service (collector and API in one process) outside Docker with `plc-data-collector --run-service --config <file>`. Service-manager examples are in `docs/deployment/service-packaging.md`.

## Design decisions

**Reading Queue and backpressure.** Polling and storage are decoupled by a bounded `asyncio` queue (`runtime/reading_queue.py`). When storage lags, a producer waits up to the storage timeout and then fails with `reading_queue_full`; the cycle is counted as failed and the queue never grows without limit. `put_timeouts` and occupancy are exposed as metrics.

**Database Writer batching and retry.** The Database Writer (`persistence/writer.py`) collects Poll Results and flushes them by batch size or interval. A failed batch is retried with capped exponential backoff and jitter (`runtime/retry.py`). Inserts are idempotent: each reading has an `event_id` and `ON CONFLICT DO NOTHING` (`persistence/repositories.py`), so a replayed batch cannot create duplicates.

**Spool.** If a batch still fails after its retries, the writer moves it to a durable SQLite Spool on disk (`persistence/spool.py`), bounded by `SPOOL_MAX_ITEMS`, and replays it when the database recovers. On shutdown, Poll Results still in the Reading Queue are moved to the Spool as well (`app/runtime.py`).

**Worker isolation and reconnect.** Each Connection has its own worker and driver instance (`runtime/connection_worker.py`), so a dead controller degrades only its own worker. When a poll fails for every Tag, the worker calls the driver's `health_check()` and reconnects if it fails; Tags then report `connection_unavailable` until the link is back. Reconnects are counted in the `reconnect_attempts` metric. The runtime starts even when a controller is unreachable, and waits for PostgreSQL up to a bounded timeout before exiting non-zero (`app/runtime.py`).

**Driver contract.** Protocols plug in through one async `CommunicationDriver` interface with `connect`, `disconnect`, `read` and `health_check` (`protocols/driver.py`), created per Connection by a registry (`protocols/registry.py`). Adding a protocol means implementing that interface; the OPC UA (`protocols/opcua.py`) and mock (`protocols/mock.py`) drivers are the two implementations today.

**PostgreSQL and the `tag_readings` row format.** PostgreSQL is the storage target because readings are relational, queried with SQL and joined to configuration. Each Tag reading is one row (`persistence/schema.py`) with `source_timestamp`, `received_at`, `quality`, `value_type`, one typed column per value kind (`numeric_value`, `integer_value`, `boolean_value`, `text_value`, `binary_value`) and `error_code` / `error_message`. Failed reads are stored too, as rows with bad quality, so gaps are visible. Migrations ship inside the package (`persistence/migrations`) and run at startup (`persistence/migrate.py`).

## Known limitations and roadmap

Known limitations of 0.2.0:

- OPC UA is the only real protocol; the mock driver is for development.
- Configuration is a JSON file, edited by hand; there is no web UI.
- The API is read-only and has no authentication, so keep it on a trusted network.
- A Poll Result that cannot enter a full Reading Queue within the timeout is dropped and counted as a failed cycle.
- If PostgreSQL is down and the Spool is full or cannot be written, the affected batch is dropped and counted (`spool_full_failures`).
- There is no data retention: `tag_readings` grows until you prune it.
- Encrypted secret storage is not included.

Roadmap:

- 0.3.0: Panel (web UI), configuration stored in the database, REST API with API keys, retention.
- 0.4.0: Modbus TCP.
- 0.5.0: Siemens S7.
- 0.6.0: M-Bus.
- Later: MQTT, TimescaleDB, OPC UA browse, writes to PLC.

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

Without it those tests are skipped. `tools/smoke_test.sh` runs the whole Docker stack end to end, and `python tools/generate_credits.py` regenerates the third-party notices.

## License

MIT. See `LICENSE`. Third-party notices are in `THIRD_PARTY_NOTICES.md`; `plc-data-collector licenses` prints them.
