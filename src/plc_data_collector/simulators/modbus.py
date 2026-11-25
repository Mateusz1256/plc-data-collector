"""Modbus TCP simulator of a press and an energy meter, for demos and tests.

Run it with ``python -m plc_data_collector.simulators.modbus``. Addresses are
zero-based, as on the wire. Every documented block is one contiguous defined
range; addresses outside the ranges answer Modbus exception 02.

Unit 1, "press":

* ``hr:100`` float32 hydraulic pressure, bar (150 +- 20, sine)
* ``hr:102`` float32 oil temperature, degC (45 +- 5)
* ``hr:104`` uint32 cycle counter (+1 every 2 s)
* ``hr:106`` int16 ram position times 10 (use scale 0.1; 0-100 mm)
* ``hr:107`` status word: bit 0 running, bit 1 fault, bit 3 door closed
* ``hr:110`` float32 motor current, A, low word first
* ``hr:120`` string, length 8: recipe name ``RCP-0042``
* ``hr:100-127`` is defined; registers not listed above read 0
* ``co:0`` pump on, ``co:1`` alarm horn (defined: coils 0-1)
* ``di:0`` door closed, ``di:7`` guard closed (defined: inputs 0-7)

Unit 2, "meter" (defined: ``ir:0-3`` and ``ir:100-103``):

* ``ir:0`` float32 voltage L1, V (230 +- 2)
* ``ir:2`` float32 current L1, A
* ``ir:100`` uint64 energy, Wh, low word first

The empty areas of each unit keep one filler register at address 65535,
because a pymodbus device needs at least one entry per area.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import struct
import time
from collections.abc import Sequence

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from plc_data_collector.app.logging import configure_logging
from plc_data_collector.app.service import install_signal_handlers

LOGGER = logging.getLogger("plc_data_collector.simulators.modbus")

PRESS_UNIT = 1
METER_UNIT = 2
RECIPE_NAME = "RCP-0042"
PRESSURE_PERIOD_S = 45.0
TEMPERATURE_PERIOD_S = 120.0
RAM_PERIOD_S = 10.0
FAULT_PERIOD_S = 60.0
FAULT_DURATION_S = 5.0
_LAST_ADDRESS = 65535

# Keys are (unit, area, address); values are 16-bit words or bits.
RegisterKey = tuple[int, str, int]
RegisterValues = list[int] | list[bool]

_FUNCTION_CODES = {"co": 15, "di": 2, "hr": 16, "ir": 4}


def _float32_words(value: float, *, low_first: bool = False) -> list[int]:
    high, low = struct.unpack(">HH", struct.pack(">f", value))
    return [low, high] if low_first else [high, low]


def _string_words(text: str) -> list[int]:
    data = text.encode("ascii")
    if len(data) % 2:
        data += b"\x00"
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def simulated_registers(
    elapsed_s: float, rng: random.Random
) -> dict[RegisterKey, RegisterValues]:
    """Return the simulated device memory ``elapsed_s`` seconds after start."""
    pressure = (
        150.0
        + 20.0 * math.sin(2 * math.pi * elapsed_s / PRESSURE_PERIOD_S)
        + rng.uniform(-0.5, 0.5)
    )
    temperature = (
        45.0
        + 5.0 * math.sin(2 * math.pi * elapsed_s / TEMPERATURE_PERIOD_S)
        + rng.uniform(-0.5, 0.5)
    )
    cycles = int(elapsed_s // 2)
    ram = round(50.0 + 50.0 * math.sin(2 * math.pi * elapsed_s / RAM_PERIOD_S), 1)
    fault = elapsed_s % FAULT_PERIOD_S >= FAULT_PERIOD_S - FAULT_DURATION_S
    status = 0b1000 | (0b10 if fault else 0b01)
    current = 12.0 + 3.0 * math.sin(2 * math.pi * elapsed_s / PRESSURE_PERIOD_S)
    voltage = (
        230.0 + 2.0 * math.sin(2 * math.pi * elapsed_s / 30.0) + rng.uniform(-0.1, 0.1)
    )
    meter_current = 10.0 + 5.0 * math.sin(2 * math.pi * elapsed_s / 60.0)
    energy = 1_000_000 + int(elapsed_s * 5)
    return {
        (PRESS_UNIT, "hr", 100): _float32_words(pressure),
        (PRESS_UNIT, "hr", 102): _float32_words(temperature),
        (PRESS_UNIT, "hr", 104): [(cycles >> 16) & 0xFFFF, cycles & 0xFFFF],
        (PRESS_UNIT, "hr", 106): [round(ram * 10) & 0xFFFF],
        (PRESS_UNIT, "hr", 107): [status],
        (PRESS_UNIT, "hr", 110): _float32_words(current, low_first=True),
        (PRESS_UNIT, "hr", 120): _string_words(RECIPE_NAME),
        (PRESS_UNIT, "co", 0): [True, fault],
        (PRESS_UNIT, "di", 0): [True],
        (PRESS_UNIT, "di", 7): [True],
        (METER_UNIT, "ir", 0): _float32_words(voltage),
        (METER_UNIT, "ir", 2): _float32_words(meter_current),
        (METER_UNIT, "ir", 100): [
            (energy >> shift) & 0xFFFF for shift in (0, 16, 32, 48)
        ],
    }


def _bits(address: int, count: int) -> list[SimData]:
    return [
        SimData(address, count=count, values=False, datatype=DataType.BITS),
    ]


def _regs(address: int, count: int) -> list[SimData]:
    return [SimData(address, count=count, values=0, datatype=DataType.REGISTERS)]


def _filler(bits: bool) -> list[SimData]:
    return _bits(_LAST_ADDRESS, 1) if bits else _regs(_LAST_ADDRESS, 1)


def _devices() -> list[SimDevice]:
    press = SimDevice(
        PRESS_UNIT,
        simdata=(_bits(0, 2), _bits(0, 8), _regs(100, 28), _filler(False)),
    )
    meter = SimDevice(
        METER_UNIT,
        simdata=(
            _filler(True),
            _filler(True),
            _filler(False),
            [*_regs(0, 4), *_regs(100, 4)],
        ),
    )
    return [press, meter]


async def _apply(
    server: ModbusTcpServer, values: dict[RegisterKey, RegisterValues]
) -> None:
    for (unit, area, address), words in values.items():
        error = await server.context.async_setValues(
            unit, _FUNCTION_CODES[area], address, words
        )
        if error is not None:  # pragma: no cover - the map is defined at build time
            raise RuntimeError(f"Cannot set {area}:{address} of unit {unit}: {error}")


async def run_simulator(
    *,
    host: str = "0.0.0.0",
    port: int = 5020,
    update_interval_s: float = 1.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Serve the simulated press and meter until ``stop_event`` is set."""
    stop = asyncio.Event() if stop_event is None else stop_event
    rng = random.Random()
    server = ModbusTcpServer(_devices(), address=(host, port))
    await _apply(server, simulated_registers(0.0, rng))
    await server.serve_forever(background=True)
    try:
        LOGGER.info("simulator_started", extra={"host": host, "port": port})
        started = time.monotonic()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=update_interval_s)
            except TimeoutError:
                await _apply(
                    server, simulated_registers(time.monotonic() - started, rng)
                )
        LOGGER.info("simulator_stopping")
    finally:
        await server.shutdown()  # type: ignore[no-untyped-call]
    LOGGER.info("simulator_stopped")


def build_parser() -> argparse.ArgumentParser:
    """Build the simulator command line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m plc_data_collector.simulators.modbus",
        description="Modbus TCP simulator of a press and an energy meter.",
    )
    parser.add_argument("--host", default=os.environ.get("MODBUS_SIM_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MODBUS_SIM_PORT", "5020"))
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
    asyncio.run(_serve(args.host, args.port, args.update_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
