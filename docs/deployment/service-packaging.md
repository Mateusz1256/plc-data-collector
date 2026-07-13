# Service Packaging

PLC Collector moze dzialac jako dlugotrwaly proces uruchamiany przez manager
uslug systemu operacyjnego. Core aplikacji nie zalezy od systemd ani Windows
Service API; integracja z usluga jest zewnetrznym wrapperem procesu.

## CLI

Tryb uslugi:

```powershell
plc-gateway --run-service `
  --config C:\ProgramData\PLC Collector\gateway.config.json `
  --data-dir C:\ProgramData\PLC Collector\data `
  --log-dir C:\ProgramData\PLC Collector\logs `
  --run-dir C:\ProgramData\PLC Collector\run
```

Linux:

```bash
plc-gateway --run-service \
  --config /etc/plc-gateway/gateway.config.json \
  --data-dir /var/lib/plc-gateway \
  --log-dir /var/log/plc-gateway \
  --run-dir /run/plc-gateway
```

Supported environment variables:

- `PLC_GATEWAY_CONFIG`
- `PLC_GATEWAY_DATA_DIR`
- `PLC_GATEWAY_LOG_DIR`
- `PLC_GATEWAY_RUN_DIR`
- `PLC_GATEWAY_PID_FILE`
- `PLC_GATEWAY_LOG_FILE`
- `PLC_GATEWAY_LOG_LEVEL`

Runtime data must live outside the installation directory. The service process
creates data, log and run directories at startup, writes a PID file, and removes
the PID file during graceful shutdown. `SIGTERM` and `SIGINT` request graceful
shutdown. On Windows console/service wrappers can use the equivalent stop
signal, for example Ctrl-Break or process termination handled by the wrapper.

## systemd

Example unit:

```ini
[Unit]
Description=PLC Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=plcgateway
Group=plcgateway
Environment=PLC_GATEWAY_LOG_LEVEL=INFO
ExecStart=/opt/plc-gateway/.venv/bin/plc-gateway --run-service \
  --config /etc/plc-gateway/gateway.config.json \
  --data-dir /var/lib/plc-gateway \
  --log-dir /var/log/plc-gateway \
  --run-dir /run/plc-gateway
PIDFile=/run/plc-gateway/plc-gateway.pid
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
sudo install -d -o plcgateway -g plcgateway /etc/plc-gateway /var/lib/plc-gateway /var/log/plc-gateway
sudo systemctl daemon-reload
sudo systemctl enable --now plc-gateway
sudo systemctl status plc-gateway
```

## WinSW

Example `plc-gateway.xml`:

```xml
<service>
  <id>plc-gateway</id>
  <name>PLC Collector</name>
  <description>Industrial PLC and OPC data collector</description>
  <executable>C:\PLC Collector\.venv\Scripts\plc-gateway.exe</executable>
  <arguments>--run-service --config "C:\ProgramData\PLC Collector\gateway.config.json" --data-dir "C:\ProgramData\PLC Collector\data" --log-dir "C:\ProgramData\PLC Collector\logs" --run-dir "C:\ProgramData\PLC Collector\run"</arguments>
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
nssm install plc-gateway "C:\PLC Collector\.venv\Scripts\plc-gateway.exe"
nssm set plc-gateway AppParameters --run-service --config "C:\ProgramData\PLC Collector\gateway.config.json" --data-dir "C:\ProgramData\PLC Collector\data" --log-dir "C:\ProgramData\PLC Collector\logs" --run-dir "C:\ProgramData\PLC Collector\run"
nssm set plc-gateway AppDirectory "C:\PLC Collector"
nssm set plc-gateway AppStdout "C:\ProgramData\PLC Collector\logs\service.stdout.log"
nssm set plc-gateway AppStderr "C:\ProgramData\PLC Collector\logs\service.stderr.log"
nssm set plc-gateway AppRotateFiles 1
nssm set plc-gateway AppRotateBytes 10485760
nssm start plc-gateway
```

## Updates

Recommended update flow:

1. Stop the service.
2. Back up configuration, main database and spool directory.
3. Install the new package or virtual environment in a separate installation
   directory.
4. Run database migrations if the release notes require them.
5. Start the service and check logs plus `/health/ready`.
6. Keep the previous installation until the new version has processed backlog
   from the durable spool.

Do not delete `data-dir` or the durable spool during upgrades.
