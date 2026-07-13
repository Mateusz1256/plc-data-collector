from __future__ import annotations

import json
from pathlib import Path

import pytest

from plc_gateway.app.config import load_gateway_config
from plc_gateway.domain import ConfigurationError, OverlapPolicy, ValueType


def write_config(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def valid_config() -> dict[str, object]:
    return {
        "connections": [
            {
                "id": "opcua_demo",
                "protocol": "opcua",
                "endpoint": "opc.tcp://user:secret@127.0.0.1:4840",
                "timeout_ms": 5000,
                "protocol_options": {
                    "username": "operator",
                    "password": "do-not-log",
                    "security": {"private_key_path": "certs/client.key"},
                },
            }
        ],
        "tag_groups": [
            {
                "id": "fast",
                "connection_id": "opcua_demo",
                "interval_ms": 1000,
                "timeout_ms": 2000,
                "overlap_policy": "skip",
            }
        ],
        "tags": [
            {
                "id": "temperature",
                "tag_group_id": "fast",
                "name": "Temperature",
                "address": "ns=2;s=Machine.Temperature",
                "value_type": "numeric",
                "metadata": {"unit": "C"},
            }
        ],
    }


def test_load_gateway_config_maps_valid_json_to_domain_models(
    tmp_path: Path,
) -> None:
    config_path = write_config(tmp_path / "gateway.config.json", valid_config())

    config = load_gateway_config(config_path, environ={})

    assert config.connections[0].id == "opcua_demo"
    assert config.connections[0].protocol == "opcua"
    assert config.connections[0].timeout_ms == 5000
    assert config.tag_groups[0].connection_id == "opcua_demo"
    assert config.tag_groups[0].overlap_policy is OverlapPolicy.SKIP
    assert config.tags[0].value_type is ValueType.NUMERIC


def test_load_gateway_config_rejects_unknown_group_connection(
    tmp_path: Path,
) -> None:
    payload = valid_config()
    groups = payload["tag_groups"]
    assert isinstance(groups, list)
    group = groups[0]
    assert isinstance(group, dict)
    group["connection_id"] = "missing"
    config_path = write_config(tmp_path / "gateway.config.json", payload)

    with pytest.raises(
        ConfigurationError,
        match=r"tag_groups\[0\]\.connection_id",
    ):
        load_gateway_config(config_path, environ={})


def test_load_gateway_config_applies_connection_environment_overrides(
    tmp_path: Path,
) -> None:
    config_path = write_config(tmp_path / "gateway.config.json", valid_config())

    config = load_gateway_config(
        config_path,
        environ={
            "PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENDPOINT": "opc.tcp://localhost:4841",
            "PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__TIMEOUT_MS": "7500",
            "PLC_GATEWAY_CONNECTIONS__OPCUA_DEMO__ENABLED": "false",
        },
    )

    connection = config.connections[0]
    assert connection.endpoint == "opc.tcp://localhost:4841"
    assert connection.timeout_ms == 7500
    assert connection.enabled is False


def test_safe_for_logging_masks_secrets() -> None:
    config = load_gateway_config(
        Path("docs/examples/gateway.config.example.json"),
        environ={},
    )

    safe_text = json.dumps(config.safe_for_logging(), sort_keys=True)

    assert "do-not-log" not in safe_text
    assert "client.key" not in safe_text
    assert "operator:secret" not in safe_text
    assert '"password": "***"' in safe_text
    assert '"endpoint": "opc.tcp://***@127.0.0.1:4840"' in safe_text


def test_load_gateway_config_reports_validation_location(tmp_path: Path) -> None:
    payload = valid_config()
    connections = payload["connections"]
    assert isinstance(connections, list)
    connection = connections[0]
    assert isinstance(connection, dict)
    connection["timeout_ms"] = 0
    config_path = write_config(tmp_path / "gateway.config.json", payload)

    with pytest.raises(ConfigurationError, match=r"connections\.0\.timeout_ms"):
        load_gateway_config(config_path, environ={})
