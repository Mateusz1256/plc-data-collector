# PLC Collector

Industrial PLC and OPC UA data collector written in Python.

PLC Collector reads configured groups of tags from industrial data sources,
normalizes the values, and writes poll results to relational storage. The MVP is
designed around small runtime components that are easy to test and operate:
protocol drivers, isolated connection workers, a non-overlapping scheduler, a
bounded reading queue, persistence repositories, a batched database writer,
durable disk spool support, and a read-only health API.

## Project Status

Current release: `0.1.0`

The MVP runtime is implemented and covered by automated tests. Production use
still requires environment-specific validation against the target PLCs, storage
backend, service manager, network policy, and secret-management process.

## Features

- JSON configuration loading with reference validation and safe diagnostics.
- Mock protocol driver for local development and deterministic tests.
- OPC UA protocol driver based on `asyncua`.
- One isolated worker per configured protocol connection.
- Fixed-interval scheduler with non-overlapping poll cycles.
- Bounded async queue between polling and storage.
- SQLAlchemy repositories and Alembic migrations for relational storage.
- Batched database writer with retry handling and optional durable spool.
- Read-only FastAPI endpoints for liveness, readiness, runtime state, and build
  information.
- Service-process helpers for runtime directories, PID files, structured JSON
  logging, and graceful shutdown.
- Bundled project license metadata and verified third-party credits.

## Requirements

- Python 3.12 or newer.
- SQLite for local development and MVP validation.
- Optional: an OPC UA server or device for protocol-level integration testing.

Runtime and development dependencies are pinned in `pyproject.toml`.

## Installation

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Quick Start

Print the basic application status:

```powershell
plc-gateway
```

Show the installed version:

```powershell
plc-gateway --version
```

Start the read-only API on localhost:

```powershell
plc-gateway --serve-api
```

Default API address:

```text
http://127.0.0.1:8080
```

Useful endpoints:

- `GET /health/live` - process liveness.
- `GET /health/ready` - readiness of critical prerequisites.
- `GET /api/runtime/components` - component state and runtime metrics.
- `GET /api/runtime/workers` - connection worker state and metrics.
- `GET /api/about` - version, build, license, and dependency credits.
- `GET /docs` - OpenAPI UI.

## Configuration

Configuration is loaded from JSON and validated before activation. A sample file
is available at:

```text
docs/examples/gateway.config.example.json
```

Minimal shape:

```json
{
  "connections": [
    {
      "id": "opcua_demo",
      "protocol": "opcua",
      "endpoint": "opc.tcp://127.0.0.1:4840",
      "timeout_ms": 5000,
      "protocol_options": {}
    }
  ],
  "tag_groups": [
    {
      "id": "fast",
      "connection_id": "opcua_demo",
      "interval_ms": 1000
    }
  ],
  "tags": [
    {
      "id": "temperature",
      "tag_group_id": "fast",
      "name": "Temperature",
      "address": "ns=2;s=Machine.Temperature",
      "value_type": "numeric"
    }
  ]
}
```

Run the service host with explicit runtime paths:

```powershell
plc-gateway --run-service `
  --config C:\ProgramData\PLC Collector\gateway.config.json `
  --data-dir C:\ProgramData\PLC Collector\data `
  --log-dir C:\ProgramData\PLC Collector\logs `
  --run-dir C:\ProgramData\PLC Collector\run
```

Supported environment variables:

- `PLC_GATEWAY_CONFIG`
- `PLC_GATEWAY_DATA_DIR`
- `PLC_GATEWAY_LOG_DIR`
- `PLC_GATEWAY_RUN_DIR`
- `PLC_GATEWAY_PID_FILE`
- `PLC_GATEWAY_LOG_FILE`
- `PLC_GATEWAY_LOG_LEVEL`
- `PLC_GATEWAY_API_HOST`
- `PLC_GATEWAY_API_PORT`

Connection fields can be overridden through environment variables:

```powershell
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENDPOINT = "opc.tcp://localhost:4841"
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__TIMEOUT_MS = "7500"
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENABLED = "false"
```

Sensitive configuration values are masked by the safe logging helpers. Do not
commit production endpoints, credentials, private keys, certificates, passwords,
or customer data.

## Database

Local development uses SQLite through SQLAlchemy Core. Alembic migrations are in
`migrations/`.

Apply migrations:

```powershell
alembic upgrade head
```

The schema contains:

- `connections`
- `tag_groups`
- `tags`
- `tag_readings`
- `poll_executions`
- `runtime_components`

Local `*.db` files are ignored by git.

## Drivers

### Mock

Use the `mock` protocol for local development and automated tests without a PLC
or OPC UA server.

Example protocol options:

```json
{
  "delay_ms": 250,
  "connect_failures_before_success": 1,
  "timeout_on_read": false,
  "tags": {
    "temperature": { "value": 20.5 },
    "counter": { "sequence": [1, 2, 3] },
    "broken": {
      "error_code": "mock_bad_tag",
      "error_message": "Configured tag failure"
    }
  }
}
```

### OPC UA

Use the `opcua` protocol for real OPC UA reads. The driver creates one async
client session per connection worker and maps per-node failures to tag-level
results without stopping unrelated workers.

Example protocol options:

```json
{
  "security_string": "Basic256Sha256,SignAndEncrypt,certs/client.der,certs/client.pem",
  "username": "operator",
  "password": "read-from-environment",
  "auto_reconnect": false,
  "reconnect_max_delay_s": 30,
  "reconnect_request_timeout_s": 60
}
```

## Testing

Run the full local validation suite:

```powershell
pytest -q
ruff check .
ruff format --check .
mypy src tests
```

Regenerate and validate third-party credits:

```powershell
python tools/generate_credits.py
```

Build a local wheel without dependencies:

```powershell
pip wheel . --no-deps -w dist
```

## Release

Release notes:

```text
docs/release-notes/0.1.0.md
```

Expected wheel artifact:

```text
dist/plc_gateway-0.1.0-py3-none-any.whl
```

Known MVP limitations:

- Service orchestration is delegated to systemd, WinSW, NSSM, or another
  external process manager.
- Encrypted secret storage is not included.
- Runtime API endpoints are read-only.
- SQLite is the validated local persistence backend.
- Real PLC interoperability must be validated against target devices before
  production rollout.

## Project Layout

```text
src/plc_gateway/
  api/          FastAPI read-only runtime API
  app/          application bootstrap, service host, config, logging
  domain/       framework-independent domain models and exceptions
  persistence/  SQLAlchemy repositories, writer, and disk spool
  protocols/    driver contract, mock driver, OPC UA driver, registry
  runtime/      scheduler, retry, reading queue, connection worker
tests/          unit and integration-style tests
docs/           task roadmap, examples, release notes, deployment notes
migrations/     Alembic migrations
tools/          maintenance scripts
```

## Deployment

Service wrapper examples are documented in:

```text
docs/deployment/service-packaging.md
```

The service host creates data, log, and runtime directories, writes a PID file,
logs structured JSON to file, and handles graceful shutdown signals.

## License and Credits

The project currently uses:

```text
LicenseRef-PLC-Gateway-Proprietary
```

See `LICENSE` for the project license notice.

Verified third-party dependency notices are maintained in
`THIRD_PARTY_NOTICES.md` and bundled into the package as
`plc_gateway/license_data/credits.json`.

Show license and dependency credits:

```powershell
plc-gateway licenses
```

The Python package and CLI entry point currently keep the historical
`plc_gateway` / `plc-gateway` names.
