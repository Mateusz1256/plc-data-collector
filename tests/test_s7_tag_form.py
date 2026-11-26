"""Tests for the Panel's S7 Tag form: adapter, fast entry and the Paste address box."""

from __future__ import annotations

import re
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient
from panel_support import FakeHost

from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import (
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.panel.protocol_forms import FormErrors
from plc_data_collector.panel.s7_tag_form import S7TagForm
from plc_data_collector.panel.tag_adapters import tag_adapter_for

S7 = ConnectionConfig(
    id="press3",
    name="Press 3",
    protocol="s7",
    endpoint="tcp://press3-plc:102",
)
GROUPS = [("press_fast", "press_fast (500 ms)")]


def _data(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Line speed",
        "tag_group": "press_fast",
        "area": "db",
        "db": "10",
        "byte": "4",
        "bit": "",
        "data_type": "real",
        "length": "",
        "scale": "",
        "offset": "",
        "unit": "m/min",
        "enabled": "on",
    }
    data.update(overrides)
    return data


def _build(data: dict[str, str], existing: TagConfig | None = None) -> TagConfig:
    return S7TagForm().build(
        data,
        connection=S7,
        tag_id="t1",
        allowed_group_ids={"press_fast"},
        existing=existing,
        enabled="enabled" in data,
    )


def _errors(data: dict[str, str]) -> dict[str, str]:
    with pytest.raises(FormErrors) as raised:
        _build(data)
    return raised.value.errors


# Adapter


def test_adapter_for_s7() -> None:
    assert isinstance(tag_adapter_for("s7"), S7TagForm)
    assert S7TagForm.columns == (
        "name",
        "address",
        "data_type",
        "length",
        "scale",
        "offset",
        "unit",
        "tag_group",
        "enabled",
    )
    assert S7TagForm.required_columns == ("name", "address", "data_type")


def test_defaults_choose_no_data_type() -> None:
    values = S7TagForm().defaults(S7, GROUPS)

    assert values["area"] == "db"
    assert values["data_type"] == ""
    assert values["tag_group"] == "press_fast"
    assert values["enabled"] is True


def test_build_real_in_a_data_block() -> None:
    tag = _build(_data())

    assert tag.address == "DB10.4"
    assert dict(tag.protocol_options) == {"data_type": "real"}
    assert tag.value_type is ValueType.NUMERIC
    assert tag.metadata == {"unit": "m/min"}
    assert tag.enabled is True


def test_build_merker_bool_ignores_db_number() -> None:
    tag = _build(_data(area="m", db="10", byte="20", bit="3", data_type="bool"))

    assert tag.address == "M20.3"
    assert tag.value_type is ValueType.BOOLEAN


def test_build_string_with_length_and_scaled_int() -> None:
    text = _build(_data(data_type="string", length="20"))
    scaled = _build(_data(data_type="int", scale="0,1", offset="-5"))

    assert dict(text.protocol_options) == {"data_type": "string", "length": 20}
    assert text.value_type is ValueType.TEXT
    assert dict(scaled.protocol_options) == {
        "data_type": "int",
        "scale": 0.1,
        "offset": -5.0,
    }
    assert scaled.value_type is ValueType.NUMERIC


@pytest.mark.parametrize(
    ("overrides", "errors"),
    [
        (
            {"data_type": "bool"},
            {"bit": "Enter the bit number (0-7) for data type bool."},
        ),
        ({"bit": "3"}, {"bit": "A bit number is used only with data type bool."}),
        ({"data_type": "bool", "bit": "8"}, {"bit": "Enter a bit number from 0 to 7."}),
        ({"db": ""}, {"db": "Enter a DB number from 1 to 65535."}),
        ({"db": "0"}, {"db": "Enter a DB number from 1 to 65535."}),
        ({"byte": "65536"}, {"byte": "Enter a byte number from 0 to 65535."}),
        ({"byte": ""}, {"byte": "Enter a byte number from 0 to 65535."}),
        ({"area": "x"}, {"area": "Choose one of the listed options."}),
        ({"data_type": ""}, {"data_type": "Choose one of the listed options."}),
        (
            {"data_type": "string"},
            {"length": "Enter the length in characters (1-254) for data type string."},
        ),
        ({"length": "4"}, {"length": "Length is used only with data type string."}),
        (
            {"data_type": "string", "length": "255"},
            {"length": "Enter a length from 1 to 254 characters."},
        ),
        ({"scale": "0"}, {"scale": "Scale must not be 0."}),
        ({"scale": "abc"}, {"scale": "Enter a number, for example 0.1."}),
        (
            {"data_type": "bool", "bit": "0", "scale": "2"},
            {"scale": "Scale isn't used with bool."},
        ),
        ({"byte": "65534"}, {"byte": "The value does not fit in bytes 0-65535."}),
    ],
)
def test_build_field_errors(overrides: dict[str, str], errors: dict[str, str]) -> None:
    assert _errors(_data(**overrides)) == errors


def test_build_keeps_other_metadata() -> None:
    existing = _build(_data())
    existing = TagConfig(
        id=existing.id,
        tag_group_id=existing.tag_group_id,
        name=existing.name,
        address=existing.address,
        value_type=existing.value_type,
        metadata={"description": "From the drive", "unit": "m/min"},
        protocol_options=existing.protocol_options,
    )

    again = _build(_data(unit=""), existing)

    assert again.metadata == {"description": "From the drive"}


def test_values_round_trip() -> None:
    form = S7TagForm()
    tag = _build(_data(data_type="int", scale="0.1", offset="-3.5"))

    values = form.values(tag, S7)

    assert (values["area"], values["db"], values["byte"], values["bit"]) == (
        "db",
        "10",
        "4",
        "",
    )
    assert (values["data_type"], values["scale"], values["offset"]) == (
        "int",
        "0.1",
        "-3.5",
    )
    data = {k: str(v) for k, v in values.items() if k != "enabled"}
    data["enabled"] = "on"
    assert _build(data, tag) == tag


def test_next_values_steps_by_span() -> None:
    values = S7TagForm().next_values(_build(_data()), S7)

    assert values is not None
    assert (values["area"], values["db"], values["byte"]) == ("db", "10", "8")
    assert (values["name"], values["data_type"], values["tag_group"]) == (
        "",
        "real",
        "press_fast",
    )


def test_next_values_for_bool_steps_the_bit() -> None:
    form = S7TagForm()
    middle = form.next_values(_build(_data(data_type="bool", bit="3")), S7)
    last = form.next_values(_build(_data(data_type="bool", bit="7")), S7)

    assert middle is not None and last is not None
    assert (middle["byte"], middle["bit"]) == ("4", "4")
    assert (last["byte"], last["bit"]) == ("5", "0")


def test_next_values_for_string_and_at_the_end() -> None:
    form = S7TagForm()
    string = form.next_values(_build(_data(data_type="string", length="20")), S7)
    end = form.next_values(_build(_data(byte="65534", data_type="int")), S7)

    assert string is not None and end is not None
    assert string["byte"] == "26"
    assert end["byte"] == ""


def test_table_cells_show_step7_address() -> None:
    form = S7TagForm()

    assert form.table_cells(_build(_data()), S7) == ("Data block", "DB10.DBD4", "real")
    bit = _build(_data(area="q", byte="1", bit="2", data_type="bool"))
    assert form.table_cells(bit, S7) == ("Outputs", "Q1.2", "bool")


def test_register_key_is_canonical_area_db_byte_bit() -> None:
    form = S7TagForm()
    tag = _build(_data(data_type="bool", bit="0"))

    assert form.register_key(tag, S7) == ("db", 10, 4, 0)


def test_build_from_csv_row_checks_the_size_letter() -> None:
    row = {
        "name": "Speed",
        "address": "%DB10.DBD4",
        "data_type": "real",
        "tag_group": "press_fast",
    }
    tag = S7TagForm().build(
        row,
        connection=S7,
        tag_id="t1",
        allowed_group_ids={"press_fast"},
        existing=None,
    )
    assert tag.address == "DB10.4"

    row["address"] = "DB10.DBW4"
    with pytest.raises(FormErrors) as raised:
        S7TagForm().build(
            row,
            connection=S7,
            tag_id="t1",
            allowed_group_ids={"press_fast"},
            existing=None,
        )
    assert raised.value.errors == {
        "address": "DB10.DBW4 is a 2-byte address; real needs 4 bytes."
    }


@pytest.mark.parametrize(
    ("text", "chosen", "expected", "data_type"),
    [
        ("%MW20", "", {"area": "m", "db": "", "byte": "20", "bit": ""}, "word"),
        ("%MW20", "int", {"area": "m", "byte": "20"}, "int"),
        (
            "DB10.DBX4.3",
            "",
            {"area": "db", "db": "10", "byte": "4", "bit": "3"},
            "bool",
        ),
        ("%DB7.DBD12", "", {"area": "db", "db": "7", "byte": "12"}, "dword"),
        ("IB2", "", {"area": "i", "byte": "2"}, "byte"),
        ("AW8", "", {"area": "q", "byte": "8"}, "word"),
        ("DB10.4", "", {"area": "db", "db": "10", "byte": "4"}, ""),
        ("DB10.DBB4", "string", {"area": "db", "byte": "4"}, "string"),
    ],
)
def test_fill_from_address(
    text: str, chosen: str, expected: dict[str, str], data_type: str
) -> None:
    values = S7TagForm().defaults(S7, GROUPS)
    values.update({"paste_address": text, "data_type": chosen, "bit": "5"})

    filled, errors = S7TagForm().fill_from_address(values)

    assert errors == {}
    assert {key: filled[key] for key in expected} == expected
    assert filled["data_type"] == data_type
    assert filled["paste_address"] == ""


@pytest.mark.parametrize(
    ("text", "chosen", "message"),
    [
        ("%MW20", "real", "%MW20 is a 2-byte address; real needs 4 bytes."),
        ("DB0.DBW4", "", "DB number 0 in 'DB0.DBW4' is outside 1-65535."),
        ("", "", "Enter an address, for example %DB10.DBD4 or %MW20."),
    ],
)
def test_fill_from_address_errors_keep_the_fields(
    text: str, chosen: str, message: str
) -> None:
    values = S7TagForm().defaults(S7, GROUPS)
    values.update({"paste_address": text, "data_type": chosen, "byte": "7"})

    filled, errors = S7TagForm().fill_from_address(values)

    assert errors == {"paste_address": message}
    assert filled["byte"] == "7"
    assert filled["paste_address"] == text


# Routes


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


def _input_value(html: str, name: str) -> str | None:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    if match is None:
        return None
    value = re.search(r'value="([^"]*)"', match.group(0))
    return value.group(1) if value else ""


def _selected(html: str, name: str) -> str | None:
    body = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S)
    option = re.search(
        r'<option value="([^"]*)" selected>', body.group(1) if body else ""
    )
    return option.group(1) if option else None


def test_tag_form_creates_real_tag(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)

    response = signed_in_client.post("/panel/connections/press3/tags", data=_data())

    assert response.status_code == 303
    tag = stores.config.get_tag("press3_line_speed")
    assert tag is not None
    assert tag.address == "DB10.4"
    assert dict(tag.protocol_options) == {"data_type": "real"}
    assert tag.value_type is ValueType.NUMERIC


def test_tag_form_refuses_bool_without_bit(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)

    response = signed_in_client.post(
        "/panel/connections/press3/tags", data=_data(data_type="bool")
    )

    assert response.status_code == 400
    assert "Enter the bit number (0-7) for data type bool." in response.text
    assert stores.config.all_tag_ids() == set()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, {"area": ["db"], "db": ["10"], "byte": ["8"], "data_type": ["real"]}),
        (
            {"data_type": "bool", "bit": "7"},
            {"db": ["10"], "byte": ["5"], "bit": ["0"], "data_type": ["bool"]},
        ),
    ],
)
def test_fast_entry_proposes_next_address(
    signed_in_client: TestClient,
    fake_host: FakeHost,
    overrides: dict[str, str],
    expected: dict[str, list[str]],
) -> None:
    _seed(fake_host)

    response = signed_in_client.post(
        "/panel/connections/press3/tags", data=_data(**overrides)
    )

    assert response.status_code == 303
    path, _, query = response.headers["location"].partition("?")
    assert path == "/panel/connections/press3/tags/new"
    params = parse_qs(query)
    assert {key: params.get(key) for key in expected} == expected
    assert params["tag_group"] == ["press_fast"]
    assert "name" not in params
    page = signed_in_client.get(response.headers["location"]).text
    html = page[page.index('action="/panel/connections/press3/tags"') :]
    assert _input_value(html, "byte") == expected["byte"][0]
    assert _selected(html, "data_type") == expected["data_type"][0]


def test_tags_list_shows_step7_address_and_description(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_tag(
        TagConfig(
            id="press3_speed",
            tag_group_id="press_fast",
            name="Speed",
            address="DB10.4",
            value_type=ValueType.NUMERIC,
            metadata={"description": 'Drive "A" speed'},
            protocol_options={"data_type": "real"},
        )
    )

    page = signed_in_client.get("/panel/connections/press3").text

    row = re.search(r'<tr[^>]*data-tag="press3_speed"[^>]*>.*?</tr>', page, re.S)
    assert row is not None
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(0), re.S)
    assert cells[1:4] == ["Data block", "DB10.DBD4", "real"]
    assert 'title="Drive &#34;A&#34; speed"' in row.group(0)


def test_new_tag_form_fields(signed_in_client: TestClient, fake_host: FakeHost) -> None:
    _seed(fake_host)

    html = signed_in_client.get(
        "/panel/connections/press3/tags/new", headers={"HX-Request": "true"}
    ).text

    assert _selected(html, "area") == "db"
    assert _input_value(html, "db") == ""
    assert _input_value(html, "paste_address") == ""
    assert _selected(html, "data_type") == ""
    assert _input_value(html, "bit") is None
    assert _input_value(html, "length") is None
    assert 'hx-post="/panel/connections/press3/tags/s7-address"' in html


def test_redraw_shows_bit_for_bool_and_hides_db_for_merkers(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    html = signed_in_client.get(
        "/panel/connections/press3/tags/new",
        params={
            "area": "m",
            "byte": "2",
            "data_type": "bool",
            "tag_group": "press_fast",
        },
        headers={"HX-Request": "true"},
    ).text

    assert _input_value(html, "bit") == ""
    assert _input_value(html, "db") is None
    assert _input_value(html, "length") is None


def test_paste_address_fills_fields(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)
    data = _data(paste_address="%MW20", data_type="", name="Pressure", db="10")

    response = signed_in_client.post(
        "/panel/connections/press3/tags/s7-address",
        data=data,
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    html = response.text
    assert "<html" not in html
    assert 'aria-label="New tag"' in html
    assert _selected(html, "area") == "m"
    assert _input_value(html, "byte") == "20"
    assert _input_value(html, "db") is None
    assert _selected(html, "data_type") == "word"
    assert _input_value(html, "name") == "Pressure"


def test_paste_address_keeps_a_chosen_data_type(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    html = signed_in_client.post(
        "/panel/connections/press3/tags/s7-address",
        data=_data(paste_address="%MW20", data_type="int"),
        headers={"HX-Request": "true"},
    ).text

    assert _selected(html, "area") == "m"
    assert _selected(html, "data_type") == "int"


def test_paste_address_contradiction_is_an_error(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    response = signed_in_client.post(
        "/panel/connections/press3/tags/s7-address",
        data=_data(paste_address="DB10.DBW4", data_type="real", byte="9"),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "DB10.DBW4 is a 2-byte address; real needs 4 bytes." in response.text
    assert _input_value(response.text, "byte") == "9"


def test_paste_address_in_edit_form_keeps_edit_mode(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    signed_in_client.post("/panel/connections/press3/tags", data=_data())
    assert stores.config.get_tag("press3_line_speed") is not None

    edit = signed_in_client.get(
        "/panel/tags/press3_line_speed/edit", headers={"HX-Request": "true"}
    ).text
    assert _input_value(edit, "tag_id") == "press3_line_speed"

    html = signed_in_client.post(
        "/panel/connections/press3/tags/s7-address",
        data=_data(paste_address="DB11.DBD0", tag_id="press3_line_speed"),
        headers={"HX-Request": "true"},
    ).text

    assert 'aria-label="Edit tag"' in html
    assert 'action="/panel/tags/press3_line_speed"' in html
    assert _input_value(html, "db") == "11"


def test_paste_address_without_htmx_shows_the_page(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    response = signed_in_client.post(
        "/panel/connections/press3/tags/s7-address",
        data=_data(paste_address="%MW20", data_type=""),
    )

    assert response.status_code == 200
    assert "<html" in response.text
    assert _selected(response.text, "area") == "m"


def test_paste_address_needs_an_s7_connection(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    stores.config.upsert_connection(
        ConnectionConfig(
            id="line1", name="Line 1", protocol="opcua", endpoint="opc.tcp://x:4840"
        )
    )

    for connection_id in ("nope", "line1"):
        response = signed_in_client.post(
            f"/panel/connections/{connection_id}/tags/s7-address",
            data={"paste_address": "%MW20"},
        )
        assert response.status_code == 404, connection_id
    foreign = signed_in_client.post(
        "/panel/connections/press3/tags/s7-address",
        data={"paste_address": "%MW20", "tag_id": "nope"},
    )
    assert foreign.status_code == 404


def test_paste_address_requires_sign_in(panel_client: TestClient) -> None:
    response = panel_client.post(
        "/panel/connections/press3/tags/s7-address", data={"paste_address": "%MW20"}
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/panel/login")


def test_paste_address_refuses_cross_site(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    _seed(fake_host)

    response = signed_in_client.post(
        "/panel/connections/press3/tags/s7-address",
        data={"paste_address": "%MW20"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_s7_paste_route_preview_and_save(
    signed_in_client: TestClient, fake_host: FakeHost
) -> None:
    stores = _seed(fake_host)
    url = "/panel/connections/press3/tags/paste"
    text = "name;address;data_type\nSpeed;%DB10.DBD4;real\nRun;DB10.DBX8.0;bool\n"
    data = {"text": text, "tag_group_id": "press_fast"}

    preview = signed_in_client.post(url, data={**data, "action": "preview"})

    assert preview.status_code == 200
    assert "DB10.DBD4" in preview.text
    assert "DB10.DBX8.0" in preview.text
    saved = signed_in_client.post(url, data={**data, "action": "save"})
    assert saved.status_code == 303
    assert stores.config.all_tag_ids() == {"press3_speed", "press3_run"}
