"""Tests for the Panel's "Import from TIA Portal" page: upload, preview, save."""

from __future__ import annotations

import html
import logging
import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost
from s7_layout_support import documented_rows
from test_tia_tag_table import table

from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.panel.tia_import import (
    UploadedFile,
    UploadStore,
)
from plc_data_collector.tia.layout import OPTIMIZED_MESSAGE, UNKNOWN_ACCESS_MESSAGE

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "deploy" / "demo" / "s7"
S7 = ConnectionConfig(
    id="press3", name="Press 3", protocol="s7", endpoint="tcp://press3-plc:102"
)
MODBUS = ConnectionConfig(
    id="press2",
    name="Press 2",
    protocol="modbus",
    endpoint="tcp://press2-plc:502",
)
FORM_URL = "/panel/connections/press3/tags/import-tia"
EXPIRED = "This upload expired. Upload the files again."
SIZE_LINE = (
    "Calculated size: {} bytes. Compare a few offsets with the Offset column "
    "in TIA before saving."
)


def db_source(
    members: str,
    *,
    name: str = "Data",
    access: str | None = "FALSE",
    number: int | None = None,
) -> bytes:
    """A DB source; with ``number`` it is declared as ``DATA_BLOCK DB<n>``."""
    header = f"DB{number}" if number is not None else f'"{name}"'
    attribute = "" if access is None else f"{{ S7_Optimized_Access := '{access}' }}\n"
    return (
        f"DATA_BLOCK {header}\n{attribute}VERSION : 0.1\nNON_RETAIN\n"
        f"   STRUCT\n{members}   END_STRUCT;\nBEGIN\nEND_DATA_BLOCK\n"
    ).encode()


def press_files() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        (
            "files",
            (name, (DEMO / name).read_bytes(), "application/octet-stream"),
        )
        for name in ("PressData.db", "PressTypes.udt")
    ]


def one_file(name: str, data: bytes) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (name, data, "application/octet-stream"))]


def _stores(host: FakeHost) -> Stores:
    assert host.stores is not None
    return host.stores


def _seed(host: FakeHost) -> Stores:
    stores = _stores(host)
    stores.config.upsert_connection(S7)
    stores.config.upsert_tag_group(
        TagGroupConfig(
            id="press_fast", connection_id="press3", interval_ms=500, timeout_ms=400
        )
    )
    return stores


def upload(
    client: TestClient,
    files: list[tuple[str, tuple[str, bytes, str]]],
) -> tuple[int, str, str | None]:
    """Upload ``files``; return the status, the page and the token, if any."""
    response = client.post(FORM_URL, files=files, data={"tag_group_id": "press_fast"})
    match = re.search(rf'action="{FORM_URL}/([A-Za-z0-9_-]+)"', response.text)
    return response.status_code, response.text, match.group(1) if match else None


def submit(
    client: TestClient,
    token: str,
    fields: list[tuple[str, str]],
    action: str = "preview",
) -> tuple[int, str]:
    response = client.post(
        f"{FORM_URL}/{token}",
        data=form([("tag_group_id", "press_fast"), ("action", action), *fields]),
    )
    return response.status_code, response.text


def form(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Form fields with repeated names, as httpx takes them."""
    data: dict[str, list[str]] = {}
    for name, value in pairs:
        data.setdefault(name, []).append(value)
    return data


def rows(page: str) -> dict[str, list[str]]:
    """Candidate rows by key: the text of each cell after the checkbox."""
    found: dict[str, list[str]] = {}
    for match in re.finditer(r'<tr data-key="([^"]*)"[^>]*>(.*?)</tr>', page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", match.group(2), re.S)
        found[html.unescape(match.group(1))] = [
            html.unescape(re.sub(r"<[^>]+>", " ", cell)).strip() for cell in cells
        ]
    return found


def text(page: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))


def upload_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / "data" / "tia-uploads"
    return sorted(root.iterdir()) if root.exists() else []


KEY = "PressData.db#PressData#"
# Columns after the checkbox: name, path, address, type, offset, comment, ID,
# problem.
NAME, PATH, ADDRESS, TYPE, OFFSET, COMMENT, TAG_ID, PROBLEM = range(1, 9)


# Entry points


def test_tags_list_has_import_button_for_s7_only(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_connection(MODBUS)
    stores.config.upsert_tag_group(
        TagGroupConfig(
            id="press2_fast", connection_id="press2", interval_ms=500, timeout_ms=400
        )
    )

    s7_page = signed_in_client.get("/panel/connections/press3").text
    modbus_page = signed_in_client.get("/panel/connections/press2").text

    assert f'href="{FORM_URL}"' in s7_page
    assert "Import from TIA Portal" in s7_page
    assert "import-tia" not in modbus_page
    missing = signed_in_client.get("/panel/connections/press2/tags/import-tia")
    assert missing.status_code == 404


def test_upload_form(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    _seed(fake_host)

    page = signed_in_client.get(FORM_URL).text

    assert 'enctype="multipart/form-data"' in page
    assert 'type="file"' in page and "multiple" in page
    assert 'accept=".db,.udt,.xlsx"' in page
    assert 'name="tag_group_id"' in page


# Preview


def test_press_data_preview_matches_simulator_layout(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    status, first, token = upload(signed_in_client, press_files())

    assert status == 200, first
    assert token is not None
    # The source names the DB only, so the number is asked.
    assert 'name="db_number_0" value=""' in first
    assert "Enter the DB number" in text(first)
    status, page = submit(signed_in_client, token, [("db_number_0", "1")])
    assert status == 200, page
    shown = rows(page)
    expected = documented_rows()
    assert [key.removeprefix(KEY) for key in shown] == [row.name for row in expected]
    for row in expected:
        cells = shown[KEY + row.name]
        assert cells[PATH] == row.name
        assert cells[OFFSET] == row.offset
        assert cells[TYPE] == (
            f"string[{row.length}]" if row.data_type == "string" else row.data_type
        )
        assert cells[PROBLEM] == ""
    assert shown[KEY + "Pressure"][ADDRESS] == "DB1.DBD0"
    assert shown[KEY + "Status.Running"][ADDRESS] == "DB1.DBX4.0"
    assert shown[KEY + "Pressure"][COMMENT] == "Hydraulikdruck in bar"
    assert SIZE_LINE.format(68) in text(page)


def test_save_five_rows_creates_tags_in_one_revision(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    stores = _seed(fake_host)
    _, _, token = upload(signed_in_client, press_files())
    assert token is not None
    assert len(upload_dirs(tmp_path)) == 1
    picked = ["Pressure", "Status.Running", "Temps[2]", "Recipe", "Motor.Speed"]
    before = stores.config.revision()

    response = signed_in_client.post(
        f"{FORM_URL}/{token}",
        data=form(
            [
                ("tag_group_id", "press_fast"),
                ("db_number_0", "1"),
                ("action", "save"),
                *[("select", KEY + path) for path in picked],
            ]
        ),
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/panel/connections/press3"
    assert stores.config.revision() == before + 1
    tags = {
        tag.name: tag
        for tag in (stores.config.get_tag(i) for i in stores.config.all_tag_ids())
        if tag is not None
    }
    assert set(tags) == set(picked)
    pressure = tags["Pressure"]
    assert pressure.id == "press3_pressure"
    assert pressure.tag_group_id == "press_fast"
    assert pressure.address == "DB1.0"
    assert dict(pressure.protocol_options) == {"data_type": "real"}
    assert pressure.value_type is ValueType.NUMERIC
    assert dict(pressure.metadata) == {"description": "Hydraulikdruck in bar"}
    running = tags["Status.Running"]
    assert (running.address, running.value_type) == ("DB1.4.0", ValueType.BOOLEAN)
    assert tags["Temps[2]"].address == "DB1.14"
    assert tags["Temps[2]"].id == "press3_temps_2"
    recipe = tags["Recipe"]
    assert recipe.address == "DB1.36"
    assert dict(recipe.protocol_options) == {"data_type": "string", "length": 20}
    assert recipe.value_type is ValueType.TEXT
    speed = tags["Motor.Speed"]
    assert (speed.address, speed.value_type) == ("DB1.62", ValueType.INTEGER)
    assert dict(speed.metadata) == {"description": "Drehzahl in 1/min"}
    audits = [
        record
        for record in caplog.records
        if record.getMessage() == "configuration_changed"
        and getattr(record, "kind", None) == "tag"
    ]
    assert len(audits) == 5
    assert upload_dirs(tmp_path) == []


def test_comment_free_member_gets_no_description(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    source = db_source("      Speed : Int;\n")
    _, _, token = upload(signed_in_client, one_file("Data.db", source))
    assert token is not None

    status, _ = submit(
        signed_in_client,
        token,
        [("db_number_0", "4"), ("select", "Data.db#Data#Speed")],
        action="save",
    )

    assert status == 303
    tag = stores.config.get_tag("press3_speed")
    assert tag is not None
    assert dict(tag.metadata) == {}
    assert tag.address == "DB4.0"


def test_edited_name_is_used_for_name_and_id(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _, _, token = upload(
        signed_in_client, one_file("Data.db", db_source("      Speed : Int;\n"))
    )
    assert token is not None
    key = "Data.db#Data#Speed"

    status, _ = submit(
        signed_in_client,
        token,
        [("db_number_0", "4"), ("select", key), (f"name:{key}", "Line speed")],
        action="save",
    )

    assert status == 303
    tag = stores.config.get_tag("press3_line_speed")
    assert tag is not None and tag.name == "Line speed"


def test_save_refused_when_a_selected_row_has_a_problem(
    signed_in_client: TestClient, fake_host: FakeHost, tmp_path: Path
) -> None:
    stores = _seed(fake_host)
    source = db_source("      Speed : Int;\n      Stamp : Date;\n")
    _, _, token = upload(signed_in_client, one_file("Data.db", source))
    assert token is not None
    before = stores.config.revision()

    status, page = submit(
        signed_in_client,
        token,
        [
            ("db_number_0", "4"),
            ("select", "Data.db#Data#Speed"),
            ("select", "Data.db#Data#Stamp"),
        ],
        action="save",
    )

    assert status == 400
    assert "Not supported in 0.5.0" in rows(page)["Data.db#Data#Stamp"][PROBLEM]
    assert "Some selected rows have a problem" in text(page)
    assert stores.config.all_tag_ids() == set()
    assert stores.config.revision() == before
    assert len(upload_dirs(tmp_path)) == 1


def test_save_without_db_number_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    _, _, token = upload(
        signed_in_client, one_file("Data.db", db_source("      Speed : Int;\n"))
    )
    assert token is not None

    status, page = submit(
        signed_in_client,
        token,
        [("db_number_0", ""), ("select", "Data.db#Data#Speed")],
        action="save",
    )

    assert status == 400
    assert "Enter the DB number" in rows(page)["Data.db#Data#Speed"][PROBLEM]
    assert stores.config.all_tag_ids() == set()


@pytest.mark.parametrize("number", ["0", "65536", "x"])
def test_bad_db_number_is_shown(
    signed_in_client: TestClient, fake_host: FakeHost, number: str
) -> None:
    _seed(fake_host)
    _, _, token = upload(
        signed_in_client, one_file("Data.db", db_source("      Speed : Int;\n"))
    )
    assert token is not None

    status, page = submit(signed_in_client, token, [("db_number_0", number)])

    assert status == 200
    assert "Enter a DB number from 1 to 65535." in text(page)


def test_numbered_source_needs_no_number(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    source = db_source("      Speed : Int;\n", number=12)

    _, page, _ = upload(signed_in_client, one_file("Data.db", source))

    assert rows(page)["Data.db#DB12#Speed"][ADDRESS] == "DB12.DBW0"
    assert 'name="db_number_0"' not in page


def test_optimized_source_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    optimized = db_source("      Speed : Int;\n", name="Fast", access="TRUE")
    standard = db_source("      Level : Real;\n", name="Slow")

    _, page, _ = upload(
        signed_in_client,
        [*one_file("Fast.db", optimized), *one_file("Slow.db", standard)],
    )

    assert OPTIMIZED_MESSAGE in text(page)
    shown = rows(page)
    assert "Fast.db#Fast#Speed" not in shown
    assert "Slow.db#Slow#Level" in shown


def test_source_without_access_attribute_needs_the_s7_300_tick(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    source = db_source("      Speed : Int;\n", access=None)

    _, page, token = upload(signed_in_client, one_file("Old.db", source))

    assert token is not None
    assert UNKNOWN_ACCESS_MESSAGE in text(page)
    assert "This DB is from an S7-300/400 (standard access)" in text(page)
    assert rows(page) == {}
    _, ticked = submit(
        signed_in_client, token, [("db_number_0", "3"), ("classic_0", "on")]
    )
    assert rows(ticked)["Old.db#Data#Speed"][ADDRESS] == "DB3.DBW0"
    assert UNKNOWN_ACCESS_MESSAGE not in text(ticked)


def test_syntax_error_refuses_the_upload_with_file_and_line(
    signed_in_client: TestClient, fake_host: FakeHost, tmp_path: Path
) -> None:
    _seed(fake_host)
    broken = b'DATA_BLOCK "Bad"\nVERSION : 0.1\n   STRUCT\n      Speed : ;\n'

    status, page, token = upload(signed_in_client, one_file("Bad.db", broken))

    assert status == 400
    assert token is None
    assert "Bad.db, line 4" in text(page)
    assert upload_dirs(tmp_path) == []


# Tag IDs and duplicates


def test_colliding_paths_and_existing_tag_get_distinct_ids(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(
        TagConfig(
            id="press3_motor_speed",
            tag_group_id="press_fast",
            name="motor speed",
            address="M100",
            value_type=ValueType.INTEGER,
            protocol_options={"data_type": "int"},
        )
    )
    source = db_source(
        "      Motor : Struct\n         Speed : Int;\n      END_STRUCT;\n"
        "      Motor_Speed : Int;\n"
    )
    _, _, token = upload(signed_in_client, one_file("Data.db", source))
    assert token is not None
    picked = [
        ("select", "Data.db#Data#Motor.Speed"),
        ("select", "Data.db#Data#Motor_Speed"),
    ]

    _, page = submit(signed_in_client, token, [("db_number_0", "2"), *picked])
    shown = rows(page)
    assert shown["Data.db#Data#Motor.Speed"][TAG_ID] == "press3_motor_speed_2"
    assert shown["Data.db#Data#Motor_Speed"][TAG_ID] == "press3_motor_speed_3"
    status, _ = submit(
        signed_in_client, token, [("db_number_0", "2"), *picked], action="save"
    )

    assert status == 303
    assert stores.config.all_tag_ids() == {
        "press3_motor_speed",
        "press3_motor_speed_2",
        "press3_motor_speed_3",
    }
    existing = stores.config.get_tag("press3_motor_speed")
    assert existing is not None and existing.address == "M100"


def test_address_of_an_existing_tag_is_flagged_and_not_saved(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(
        TagConfig(
            id="press3_old_speed",
            tag_group_id="press_fast",
            name="Old speed",
            address="DB4.0",
            value_type=ValueType.INTEGER,
            protocol_options={"data_type": "int"},
        )
    )
    source = db_source("      Speed : Int;\n      Level : Int;\n")
    _, _, token = upload(signed_in_client, one_file("Data.db", source))
    assert token is not None

    _, page = submit(signed_in_client, token, [("db_number_0", "4")])
    assert "press3_old_speed" in rows(page)["Data.db#Data#Speed"][PROBLEM]
    assert rows(page)["Data.db#Data#Level"][PROBLEM] == ""
    status, _ = submit(
        signed_in_client,
        token,
        [("db_number_0", "4"), ("select", "Data.db#Data#Speed")],
        action="save",
    )

    assert status == 400
    assert stores.config.all_tag_ids() == {"press3_old_speed"}


def test_two_selected_rows_at_one_address_are_flagged(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    first = db_source("      Speed : Int;\n", name="A")
    second = db_source("      Level : Int;\n", name="B")
    _, _, token = upload(
        signed_in_client, [*one_file("A.db", first), *one_file("B.db", second)]
    )
    assert token is not None

    status, page = submit(
        signed_in_client,
        token,
        [
            ("db_number_0", "4"),
            ("db_number_1", "4"),
            ("select", "A.db#A#Speed"),
            ("select", "B.db#B#Level"),
        ],
        action="save",
    )

    assert status == 400
    assert "Speed" in rows(page)["B.db#B#Level"][PROBLEM]


# Big arrays


def test_big_array_previews_collapsed_and_fast(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    source = db_source("      Values : Array[0..4999] of Int;\n", number=5)

    start = time.monotonic()
    status, page, token = upload(signed_in_client, one_file("Big.db", source))
    elapsed = time.monotonic() - start

    assert status == 200
    assert elapsed < 1, elapsed
    assert token is not None
    assert len(rows(page)) < 60
    assert 'name="select_array" value="Big.db#DB5#Values"' in page
    assert "Values[0..4999]" in text(page)
    assert "5000 elements" in text(page)


def test_selecting_all_5000_elements_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    source = db_source("      Values : Array[0..4999] of Int;\n", number=5)
    _, _, token = upload(signed_in_client, one_file("Big.db", source))
    assert token is not None

    start = time.monotonic()
    status, page = submit(
        signed_in_client,
        token,
        [("select_array", "Big.db#DB5#Values")],
        action="save",
    )

    assert time.monotonic() - start < 1
    assert status == 400
    assert "Import at most 1000 tags at a time." in text(page)
    assert stores.config.all_tag_ids() == set()


def test_select_all_elements_of_a_small_collapsed_array(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    source = db_source("      Values : Array[1..60] of Int;\n", number=5)
    _, page, token = upload(signed_in_client, one_file("Big.db", source))
    assert token is not None
    assert rows(page) == {}

    status, _ = submit(
        signed_in_client,
        token,
        [("select_array", "Big.db#DB5#Values")],
        action="save",
    )

    assert status == 303
    assert len(stores.config.all_tag_ids()) == 60
    last = stores.config.get_tag("press3_values_60")
    assert last is not None and last.address == "DB5.118"


def test_collapsed_array_counts_its_problems(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    source = db_source("      Values : Array[1..60] of Int;\n")

    _, page, _ = upload(signed_in_client, one_file("Big.db", source))

    assert "60 elements with a problem." in text(page)


def test_select_all_shown_and_select_none(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    source = db_source("      Speed : Int;\n      Stamp : Date;\n      Level : Real;\n")
    _, _, token = upload(signed_in_client, one_file("Data.db", source))
    assert token is not None

    _, page = submit(
        signed_in_client,
        token,
        [("db_number_0", "4"), ("filter", "e")],
        action="select_all",
    )
    selected = re.findall(r'name="select" value="([^"]*)" checked', page)
    _, cleared = submit(
        signed_in_client,
        token,
        [("db_number_0", "4"), ("select", "Data.db#Data#Speed")],
        action="select_none",
    )

    # Stamp is not supported, so it is left out.
    assert selected == ["Data.db#Data#Speed", "Data.db#Data#Level"]
    assert "checked" not in cleared


def test_expand_shows_the_elements(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    source = db_source("      Values : Array[1..60] of Int;\n", number=5)
    _, _, token = upload(signed_in_client, one_file("Big.db", source))
    assert token is not None

    _, page = submit(signed_in_client, token, [("expand", "Big.db#DB5#Values")])

    shown = rows(page)
    assert len(shown) == 60
    assert shown["Big.db#DB5#Values[60]"][OFFSET] == "118"


def test_filter_limits_the_rows(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    _, _, token = upload(signed_in_client, press_files())
    assert token is not None

    _, page = submit(
        signed_in_client,
        token,
        [("db_number_0", "1"), ("filter", "motor"), ("select", KEY + "Pressure")],
    )

    assert list(rows(page)) == [
        KEY + "Motor.Current",
        KEY + "Motor.Speed",
        KEY + "Motor.Running",
        KEY + "Motor.Overheat",
    ]
    # A selected row the filter hides stays selected.
    assert f'type="hidden" name="select" value="{KEY}Pressure"' in page


# Tag tables


def test_tag_table_preview_and_save(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    data = table(
        [
            ["Motor on", "", "Bool", "%M10.1", "Main motor"],
            ["Speed", "", "Int", "%IW64", ""],
            ["Timer", "", "Timer", "%T1", ""],
        ]
    )
    _, page, token = upload(signed_in_client, one_file("Tags.xlsx", data))
    assert token is not None
    shown = rows(page)
    assert shown["Tags.xlsx##Motor on"][ADDRESS] == "M10.1"
    assert shown["Tags.xlsx##Speed"][ADDRESS] == "IW64"
    assert shown["Tags.xlsx##Timer"][ADDRESS] == "%T1"
    assert "Timers are not supported." in shown["Tags.xlsx##Timer"][PROBLEM]

    status, _ = submit(
        signed_in_client,
        token,
        [("select", "Tags.xlsx##Motor on"), ("select", "Tags.xlsx##Speed")],
        action="save",
    )

    assert status == 303
    motor = stores.config.get_tag("press3_motor_on")
    assert motor is not None
    assert (motor.address, dict(motor.metadata)) == (
        "M10.1",
        {"description": "Main motor"},
    )
    speed = stores.config.get_tag("press3_speed")
    assert speed is not None and speed.address == "I64"


# Upload limits and tokens


def test_eleven_files_are_refused(
    signed_in_client: TestClient, fake_host: FakeHost, tmp_path: Path
) -> None:
    _seed(fake_host)
    files = [
        f
        for index in range(11)
        for f in one_file(
            f"D{index}.db", db_source("      A : Int;\n", name=f"D{index}")
        )
    ]

    status, page, token = upload(signed_in_client, files)

    assert status == 400
    assert token is None
    assert "Upload at most 10 files at a time." in text(page)
    assert upload_dirs(tmp_path) == []


def test_six_megabyte_file_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost, tmp_path: Path
) -> None:
    _seed(fake_host)

    status, page, _ = upload(
        signed_in_client, one_file("Huge.db", b"x" * (6 * 1024 * 1024))
    )

    assert status == 400
    assert "Huge.db is larger than 5 MB." in text(page)
    assert upload_dirs(tmp_path) == []


def test_unknown_file_type_is_refused(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    status, page, _ = upload(signed_in_client, one_file("notes.txt", b"hello"))

    assert status == 400
    assert "notes.txt is not a .db, .udt or .xlsx file." in text(page)


def test_no_file_is_refused(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    _seed(fake_host)

    status, page, _ = upload(signed_in_client, one_file("", b""))

    assert status == 400
    assert "Choose at least one file." in text(page)


def test_expired_upload(
    signed_in_client: TestClient, fake_host: FakeHost, tmp_path: Path
) -> None:
    _seed(fake_host)
    _, _, token = upload(signed_in_client, press_files())
    assert token is not None
    (folder,) = upload_dirs(tmp_path)
    old = time.time() - 31 * 60
    for path in (folder, *folder.iterdir()):
        os.utime(path, (old, old))

    status, page = submit(signed_in_client, token, [("db_number_0", "1")])

    assert status == 400
    assert EXPIRED in text(page)
    assert upload_dirs(tmp_path) == []


@pytest.mark.parametrize("token", ["abc", "A" * 21, "A" * 23, "..%2F..%2Fdata"])
def test_malformed_token_is_an_expired_upload(
    signed_in_client: TestClient, fake_host: FakeHost, token: str
) -> None:
    _seed(fake_host)

    response = signed_in_client.post(f"{FORM_URL}/{token}", data={"action": "preview"})

    assert response.status_code in (400, 404)
    if response.status_code == 400:
        assert EXPIRED in text(response.text)


# Access


def test_routes_require_sign_in(panel_client: TestClient) -> None:
    token = "A" * 22
    for response in (
        panel_client.get(FORM_URL),
        panel_client.post(FORM_URL, files=one_file("Data.db", b"x")),
        panel_client.post(f"{FORM_URL}/{token}", data={"action": "preview"}),
    ):
        assert response.status_code == 303
        assert response.headers["location"].startswith("/panel/login")


def test_posts_refuse_cross_site(
    signed_in_client: TestClient, fake_host: FakeHost, tmp_path: Path
) -> None:
    _seed(fake_host)
    evil = {"Origin": "https://evil.example"}

    first = signed_in_client.post(
        FORM_URL, files=press_files(), data={"tag_group_id": "press_fast"}, headers=evil
    )
    second = signed_in_client.post(
        f"{FORM_URL}/{'A' * 22}", data={"action": "save"}, headers=evil
    )

    assert (first.status_code, second.status_code) == (403, 403)
    assert upload_dirs(tmp_path) == []


# Upload store


def test_upload_store_keeps_names_as_metadata_only(tmp_path: Path) -> None:
    store = UploadStore(tmp_path / "up")
    token = store.save([UploadedFile("../../evil.db", b"data")])

    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", token)
    stored = sorted(p.name for p in (tmp_path / "up" / token).iterdir())
    assert all("evil" not in name for name in stored)
    assert store.load(token) == [UploadedFile("../../evil.db", b"data")]
    store.delete(token)
    assert store.load(token) is None


@pytest.mark.parametrize("token", ["..", ".", "", "a/b", "A" * 21, "A" * 23])
def test_upload_store_refuses_malformed_tokens(tmp_path: Path, token: str) -> None:
    store = UploadStore(tmp_path / "up")
    (tmp_path / "up").mkdir()

    assert store.load(token) is None
    store.delete(token)
    assert (tmp_path / "up").exists()


def test_upload_store_cleans_expired_uploads_at_start(tmp_path: Path) -> None:
    now = [time.time()]
    store = UploadStore(tmp_path / "up", clock=lambda: now[0])
    token = store.save([UploadedFile("a.db", b"1")])
    now[0] += 29 * 60
    assert store.load(token) is not None
    now[0] += 31 * 60

    UploadStore(tmp_path / "up", clock=lambda: now[0])

    assert not (tmp_path / "up" / token).exists()
