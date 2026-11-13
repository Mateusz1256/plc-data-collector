# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic
Versioning.

## [Unreleased]

## [0.3.0] - 2026-11-13

### Added

- Panel at `/panel`, a password-protected web UI (FastAPI, Jinja and htmx).
  Sign-in uses `PLC_DATA_COLLECTOR_ADMIN_PASSWORD`; when it is unset, the
  Panel is off. Pages: Live (current value, quality, update time and
  freshness of every Tag, refreshed every second), Connections with "Test
  connection", Tag Groups and Tags, and Settings. `/` redirects to the Live
  page.
- Configuration stored in the database (Connections, Tag Groups and Tags),
  with a revision that the pending-changes banner compares with the running
  configuration. ADR 0001 records the decision.
- "Apply changes": `RuntimeSupervisor` stops the running collection, drains
  the Reading Queue into the database or the Spool, and starts a new runtime
  from the saved configuration while the web server keeps serving.
- Configuration export and import as JSON in Settings. Import shows what will
  be added, changed and removed before it replaces the configuration.
- Removed Connections, Tag Groups and Tags are archived (`archived_at`), so
  their stored readings stay; creating an item with an archived ID restores it.
  ADR 0002 records the decision.
- Connection secrets encrypted with Fernet in `connections.secret_options`,
  with the key from `PLC_DATA_COLLECTOR_SECRET_KEY` or generated once into
  `secret.key` in the data directory.
- Read-only data API at `/api/v1`: `/tags`, `/tags/{tag_id}` with current
  values from an in-memory live value cache, and `/tags/{tag_id}/history`
  with a time range and a limit.
- Per-application API keys, created and revoked in Settings, sent in the
  `X-API-Key` header. A key is shown once; only its SHA-256 hash and prefix
  are stored, and the last use is recorded.
- Global retention in Settings (off by default, 1 to 3650 days) that deletes
  old `tag_readings` and `poll_executions` in batches, hourly and right after
  the setting is saved.
- Migration 0003: configuration columns, `app_settings`, `api_keys`, and
  indexes on `tag_readings` (`received_at`; `tag_id, received_at`) and
  `poll_executions` (`started_at`).
- `docs/database-schema.md`, the documented contract for SQL consumers, and
  a Grafana example in the README.
- The Docker smoke test signs in to the Panel, creates an API key and reads
  `/api/v1`.

### Changed

- **Breaking:** the JSON config file is now only imported when the database
  has no configuration; edit configuration in the Panel or import a file.
  Environment overrides of Connection fields
  (`PLC_DATA_COLLECTOR_CONNECTIONS__<ID>__<FIELD>`) apply only to that
  import.
- `RuntimeSettings.config_file` is optional, and so are
  `PLC_DATA_COLLECTOR_CONFIG` and `--config`.
- A 0.2.0 database is not re-seeded from the file. On first start its
  plaintext protocol secrets move to the encrypted `secret_options` column.
- The demo stack passes `PLC_DATA_COLLECTOR_ADMIN_PASSWORD` and
  `PLC_DATA_COLLECTOR_SECRET_KEY` to the collector; `.env.example` documents
  both.
- New direct runtime dependencies: `cryptography` (already installed through
  `asyncua`), `jinja2`, `python-multipart` and `itsdangerous`, plus vendored
  htmx 2.0.11, all listed in `THIRD_PARTY_NOTICES.md`.

## [0.2.0] - 2026-11-12

### Added

- Runtime composition (`CollectorRuntime`) that wires Connection workers, the
  scheduler, the Reading Queue, the Database Writer and the Spool, and the
  `plc-data-collector --run-service` mode that runs the runtime and the API
  together.
- PostgreSQL support through `pg8000`, with a bounded wait for the database at
  startup and Alembic migrations packaged with the library and applied at
  startup.
- Worker reconnect: when every Tag of a poll fails, the worker checks the driver
  health and reconnects; Tags report `connection_unavailable` meanwhile, and
  reconnects are counted in the `reconnect_attempts` metric.
- OPC UA simulator of a production line for demos and tests.
- Dockerfile, a Docker Compose demo stack (PostgreSQL 17, simulator, collector),
  `.env.example` and an end-to-end smoke test script.
- CI with tests (including PostgreSQL), ruff, mypy, a credits check and a Docker
  smoke test, plus `constraints.txt` pinning all dependencies.
- Portfolio README with architecture diagram and design decisions.

### Changed

- **Breaking:** renamed the package, CLI and environment variable prefix to
  `plc_data_collector`, `plc-data-collector` and `PLC_DATA_COLLECTOR_`. Update
  imports, service definitions and environment variables.
- Relicensed the project under the MIT License.
- Documentation is now written in English.

### Fixed

- Failed tag readings now persist the configured tag `value_type`, preventing
  storage batch rejection on error-only poll results.
- PostgreSQL no longer rejects a whole storage batch, which then stayed in the
  Spool, when a reading holds an integer above 2^31, text longer than 4096
  characters or a long error message. Migration 0002 widens
  `tag_readings.integer_value` to BIGINT and the text value and error message
  columns to TEXT; it runs automatically at startup. The Poll Result error
  message now lists distinct Tag errors only and is capped at 512 characters.
- PostgreSQL connections use a 10 s socket timeout, so an unreachable database
  host no longer blocks the startup database wait, Spool writes or shutdown for
  minutes.
- Tag Groups of one Connection no longer reconnect at the same time; one
  reconnect runs per outage, and the OPC UA driver closes its previous session
  before opening a new one, so sessions are not leaked on the controller.

## [0.1.0] - 2026-07-22

Internal release under the working name "PLC Collector"; not published.

### Added

- Bootstrap Python package with editable install support.
- Minimal command entry point exposing application status and version.
- Pytest, pytest-asyncio, ruff, and mypy configuration.
- GitHub Actions workflow for tests, linting, formatting check, and type check.
- Core framework-independent domain models, enums, and exceptions.
- JSON configuration loading with Pydantic validation, environment overrides,
  reference checks, secret masking, and an example configuration file.
- Protocol driver contract, capability metadata, factory type, and registry for
  creating drivers by configured protocol name.
- Deterministic mock protocol driver with configurable values, sequences,
  delays, timeouts, connection failures, tag failures, and health checks.
- Async non-overlapping poll scheduler with monotonic timing, missed-cycle
  accounting, stop/cancel controls, and observable scheduling state.
- Isolated connection worker lifecycle with owned driver instances, observable
  health state, heartbeat, poll execution summaries, and cancellation cleanup.
- Bounded reading queue for worker poll results with producer timeouts,
  occupancy metrics, consumer acknowledgement, and explicit shutdown outcomes.
- SQLAlchemy/Alembic relational persistence schema and repositories for
  configuration, poll executions, tag readings, and runtime component status.
- Batched database writer that consumes the bounded reading queue, flushes by
  size or interval, retries storage failures, and records writer metrics.
- Read-only FastAPI health and runtime status endpoints with localhost default
  API serving support.
- Shared retry policy with capped exponential backoff, jitter, error
  classification, structured correlation logs, and runtime worker metrics.
- Asynchronous OPC UA driver with batch reads, source timestamps, quality
  mapping, timeout handling, reconnect support, and marked integration tests.
- Durable SQLite offline spool with bounded capacity, retry scheduling,
  restart recovery, and database writer replay support.
- Service process packaging helpers with configurable runtime paths, PID file,
  file logging, graceful shutdown handling, and deployment examples.
- Project license metadata, bundled verified dependency credits, `licenses` CLI
  output, and build/license information in `/api/about`.
- MVP release validation covering multi-cycle runtime polling, connection
  failure isolation, storage failure spool/replay, graceful shutdown draining,
  release notes, known limitations, and distribution artifact guidance.

### Changed

### Deprecated

### Removed

### Fixed

### Security
