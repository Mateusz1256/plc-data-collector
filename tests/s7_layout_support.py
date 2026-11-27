"""The simulator's hand-written DB1 layout table, shared by the S7 tests.

``simulators/s7.py`` documents DB1 in its module docstring between
``<!-- layout:begin -->`` and ``<!-- layout:end -->``. The simulator test reads
those offsets with the S7 driver; the TIA import test checks that the demo
source lays out to the same offsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from plc_data_collector.simulators import s7 as simulator

_STRING_TYPE = re.compile(r"string\[(\d+)\]")


@dataclass(frozen=True)
class Row:
    name: str
    data_type: str
    offset: str
    length: int | None

    @property
    def address(self) -> str:
        return f"DB1.{self.offset}"


def documented_rows() -> list[Row]:
    """Parse the layout table of the simulator docstring (format ruling R1).

    The table is a Markdown table with the header ``| Name | Type | Offset |``
    followed by ``| --- | --- | --- |``; Type is an S7 data type string
    (``string[N]`` for strings), Offset is ``byte`` or ``byte.bit``.
    """
    doc = simulator.__doc__ or ""
    lines = doc.splitlines()
    start = lines.index("| Name | Type | Offset |")
    assert lines[start + 1] == "| --- | --- | --- |"
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        name, kind, offset = (cell.strip() for cell in line.strip("|").split("|"))
        match = _STRING_TYPE.fullmatch(kind)
        rows.append(
            Row(
                name,
                "string" if match else kind,
                offset,
                int(match.group(1)) if match else None,
            )
        )
    return rows
