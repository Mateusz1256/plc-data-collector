# Changelog

Wszystkie istotne zmiany w projekcie beda dokumentowane w tym pliku.

Format opiera sie na Keep a Changelog, a projekt uzywa Semantic Versioning.

## [Unreleased]

### Added

- Initial project planning files.
- Incremental MVP task roadmap.
- Contribution and agent guidelines.
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

### Changed

### Deprecated

### Removed

### Fixed

### Security

