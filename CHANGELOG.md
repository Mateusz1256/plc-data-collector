# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic
Versioning.

## [Unreleased]

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
