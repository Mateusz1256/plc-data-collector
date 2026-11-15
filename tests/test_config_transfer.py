"""Tests for configuration diff, export and import parsing."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from plc_data_collector.app.config import CollectorConfig, load_collector_config
from plc_data_collector.app.config_transfer import (
    MAX_IMPORT_BYTES,
    ConfigChange,
    diff_configs,
    export_config,
    export_filename,
    parse_import,
)
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    TagConfig,
    TagGroupConfig,
    ValueType,
)


def _config() -> CollectorConfig:
    return CollectorConfig(
        connections=(
            ConnectionConfig(
                id="line-1",
                name="Line 1",
                protocol="opcua",
                endpoint="opc.tcp://plc:4840",
                protocol_options={
                    "username": "u",
                    "password": "p",
                    "security": {"a": 1},
                },
            ),
        ),
        tag_groups=(
            TagGroupConfig(id="fast", connection_id="line-1", interval_ms=1000),
        ),
        tags=(
            TagConfig(
                id="t1",
                tag_group_id="fast",
                name="Temperature",
                address="ns=2;i=1",
                value_type=ValueType.NUMERIC,
                metadata={"unit": "C"},
            ),
        ),
    )


def test_diff_detects_added_changed_removed() -> None:
    current = _config()
    extra_tag = replace(current.tags[0], id="t2", name="Pressure")
    new = CollectorConfig(
        connections=(
            replace(
                current.connections[0],
                protocol_options={"username": "u", "password": "other"},
            ),
        ),
        tag_groups=(),
        tags=(extra_tag,),
    )

    diff = diff_configs(current, new)

    assert diff.added == (ConfigChange("tag", "t2", "Pressure"),)
    assert diff.changed == (ConfigChange("connection", "line-1", "Line 1"),)
    assert diff.removed == (
        ConfigChange("tag_group", "fast", "fast"),
        ConfigChange("tag", "t1", "Temperature"),
    )
    assert diff.total == 4
    assert not diff.is_empty


def test_diff_empty_for_identical() -> None:
    diff = diff_configs(_config(), _config())

    assert diff.is_empty
    assert diff.total == 0


def test_export_omits_secrets_and_round_trips() -> None:
    config = _config()

    exported = export_config(config)

    assert exported["format_version"] == 1
    assert "password" not in json.dumps(exported)
    assert parse_import(json.dumps(exported).encode(), current=config) == config


def test_import_keeps_existing_secrets_only_for_known_ids() -> None:
    config = _config()
    exported: dict[str, Any] = json.loads(json.dumps(export_config(config)))
    exported["connections"].append(
        {"id": "line-2", "protocol": "opcua", "endpoint": "opc.tcp://other:4840"}
    )
    exported["tag_groups"].append(
        {"id": "g2", "connection_id": "line-2", "interval_ms": 500}
    )

    parsed = parse_import(json.dumps(exported).encode(), current=config)

    known, new = parsed.connections
    assert known.protocol_options["password"] == "p"
    assert "password" not in new.protocol_options
    # A secret supplied in the file wins over the stored one.
    exported["connections"][0]["protocol_options"]["password"] = "file"
    again = parse_import(json.dumps(exported).encode(), current=config)
    assert again.connections[0].protocol_options["password"] == "file"


def test_import_errors() -> None:
    config = _config()
    good = export_config(config)

    def code_of(data: bytes) -> ConfigurationError:
        with pytest.raises(ConfigurationError) as error:
            parse_import(data, current=config)
        return error.value

    too_large = code_of(b" " * (MAX_IMPORT_BYTES + 1))
    assert too_large.code == "import_too_large"

    bad_json = code_of(b'{"connections": ')
    assert bad_json.code == "invalid_configuration_json"
    assert "line 1" in bad_json.message

    bad_bytes = code_of(b"\xff\xfe")
    assert bad_bytes.code == "invalid_configuration_json"

    shape = code_of(b"[]")
    assert shape.code == "invalid_configuration_shape"
    assert "$" in shape.message

    invalid = json.loads(json.dumps(good))
    invalid["tags"][0]["value_type"] = "nope"
    err = code_of(json.dumps(invalid).encode())
    assert err.code == "invalid_configuration"
    assert "tags.0.value_type" in err.message

    reference = json.loads(json.dumps(good))
    reference["tags"][0]["tag_group_id"] = "missing"
    err = code_of(json.dumps(reference).encode())
    assert err.code == "invalid_configuration_reference"
    assert "tags[0].tag_group_id" in err.message


def test_nested_secret_not_exported_and_restored_on_import() -> None:
    base = _config()
    connection = replace(
        base.connections[0],
        protocol_options={"auth": {"user": "u", "password": "nested-pw"}},
    )
    config = replace(base, connections=(connection,))

    exported = export_config(config)

    assert "nested-pw" not in json.dumps(exported)
    assert parse_import(json.dumps(exported).encode(), current=config) == config


@pytest.mark.parametrize(
    "data",
    [b"[" * 100_000, b"1" + b"0" * 5000],
    ids=["too-deep", "huge-integer"],
)
def test_import_pathological_json_is_a_coded_error(data: bytes) -> None:
    with pytest.raises(ConfigurationError) as error:
        parse_import(data, current=_config())

    assert error.value.code == "invalid_configuration_json"
    assert "too deeply" in error.value.message


def test_export_is_valid_seed_file(tmp_path: Path) -> None:
    config = _config()
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(export_config(config)), encoding="utf-8")

    loaded = load_collector_config(path, environ={})

    without_secrets = replace(
        config.connections[0],
        protocol_options={"username": "u", "security": {"a": 1}},
    )
    assert loaded.connections == (without_secrets,)
    assert loaded.tags == config.tags


def test_export_filename() -> None:
    now = datetime(2026, 11, 12, 9, 5, 7, tzinfo=UTC)

    assert export_filename(now) == "plc-data-collector-config-20261112-090507.json"


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("connections", "timeout_ms"),
        ("tag_groups", "interval_ms"),
        ("tag_groups", "timeout_ms"),
    ],
)
def test_import_refuses_values_beyond_a_database_integer(
    section: str, field: str
) -> None:
    config = _config()
    exported: dict[str, Any] = export_config(config)
    exported[section][0][field] = 2_147_483_648

    with pytest.raises(ConfigurationError) as caught:
        parse_import(json.dumps(exported).encode(), current=config)

    assert caught.value.code == "invalid_configuration"
    assert "2147483647" in caught.value.message


def _with_endpoint(config: CollectorConfig, endpoint: str) -> CollectorConfig:
    connection = replace(config.connections[0], endpoint=endpoint)
    return replace(config, connections=(connection,))


@pytest.mark.parametrize(
    ("endpoint", "exported"),
    [
        ("opc.tcp://op:pa%40ss@plc:4840/path", "opc.tcp://plc:4840/path"),
        ("opc.tcp://op@plc:4840", "opc.tcp://plc:4840"),
        ("opc.tcp://plc:4840", "opc.tcp://plc:4840"),
        # Unparseable (bad IPv6 literal): the credentials still go.
        ("opc.tcp://op:pa%40ss@[bad:4840/x", "opc.tcp://[bad:4840/x"),
    ],
)
def test_export_strips_endpoint_userinfo(endpoint: str, exported: str) -> None:
    document = export_config(_with_endpoint(_config(), endpoint))

    assert document["connections"][0]["endpoint"] == exported  # type: ignore[index]
    assert "pa%40ss" not in json.dumps(document)
    assert "***" not in json.dumps(document)


def test_import_keeps_stored_endpoint_credentials_when_host_matches() -> None:
    current = _with_endpoint(_config(), "opc.tcp://op:secret@plc:4840")
    document = export_config(current)

    same = parse_import(json.dumps(document).encode(), current=current)
    assert same.connections[0].endpoint == "opc.tcp://op:secret@plc:4840"

    document["connections"][0]["endpoint"] = "opc.tcp://other:4840"  # type: ignore[index]
    moved = parse_import(json.dumps(document).encode(), current=current)
    assert moved.connections[0].endpoint == "opc.tcp://other:4840"
