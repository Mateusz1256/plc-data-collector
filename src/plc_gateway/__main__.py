"""Command line entry point for PLC Gateway."""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence

from plc_gateway._version import get_version
from plc_gateway.app.health import build_health_status
from plc_gateway.app.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="plc-gateway")
    parser.add_argument(
        "--version",
        action="store_true",
        help="show application version and exit",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("PLC_GATEWAY_LOG_LEVEL", "INFO"),
        help="logging level, defaults to PLC_GATEWAY_LOG_LEVEL or INFO",
    )
    parser.add_argument(
        "--serve-api",
        action="store_true",
        help="serve the read-only health and runtime API",
    )
    parser.add_argument(
        "--api-host",
        default=os.getenv("PLC_GATEWAY_API_HOST", "127.0.0.1"),
        help="API host, defaults to localhost only",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=int(os.getenv("PLC_GATEWAY_API_PORT", "8080")),
        help="API port, defaults to PLC_GATEWAY_API_PORT or 8080",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the minimal PLC Gateway bootstrap command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(get_version())
        return 0

    configure_logging(args.log_level)
    logging.getLogger(__name__).info("PLC Gateway bootstrap command started")
    if args.serve_api:
        import uvicorn

        from plc_gateway.api import create_api_app

        uvicorn.run(create_api_app(), host=args.api_host, port=args.api_port)
        return 0

    print(json.dumps(build_health_status(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
