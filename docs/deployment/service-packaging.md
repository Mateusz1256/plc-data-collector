# Service Packaging

PLC Data Collector can run as a long-lived process started by the operating
system service manager. The application core does not depend on systemd or the
Windows Service API; service integration is an external process wrapper.

## CLI

Service mode:

```powershell
plc-data-collector --run-service `
  --config C:\ProgramData\PLC Data Collector\collector.config.json `
  --data-dir C:\ProgramData\PLC Data Collector\data `
  --log-dir C:\ProgramData\PLC Data Collector\logs `
  --run-dir C:\ProgramData\PLC Data Collector\run
```

Linux:

```bash
plc-data-collector --run-service \
  --config /etc/plc-data-collector/collector.config.json \
  --data-dir /var/lib/plc-data-collector \
  --log-dir /var/log/plc-data-collector \
  --run-dir /run/plc-data-collector
```

`--config` (or `PLC_DATA_COLLECTOR_CONFIG`) is optional. The file only seeds
the configuration on the first start, while the database has no
configuration; after that, edit configuration in the Panel. Without it, the
collector starts with an empty configuration.

Supported environment variables:

- `PLC_DATA_COLLECTOR_CONFIG` (optional; seed only)
- `PLC_DATA_COLLECTOR_ADMIN_PASSWORD`: the Panel sign-in password. Unset
  turns the Panel off.
- `PLC_DATA_COLLECTOR_SECRET_KEY`: the Fernet key that encrypts stored
  protocol passwords. Unset, the collector generates `secret.key` in the
  data directory once.
- `PLC_DATA_COLLECTOR_DATA_DIR`
- `PLC_DATA_COLLECTOR_LOG_DIR`
- `PLC_DATA_COLLECTOR_RUN_DIR`
- `PLC_DATA_COLLECTOR_PID_FILE`
- `PLC_DATA_COLLECTOR_LOG_FILE`
- `PLC_DATA_COLLECTOR_LOG_LEVEL`

Runtime data must live outside the installation directory. The service process
creates data, log and run directories at startup, writes a PID file, and removes
the PID file during graceful shutdown. `SIGTERM` and `SIGINT` request graceful
shutdown. On Windows console/service wrappers can use the equivalent stop
signal, for example Ctrl-Break or process termination handled by the wrapper.

## systemd

Example unit:

```ini
[Unit]
Description=PLC Data Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=plc-data-collector
Group=plc-data-collector
Environment=PLC_DATA_COLLECTOR_LOG_LEVEL=INFO
ExecStart=/opt/plc-data-collector/.venv/bin/plc-data-collector --run-service \
  --config /etc/plc-data-collector/collector.config.json \
  --data-dir /var/lib/plc-data-collector \
  --log-dir /var/log/plc-data-collector \
  --run-dir /run/plc-data-collector
PIDFile=/run/plc-data-collector/plc-data-collector.pid
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Operational commands:

```bash
sudo install -d -o plc-data-collector -g plc-data-collector /etc/plc-data-collector /var/lib/plc-data-collector /var/log/plc-data-collector
sudo systemctl daemon-reload
sudo systemctl enable --now plc-data-collector
sudo systemctl status plc-data-collector
```

## WinSW

Example `plc-data-collector.xml`:

```xml
<service>
  <id>plc-data-collector</id>
  <name>PLC Data Collector</name>
  <description>Industrial PLC and OPC data collector</description>
  <executable>C:\PLC Data Collector\.venv\Scripts\plc-data-collector.exe</executable>
  <arguments>--run-service --config "C:\ProgramData\PLC Data Collector\collector.config.json" --data-dir "C:\ProgramData\PLC Data Collector\data" --log-dir "C:\ProgramData\PLC Data Collector\logs" --run-dir "C:\ProgramData\PLC Data Collector\run"</arguments>
  <log mode="roll-by-size">
    <sizeThreshold>10485760</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
  <onfailure action="restart" delay="5 sec" />
</service>
```

Install:

```powershell
.\winsw.exe install
.\winsw.exe start
.\winsw.exe status
```

## NSSM

Example installation:

```powershell
nssm install plc-data-collector "C:\PLC Data Collector\.venv\Scripts\plc-data-collector.exe"
nssm set plc-data-collector AppParameters --run-service --config "C:\ProgramData\PLC Data Collector\collector.config.json" --data-dir "C:\ProgramData\PLC Data Collector\data" --log-dir "C:\ProgramData\PLC Data Collector\logs" --run-dir "C:\ProgramData\PLC Data Collector\run"
nssm set plc-data-collector AppDirectory "C:\PLC Data Collector"
nssm set plc-data-collector AppStdout "C:\ProgramData\PLC Data Collector\logs\service.stdout.log"
nssm set plc-data-collector AppStderr "C:\ProgramData\PLC Data Collector\logs\service.stderr.log"
nssm set plc-data-collector AppRotateFiles 1
nssm set plc-data-collector AppRotateBytes 10485760
nssm start plc-data-collector
```

## Updates

Recommended update flow:

1. Stop the service.
2. Back up the database (it holds the configuration and the readings; with
   SQLite, the database file in the data directory), `secret.key` in the
   data directory (or keep `PLC_DATA_COLLECTOR_SECRET_KEY` safe), the spool
   directory and, if used, the seed configuration file. Without the secret
   key, stored protocol passwords cannot be decrypted.
3. Install the new package or virtual environment in a separate installation
   directory.
4. Run database migrations if the release notes require them.
5. Start the service and check logs plus `/health/ready`.
6. Keep the previous installation until the new version has processed backlog
   from the durable spool.

Do not delete `data-dir` or the durable spool during upgrades.
