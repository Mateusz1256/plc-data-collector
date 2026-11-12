"""Command line entry point for PLC Data Collector."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence

from plc_data_collector._version import get_version
from plc_data_collector.app.health import build_health_status
from plc_data_collector.app.logging import configure_logging
from plc_data_collector.app.service import ServicePaths, run_service
from plc_data_collector.license_info import load_license_report


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="plc-data-collector")
    parser.add_argument(
        "--version",
        action="store_true",
        help="show application version and exit",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("PLC_DATA_COLLECTOR_LOG_LEVEL", "INFO"),
        help="logging level, defaults to PLC_DATA_COLLECTOR_LOG_LEVEL or INFO",
    )
    parser.add_argument(
        "--serve-api",
        action="store_true",
        help="serve the read-only health and runtime API",
    )
    parser.add_argument(
        "--run-service",
        action="store_true",
        help="run as a long-running service process until shutdown signal",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("PLC_DATA_COLLECTOR_CONFIG"),
        help="path to collector JSON config",
    )
    parser.add_argument(
        "--data-dir",
        default=os.getenv("PLC_DATA_COLLECTOR_DATA_DIR"),
        help="runtime data directory outside the installation directory",
    )
    parser.add_argument(
        "--log-dir",
        default=os.getenv("PLC_DATA_COLLECTOR_LOG_DIR"),
        help="runtime log directory",
    )
    parser.add_argument(
        "--run-dir",
        default=os.getenv("PLC_DATA_COLLECTOR_RUN_DIR"),
        help="runtime directory for PID and transient files",
    )
    parser.add_argument(
        "--pid-file",
        default=os.getenv("PLC_DATA_COLLECTOR_PID_FILE"),
        help="PID file path for service mode",
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("PLC_DATA_COLLECTOR_LOG_FILE"),
        help="JSON log file path for service mode",
    )
    parser.add_argument(
        "--api-host",
        default=None,
        help="API host, defaults to PLC_DATA_COLLECTOR_API_HOST or 127.0.0.1",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=None,
        help="API port, defaults to PLC_DATA_COLLECTOR_API_PORT or 8080",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the minimal PLC Data Collector bootstrap command."""
    resolved_argv = list(sys.argv[1:] if argv is None else argv)
    if resolved_argv and resolved_argv[0] == "licenses":
        print(json.dumps(load_license_report().to_dict(), sort_keys=True))
        return 0

    parser = build_parser()
    args = parser.parse_args(resolved_argv)

    if args.version:
        print(get_version())
        return 0

    configure_logging(args.log_level)
    logging.getLogger(__name__).info("PLC Data Collector bootstrap command started")
    if args.run_service:
        return run_service(
            ServicePaths.from_values(
                data_dir=args.data_dir,
                log_dir=args.log_dir,
                run_dir=args.run_dir,
                config_file=args.config,
                pid_file=args.pid_file,
                log_file=args.log_file,
            ),
            log_level=args.log_level,
            api_host=args.api_host,
            api_port=args.api_port,
        )

    if args.serve_api:
        import uvicorn

        from plc_data_collector.api import create_api_app

        uvicorn.run(
            create_api_app(),
            host=args.api_host or os.getenv("PLC_DATA_COLLECTOR_API_HOST", "127.0.0.1"),
            port=(
                args.api_port
                if args.api_port is not None
                else int(os.getenv("PLC_DATA_COLLECTOR_API_PORT", "8080"))
            ),
        )
        return 0

    print(json.dumps(build_health_status(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
