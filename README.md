# PLC Collector

Industrial PLC and OPC UA data collector written in Python.

PLC Collector is an MVP-stage runtime for reading groups of tags from industrial
data sources, normalizing the results, and writing them to relational storage.
The codebase is built around small, testable modules: protocol drivers,
connection workers, a bounded reading queue, a scheduler, persistence
repositories, a database writer, disk spool support, and a read-only health API.

## Status

The project is under active MVP development. Core runtime components are
implemented and covered by tests, while full end-to-end production wiring is
still being completed.

Current capabilities include:

- JSON configuration loading and validation.
- Mock protocol driver for local testing.
- Async OPC UA driver based on `asyncua`.
- Per-connection worker lifecycle and retry handling.
- Poll scheduler with bounded overlap behavior.
- Bounded reading queue.
- SQLAlchemy-based persistence repositories.
- Database writer with batching, retry, and optional disk spool.
- FastAPI read-only health and runtime API.
- Service-process host with PID file, JSON logging, and graceful shutdown.
- License and third-party credits reporting.

## Requirements

- Python 3.12+
- SQLite for local development
- Optional: an OPC UA server for real protocol testing

Runtime dependencies are pinned in `pyproject.toml`.

## Installation

Create a virtual environment and install the package in editable mode:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Linux/macOS:

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

The default API address is:

```text
http://127.0.0.1:8080
```

Useful endpoints:

- `GET /health/live` - process liveness
- `GET /health/ready` - readiness of critical prerequisites
- `GET /api/runtime/components` - runtime component state and metrics
- `GET /api/runtime/workers` - connection worker state and metrics
- `GET /api/about` - version, build, project license, and dependency credits
- `GET /docs` - OpenAPI UI

Example:

```powershell
curl http://127.0.0.1:8080/health/live
curl http://127.0.0.1:8080/api/about
```

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

Run the service host with an explicit config and runtime directories:

```powershell
plc-gateway --run-service `
  --config C:\ProgramData\PLC Collector\gateway.config.json `
  --data-dir C:\ProgramData\PLC Collector\data `
  --log-dir C:\ProgramData\PLC Collector\logs `
  --run-dir C:\ProgramData\PLC Collector\run
```

The same values can be provided through environment variables:

- `PLC_GATEWAY_CONFIG`
- `PLC_GATEWAY_DATA_DIR`
- `PLC_GATEWAY_LOG_DIR`
- `PLC_GATEWAY_RUN_DIR`
- `PLC_GATEWAY_PID_FILE`
- `PLC_GATEWAY_LOG_FILE`
- `PLC_GATEWAY_LOG_LEVEL`
- `PLC_GATEWAY_API_HOST`
- `PLC_GATEWAY_API_PORT`

Connection fields can also be overridden with environment variables:

```powershell
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENDPOINT = "opc.tcp://localhost:4841"
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__TIMEOUT_MS = "7500"
$env:PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENABLED = "false"
```

Sensitive values in configuration are masked by the safe logging helpers.

## Local Database

The local development backend uses SQLite through SQLAlchemy Core. Alembic
migrations are stored in `migrations/`.

Apply migrations:

```powershell
alembic upgrade head
```

The schema includes:

- `connections`
- `tag_groups`
- `tags`
- `tag_readings`
- `poll_executions`
- `runtime_components`

Local `*.db` files are ignored by git.

## Drivers

### Mock Driver

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

### OPC UA Driver

Use the `opcua` protocol for real OPC UA reads. The driver creates one async
client session per connection worker and maps per-node failures to tag-level
results without failing unrelated workers.

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

Do not commit production endpoints, private keys, certificates, passwords, or
customer data.

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
docs/           task roadmap, examples, and deployment notes
migrations/     Alembic migrations
tools/          maintenance scripts
```

## Deployment Notes

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

