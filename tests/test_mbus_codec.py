"""M-Bus frame and record codec: byte-level fixtures, exact values."""

from __future__ import annotations

from decimal import Decimal

import pytest
from mbus_fixtures import fixture_names, read_frame, source_of

from plc_data_collector.protocols.mbus_codec import (
    ACK,
    ADDRESS_BROADCAST,
    ADDRESS_SECONDARY,
    Ack,
    FrameReader,
    GarbledFrame,
    LongFrame,
    MBusRecord,
    ShortFrame,
    Telegram,
    parse_rsp_ud,
    power_low,
    req_ud2,
    select_secondary,
    snd_nke,
    status_problems,
)
from plc_data_collector.protocols.mbus_tags import (
    UNIT_FAMILIES,
    Function,
    Quantity,
    RecordKey,
    convert,
)


def _frame(name: str) -> LongFrame:
    reader = FrameReader()
    reader.feed(read_frame(name))
    frame = reader.next_frame()
    assert isinstance(frame, LongFrame)
    assert reader.next_frame() is None
    return frame


def _telegram(name: str) -> Telegram:
    return parse_rsp_ud(_frame(name))


def _long(payload: bytes, ci: int = 0x72, control: int = 0x08) -> bytes:
    body = bytes((control, 0x01, ci)) + payload
    return (
        bytes((0x68, len(body), len(body), 0x68))
        + body
        + bytes((sum(body) & 0xFF, 0x16))
    )


_HEADER = bytes.fromhex("78563412 8340 01 04 10 00 0000")


def _records(records_hex: str) -> tuple[MBusRecord, ...]:
    frame = LongFrame(
        control=0x08,
        address=1,
        ci=0x72,
        payload=_HEADER + bytes.fromhex(records_hex),
    )
    return parse_rsp_ud(frame).records


def _one(record_hex: str) -> MBusRecord:
    (record,) = _records(record_hex)
    return record


def _key(
    quantity: Quantity,
    *,
    storage: int = 0,
    tariff: int = 0,
    subunit: int = 0,
    function: Function = Function.INSTANTANEOUS,
    qualifiers: tuple[str, ...] = (),
) -> RecordKey:
    return RecordKey(quantity, storage, tariff, subunit, function, qualifiers)


# Fixtures


def test_every_fixture_names_its_source() -> None:
    allowed = (
        "pyMeterBus 0.8.5 test frame",
        "hand-assembled, checked by the pyMeterBus oracle",
        '"The M-Bus: A Documentation"',
    )
    names = fixture_names()
    assert names
    for name in names:
        assert source_of(name).startswith(allowed), name


# Frame builders


def test_constants() -> None:
    assert (ACK, ADDRESS_SECONDARY, ADDRESS_BROADCAST) == (0xE5, 253, 255)


def test_snd_nke_is_a_short_frame() -> None:
    assert snd_nke(5) == bytes.fromhex("10 40 05 45 16")
    assert snd_nke(ADDRESS_BROADCAST) == bytes.fromhex("10 40 FF 3F 16")


def test_req_ud2_toggles_fcb() -> None:
    assert req_ud2(1, fcb=False) == bytes.fromhex("10 5B 01 5C 16")
    assert req_ud2(1, fcb=True) == bytes.fromhex("10 7B 01 7C 16")


def test_select_secondary_is_snd_ud_ci_52() -> None:
    pattern = bytes.fromhex("78563412 FFFF FF FF")
    frame = select_secondary(pattern)
    body = bytes((0x53, ADDRESS_SECONDARY, 0x52)) + pattern
    assert frame == bytes((0x68, 11, 11, 0x68)) + body + bytes((sum(body) & 0xFF, 0x16))


@pytest.mark.parametrize("address", [-1, 256])
def test_builders_refuse_bad_addresses(address: int) -> None:
    with pytest.raises(ValueError):
        snd_nke(address)
    with pytest.raises(ValueError):
        req_ud2(address, fcb=False)


def test_select_secondary_needs_eight_bytes() -> None:
    with pytest.raises(ValueError):
        select_secondary(b"\x00" * 7)


# Frame reader


def test_reads_ack_short_and_long_frames() -> None:
    reader = FrameReader()
    reader.feed(read_frame("pymeterbus_ack.hex"))
    reader.feed(read_frame("pymeterbus_short_frame.hex"))
    reader.feed(read_frame("mbus_doc_rsp_ud.hex"))
    assert reader.next_frame() == Ack()
    assert reader.next_frame() == ShortFrame(control=0x08, address=0x0B)
    frame = reader.next_frame()
    assert isinstance(frame, LongFrame)
    assert (frame.control, frame.address, frame.ci) == (0x08, 0x02, 0x72)
    assert len(frame.payload) == 28
    assert reader.next_frame() is None


def test_frame_fed_one_byte_at_a_time() -> None:
    data = read_frame("pymeterbus_hyd.hex")
    reader = FrameReader()
    for index, byte in enumerate(data):
        assert reader.next_frame() is None, index
        reader.feed(bytes((byte,)))
    frame = reader.next_frame()
    assert isinstance(frame, LongFrame)
    assert frame.address == 0x4E


def test_two_frames_in_one_chunk() -> None:
    reader = FrameReader()
    reader.feed(read_frame("mbus_doc_rsp_ud.hex") + read_frame("pymeterbus_hyd.hex"))
    first, second = reader.next_frame(), reader.next_frame()
    assert isinstance(first, LongFrame)
    assert isinstance(second, LongFrame)
    assert (first.address, second.address) == (0x02, 0x4E)
    assert reader.next_frame() is None


def test_incomplete_frame_waits() -> None:
    reader = FrameReader()
    reader.feed(read_frame("mbus_doc_rsp_ud.hex")[:-1])
    assert reader.next_frame() is None


def _garbled(data: bytes) -> None:
    reader = FrameReader()
    reader.feed(data)
    with pytest.raises(GarbledFrame):
        reader.next_frame()


def test_bad_checksum_is_garbled() -> None:
    data = bytearray(read_frame("mbus_doc_rsp_ud.hex"))
    data[-2] ^= 0x01
    _garbled(bytes(data))
    _garbled(bytes.fromhex("10 5B 01 5D 16"))


def test_length_mismatch_is_garbled() -> None:
    data = bytearray(read_frame("mbus_doc_rsp_ud.hex"))
    data[2] = data[1] + 1
    _garbled(bytes(data))


def test_too_short_long_frame_is_garbled() -> None:
    _garbled(bytes.fromhex("68 02 02 68 08 01 09 16"))


def test_missing_stop_byte_is_garbled() -> None:
    data = bytearray(read_frame("mbus_doc_rsp_ud.hex"))
    data[-1] = 0x00
    _garbled(bytes(data))
    _garbled(bytes.fromhex("10 5B 01 5C 00"))


def test_bad_start_is_garbled() -> None:
    _garbled(b"\x00")
    data = bytearray(read_frame("mbus_doc_rsp_ud.hex"))
    data[3] = 0x69
    _garbled(bytes(data))


def test_reader_is_empty_after_garbage() -> None:
    reader = FrameReader()
    reader.feed(b"\x42\xe5")
    with pytest.raises(GarbledFrame):
        reader.next_frame()
    assert reader.next_frame() is None


# Fixed header and status


def test_header_of_the_documentation_example() -> None:
    telegram = _telegram("mbus_doc_rsp_ud.hex")
    header = telegram.header
    assert header.id_digits == "12345678"
    assert header.manufacturer_code == 0x4024
    assert header.manufacturer == "PAD"
    assert (header.version, header.medium, header.medium_name) == (1, 7, "water")
    assert (header.access_no, header.status) == (0x55, 0)
    assert telegram.more_follows is False


def test_records_of_the_documentation_example() -> None:
    volume, flow, energy = _telegram("mbus_doc_rsp_ud.hex").records
    assert volume.key == _key(Quantity.VOLUME)
    assert (volume.unit, volume.exact_value) == ("L", Decimal(12565))
    assert flow.key == _key(Quantity.VOLUME_FLOW, storage=5, function=Function.MAXIMUM)
    assert (flow.unit, flow.exact_value) == ("L/h", Decimal(113))
    assert energy.key == _key(Quantity.ENERGY, tariff=2, subunit=1)
    assert (energy.unit, energy.exact_value) == ("Wh", Decimal(218370))
    assert energy.raw == bytes.fromhex("8B 60 04 37 18 02")
    assert energy.family is UNIT_FAMILIES["energy_wh"]


def test_ci_76_is_msb_first() -> None:
    telegram = _telegram("pymeterbus_unknown_pl.hex")
    header = telegram.header
    assert (header.id_digits, header.manufacturer_code) == ("15531111", 0)
    assert (header.version, header.medium) == (0x52, 0x04)
    records = telegram.records
    assert records[0].key is None
    assert "date" in (records[0].unsupported_reason or "")
    assert records[1].key == _key(Quantity.ENERGY, function=Function.ERROR)
    assert (records[1].unit, records[1].exact_value) == ("MJ", Decimal(5853400))
    assert records[10].key == _key(Quantity.FLOW_TEMPERATURE)
    assert (records[10].unit, records[10].exact_value) == ("C", Decimal("42.5"))
    assert records[7].key == _key(Quantity.VOLUME, subunit=1)


def test_other_ci_is_garbled() -> None:
    with pytest.raises(GarbledFrame):
        parse_rsp_ud(LongFrame(control=0x08, address=1, ci=0x78, payload=b""))


def test_short_header_is_garbled() -> None:
    with pytest.raises(GarbledFrame):
        parse_rsp_ud(LongFrame(control=0x08, address=1, ci=0x72, payload=_HEADER[:-1]))


def test_record_running_past_the_end_is_garbled() -> None:
    with pytest.raises(GarbledFrame):
        _records("04 03 01 02 03")
    with pytest.raises(GarbledFrame):
        _records("84")


@pytest.mark.parametrize(
    ("status", "problems"),
    [
        (0x00, ()),
        (0x01, ("application busy",)),
        (0x02, ("application error",)),
        (0x04, ()),
        (0x08, ("permanent error",)),
        (0x10, ("temporary error",)),
        (0x1A, ("application error", "permanent error", "temporary error")),
        (0xE0, ()),
    ],
)
def test_status_problems(status: int, problems: tuple[str, ...]) -> None:
    assert status_problems(status) == problems


def test_power_low_is_separate() -> None:
    assert power_low(0x04) is True
    assert power_low(0x1B) is False


def test_header_status_is_reported() -> None:
    payload = _HEADER[:9] + b"\x04" + _HEADER[10:]
    frame = LongFrame(control=0x08, address=1, ci=0x72, payload=payload)
    assert parse_rsp_ud(frame).header.status == 0x04


# Data field encodings


def test_every_encoding() -> None:
    values = [r.exact_value for r in _telegram("hand_encodings.hex").records]
    assert values == [
        Decimal(-2),
        Decimal(4660),
        Decimal(-1),
        Decimal(-2147483648),
        Decimal(6618611909121),
        Decimal(9223372036854775807),
        Decimal("12.5"),
        Decimal("17.4"),
        Decimal(99),
        Decimal(-234),
        Decimal(123456),
        Decimal(12345678),
        Decimal(999999999999),
    ]


def test_bcd12_and_int64_max_are_exact() -> None:
    records = _telegram("hand_encodings.hex").records
    int64, bcd12 = records[5], records[12]
    assert (int64.unit, int64.exact_value) == ("Wh", Decimal("9223372036854775807"))
    assert str(int64.exact_value) == "9223372036854775807"
    assert (bcd12.unit, bcd12.exact_value) == ("Wh", Decimal("999999999999"))
    assert str(bcd12.exact_value) == "999999999999"


def test_bcd_volume_is_exact() -> None:
    record = _telegram("hand_encodings.hex").records[11]
    assert record.key == _key(Quantity.VOLUME)
    assert (record.unit, record.exact_value) == ("L", Decimal(12345678))
    assert record.family is not None
    assert isinstance(record.exact_value, Decimal)
    in_m3 = convert(record.exact_value, "L", "m3", record.family)
    assert in_m3 == Decimal("12345.678")
    assert repr(float(in_m3)) == "12345.678"


def test_values_are_scaled_exactly() -> None:
    record = _one("0C 10 78 56 34 12")
    # VIF 0x10: m3 x 10^-6 -> L x 10^-3
    assert (record.unit, record.exact_value) == ("L", Decimal("12345.678"))
    assert str(record.exact_value) == "12345.678"


def test_value_errors() -> None:
    bcd_a, bcd_c, nan, inf, good = _telegram("hand_value_errors.hex").records
    for record in (bcd_a, bcd_c):
        assert record.key == _key(Quantity.ENERGY)
        assert record.exact_value is None
        assert record.value_error == "mbus_value_invalid"
        assert record.unsupported_reason
    for record in (nan, inf):
        assert record.exact_value is None
        assert record.value_error == "mbus_value_not_finite"
        assert record.unsupported_reason
    assert (good.exact_value, good.value_error, good.unsupported_reason) == (
        Decimal(5),
        None,
        None,
    )


def test_bcd_minus_only_in_the_top_digit() -> None:
    assert _one("0A 03 34 F2").exact_value == Decimal(-234)
    assert _one("0A 03 F4 12").value_error == "mbus_value_invalid"


@pytest.mark.parametrize(
    ("record_hex", "reason"),
    [
        ("00 03", "no data"),
        ("08 03", "selection"),
        ("0D 03 02 41 42", "variable"),
    ],
)
def test_unsupported_encodings(record_hex: str, reason: str) -> None:
    record = _one(record_hex)
    assert record.key is None
    assert record.exact_value is None
    assert record.unsupported_reason is not None
    assert reason in record.unsupported_reason
    assert record.raw == bytes.fromhex(record_hex)


# Quantities and units

_QUANTITIES_1 = [
    (Quantity.ENERGY, "Wh", Decimal(1000)),
    (Quantity.ENERGY, "kWh", Decimal(12345)),
    (Quantity.ENERGY, "Wh", Decimal("12.34")),
    (Quantity.ENERGY, "MJ", Decimal(21)),
    (Quantity.ENERGY, "kWh", Decimal(12300)),
    (Quantity.ENERGY, "MWh", Decimal(123)),
    (Quantity.ENERGY, "MJ", Decimal(4500)),
    (Quantity.ENERGY, "GJ", Decimal(45)),
    (Quantity.VOLUME, "L", Decimal(12345678)),
    (Quantity.VOLUME, "m3", Decimal(10)),
    (Quantity.VOLUME, "L", Decimal(500)),
    (Quantity.MASS, "kg", Decimal(100)),
    (Quantity.MASS, "t", Decimal(7)),
    (Quantity.ON_TIME, "s", 3600),
    (Quantity.ON_TIME, "h", 24),
    (Quantity.OPERATING_TIME, "min", 60),
    (Quantity.OPERATING_TIME, "d", 366),
    (Quantity.POWER, "W", Decimal(300)),
    (Quantity.POWER, "kW", Decimal(15)),
    (Quantity.POWER, "kJ/h", Decimal(77)),
]

_QUANTITIES_2 = [
    (Quantity.VOLUME_FLOW, "L/h", Decimal(113)),
    (Quantity.VOLUME_FLOW, "m3/h", Decimal(2)),
    (Quantity.VOLUME_FLOW, "m3/min", Decimal(3)),
    (Quantity.VOLUME_FLOW, "m3/s", Decimal("0.12")),
    (Quantity.MASS_FLOW, "kg/h", Decimal(600)),
    (Quantity.MASS_FLOW, "t/h", Decimal(4)),
    (Quantity.FLOW_TEMPERATURE, "C", Decimal("73.98")),
    (Quantity.RETURN_TEMPERATURE, "C", Decimal("44.58")),
    (Quantity.TEMPERATURE_DIFFERENCE, "K", Decimal("44.0")),
    (Quantity.EXTERNAL_TEMPERATURE, "C", Decimal("-10.0")),
    (Quantity.PRESSURE, "bar", Decimal("3.00")),
    (Quantity.HCA_UNITS, "", Decimal(1337)),
    (Quantity.VOLTAGE, "V", Decimal("229.8")),
    (Quantity.VOLTAGE, "kV", Decimal(11)),
    (Quantity.CURRENT, "A", Decimal("1.000")),
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("hand_quantities_1.hex", _QUANTITIES_1),
        ("hand_quantities_2.hex", _QUANTITIES_2),
    ],
)
def test_every_quantity_row(
    name: str, expected: list[tuple[Quantity, str, Decimal | int]]
) -> None:
    records = _telegram(name).records
    got = [(r.key.quantity if r.key else None, r.unit, r.exact_value) for r in records]
    assert got == expected
    for record, (quantity, unit, value) in zip(records, expected, strict=True):
        assert record.key == _key(quantity)
        assert record.family is not None
        assert unit in record.family.factors
        assert type(record.exact_value) is type(value)
        assert record.unsupported_reason is None
        assert record.value_error is None


def test_durations_are_integers() -> None:
    records = _telegram("hand_quantities_1.hex").records
    durations = [r for r in records if r.unit in {"s", "min", "h", "d"}]
    assert len(durations) == 4
    assert all(type(r.exact_value) is int for r in durations)
    assert all(r.family is UNIT_FAMILIES["duration"] for r in durations)


def test_duration_multiplier_must_give_whole_units() -> None:
    whole = _one("04 A2 7D 02 00 00 00")  # hours x 10^3
    assert (whole.unit, whole.exact_value) == ("h", 2000)
    assert type(whole.exact_value) is int
    fraction = _one("04 A0 73 05 00 00 00")  # seconds x 10^-3
    assert fraction.key is None
    assert "fraction" in (fraction.unsupported_reason or "")


def test_wh_and_j_are_separate_families() -> None:
    records = _telegram("hand_quantities_1.hex").records
    assert records[1].family is UNIT_FAMILIES["energy_wh"]
    assert records[7].family is UNIT_FAMILIES["energy_j"]


# DIFE, function, qualifiers, VIFEs


def test_storage_tariff_subunit_and_function() -> None:
    records = _telegram("hand_keys.hex").records
    assert records[0].key == _key(Quantity.ENERGY, tariff=1)
    assert records[1].key == _key(Quantity.ENERGY, storage=323, tariff=12, subunit=2)
    assert records[2].key == _key(Quantity.ENERGY, function=Function.MAXIMUM)
    assert records[3].key == _key(Quantity.ENERGY, function=Function.MINIMUM)
    assert records[4].key == _key(Quantity.ENERGY, function=Function.ERROR)


def test_hydrometer_subunits_over_two_difes() -> None:
    records = _telegram("pymeterbus_hyd.hex").records
    assert records[4].key == _key(Quantity.VOLUME, subunit=1)
    assert records[5].key == _key(Quantity.VOLUME, subunit=2)
    assert (records[5].unit, records[5].exact_value) == ("L", Decimal(12))


def test_fill_bytes_are_skipped() -> None:
    records = _telegram("hand_keys.hex").records
    assert records[5].exact_value == Decimal(6)
    assert all(not r.raw.startswith(b"\x2f") for r in records)
    weptech = _telegram("pymeterbus_weptech.hex").records
    assert len(weptech) == 6


def test_qualifiers() -> None:
    records = _telegram("hand_keys.hex").records
    forward, backward, per_hour = records[5], records[6], records[7]
    assert forward.key == _key(Quantity.ENERGY, qualifiers=("forward",))
    assert backward.key == _key(Quantity.ENERGY, qualifiers=("backward",))
    assert per_hour.key == _key(Quantity.VOLUME, qualifiers=("per_hour",))
    assert (per_hour.unit, per_hour.exact_value) == ("L", Decimal(8))


def test_qualifiers_combine_sorted() -> None:
    record = _one("04 83 BC 22 07 00 00 00")
    assert record.key == _key(Quantity.ENERGY, qualifiers=("backward", "per_hour"))


def test_third_party_forward_and_backward() -> None:
    records = _telegram("pymeterbus_variable_data_record.hex").records
    forward, backward = records[-2], records[-1]
    assert forward.key == _key(Quantity.ENERGY, qualifiers=("forward",))
    assert backward.key == _key(Quantity.ENERGY, qualifiers=("backward",))
    assert forward.exact_value == Decimal(1234567)


def test_multiplier_vifes_change_the_exponent() -> None:
    records = _telegram("hand_keys.hex").records
    assert (records[8].unit, records[8].exact_value) == ("kWh", Decimal(9))
    assert (records[9].unit, records[9].exact_value) == ("Wh", Decimal(10))
    assert records[8].key == _key(Quantity.ENERGY)


def test_unsupported_records_carry_a_reason() -> None:
    records = _telegram("hand_keys.hex").records
    unknown_vife, date, error_flags, no_data, variable = records[10:]
    for record in (unknown_vife, date, error_flags, no_data, variable):
        assert record.key is None
        assert record.unit is None
        assert record.family is None
        assert record.exact_value is None
        assert record.value_error is None
        assert record.unsupported_reason
        assert record.description
    assert "VIFE" in (unknown_vife.unsupported_reason or "")
    assert "date" in (date.unsupported_reason or "")
    assert date.raw == bytes.fromhex("02 6C 21 2B")


def test_more_records_follow() -> None:
    assert _telegram("hand_keys.hex").more_follows is True
    assert _telegram("pymeterbus_elvaco_cma10.hex").more_follows is True
    assert _telegram("hand_encodings.hex").more_follows is False


def test_plain_text_vif_is_skipped_as_unsupported() -> None:
    records = _telegram("pymeterbus_elvaco_cma10.hex").records
    assert len(records) == 12
    assert records[1].key is None
    assert records[4].key == _key(Quantity.EXTERNAL_TEMPERATURE)
    assert records[4].exact_value == Decimal("22.56")
    assert records[9].key == _key(Quantity.EXTERNAL_TEMPERATURE, storage=2)


def test_description_names_the_record() -> None:
    record = _telegram("mbus_doc_rsp_ud.hex").records[2]
    assert "energy" in record.description
    assert "tariff 2" in record.description
