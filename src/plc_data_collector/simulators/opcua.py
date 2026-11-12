"""OPC UA simulator of a small production line, used for demos and tests.

Run it with ``python -m plc_data_collector.simulators.opcua``. It serves four
nodes in the ``urn:plc-data-collector:sim`` namespace without security and
with anonymous access.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass

from asyncua import ua
from asyncua.common.node import Node
from asyncua.server.server import Server

from plc_data_collector.app.logging import configure_logging
from plc_data_collector.app.service import install_signal_handlers

LOGGER = logging.getLogger("plc_data_collector.simulators.opcua")

NAMESPACE_URI = "urn:plc-data-collector:sim"
ENDPOINT_PATH = "/plc-data-collector/sim/"
# A fresh server registers the namespace right after its two built-in ones.
EXPECTED_NAMESPACE_INDEX = 2

TEMPERATURE_PERIOD_S = 120.0
PRESSURE_PERIOD_S = 45.0
# (state, duration in seconds); the cycle repeats every 55 s.
STATE_CYCLE: tuple[tuple[str, float], ...] = (
    ("RUNNING", 40.0),
    ("IDLE", 10.0),
    ("FAULT", 5.0),
)
CYCLE_PERIOD_S = sum(duration for _, duration in STATE_CYCLE)


@dataclass(frozen=True, slots=True)
class SimulatedValues:
    """One snapshot of the simulated line."""

    temperature: float
    pressure: float
    machine_state: str


def simulated_values(elapsed_s: float, rng: random.Random) -> SimulatedValues:
    """Return the simulated line values ``elapsed_s`` seconds after start."""
    temperature = (
        70.0
        + 10.0 * math.sin(2 * math.pi * elapsed_s / TEMPERATURE_PERIOD_S)
        + rng.uniform(-0.5, 0.5)
    )
    pressure = (
        5.0
        + 1.0 * math.sin(2 * math.pi * elapsed_s / PRESSURE_PERIOD_S)
        + rng.uniform(-0.05, 0.05)
    )
    return SimulatedValues(temperature, pressure, _machine_state(elapsed_s))


def _machine_state(elapsed_s: float) -> str:
    position = elapsed_s % CYCLE_PERIOD_S
    for state, duration in STATE_CYCLE:
        if position < duration:
            return state
        position -= duration
    return STATE_CYCLE[0][0]  # pragma: no cover - float rounding guard


async def run_simulator(
    *,
    host: str = "0.0.0.0",
    port: int = 4840,
    update_interval_s: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Serve the simulated line until ``stop_event`` is set."""
    stop = asyncio.Event() if stop_event is None else stop_event
    endpoint = f"opc.tcp://{host}:{port}{ENDPOINT_PATH}"

    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    server.set_server_name("PLC Data Collector simulator")
    # Anonymous access without security: no certificates are needed, and
    # offering only the anonymous token avoids asyncua's warnings about
    # unsigned certificates and plaintext passwords.
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    server.set_identity_tokens([ua.AnonymousIdentityToken])
    index = await server.register_namespace(NAMESPACE_URI)
    if index != EXPECTED_NAMESPACE_INDEX:
        msg = f"Namespace index is {index}, expected {EXPECTED_NAMESPACE_INDEX}."
        raise RuntimeError(msg)

    line = await server.nodes.objects.add_object(index, "Line1")
    initial = simulated_values(0.0, random.Random())
    temperature = await _add_variable(
        server, line, "Temperature", initial.temperature, ua.VariantType.Double
    )
    pressure = await _add_variable(
        server, line, "Pressure", initial.pressure, ua.VariantType.Double
    )
    part_count = await _add_variable(server, line, "PartCount", 0, ua.VariantType.Int64)
    state = await _add_variable(
        server, line, "MachineState", initial.machine_state, ua.VariantType.String
    )

    async with server:
        LOGGER.info("simulator_started", extra={"endpoint": endpoint})
        rng = random.Random()
        started = time.monotonic()
        count = 0
        while not stop.is_set():
            values = simulated_values(time.monotonic() - started, rng)
            if values.machine_state == "RUNNING":
                count += 1
            await temperature.write_value(
                ua.Variant(values.temperature, ua.VariantType.Double)
            )
            await pressure.write_value(
                ua.Variant(values.pressure, ua.VariantType.Double)
            )
            await part_count.write_value(ua.Variant(count, ua.VariantType.Int64))
            await state.write_value(
                ua.Variant(values.machine_state, ua.VariantType.String)
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=update_interval_s)
            except TimeoutError:
                continue
        LOGGER.info("simulator_stopping")
    LOGGER.info("simulator_stopped")


async def _add_variable(
    server: Server,
    parent: Node,
    name: str,
    value: object,
    variant_type: ua.VariantType,
) -> Node:
    return await parent.add_variable(
        ua.NodeId.from_string(f"ns={EXPECTED_NAMESPACE_INDEX};s=Line1.{name}"),
        name,
        ua.Variant(value, variant_type),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the simulator command line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m plc_data_collector.simulators.opcua",
        description="OPC UA simulator of a small production line.",
    )
    parser.add_argument("--host", default=os.environ.get("OPCUA_SIM_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("OPCUA_SIM_PORT", "4840"))
    )
    parser.add_argument("--update-interval", type=float, default=1.0)
    return parser


async def _serve(host: str, port: int, update_interval_s: float) -> None:
    stop_event = asyncio.Event()
    callbacks = install_signal_handlers(stop_event)
    try:
        await run_simulator(
            host=host,
            port=port,
            update_interval_s=update_interval_s,
            stop_event=stop_event,
        )
    finally:
        for callback in callbacks:
            callback()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the simulator until SIGTERM or SIGINT."""
    args = build_parser().parse_args(argv)
    configure_logging()
    # asyncua logs every address-space node at INFO while loading.
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    asyncio.run(_serve(args.host, args.port, args.update_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
