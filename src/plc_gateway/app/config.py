"""Application configuration loading and validation."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from plc_gateway.domain import (
    ConfigurationError,
    ConnectionConfig,
    OverlapPolicy,
    TagConfig,
    TagGroupConfig,
    ValueType,
)

ENV_PREFIX = "PLC_GATEWAY_"
_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "key",
    "credential",
)


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Validated application configuration mapped to domain models."""

    connections: tuple[ConnectionConfig, ...]
    tag_groups: tuple[TagGroupConfig, ...]
    tags: tuple[TagConfig, ...]

    def safe_for_logging(self) -> dict[str, object]:
        """Return a representation with secrets masked for diagnostics."""
        return {
            "connections": [
                _connection_for_logging(connection) for connection in self.connections
            ],
            "tag_groups": [
                {
                    "id": group.id,
                    "connection_id": group.connection_id,
                    "interval_ms": group.interval_ms,
                    "timeout_ms": group.timeout_ms,
                    "overlap_policy": group.overlap_policy.value,
                    "enabled": group.enabled,
                }
                for group in self.tag_groups
            ],
            "tags": [
                {
                    "id": tag.id,
                    "tag_group_id": tag.tag_group_id,
                    "name": tag.name,
                    "address": tag.address,
                    "value_type": tag.value_type.value,
                    "enabled": tag.enabled,
                    "metadata": _mask_mapping(tag.metadata),
                }
                for tag in self.tags
            ],
        }


class ConnectionConfigModel(BaseModel):
    """Pydantic input model for a connection definition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    enabled: bool = True
    timeout_ms: int = Field(default=3000, gt=0)
    protocol_options: dict[str, Any] = Field(default_factory=dict)


class TagGroupConfigModel(BaseModel):
    """Pydantic input model for a cyclic tag group definition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    connection_id: str = Field(min_length=1)
    interval_ms: int = Field(gt=0)
    timeout_ms: int = Field(default=3000, gt=0)
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    enabled: bool = True


class TagConfigModel(BaseModel):
    """Pydantic input model for a configured tag definition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    tag_group_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    value_type: ValueType
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class GatewayConfigModel(BaseModel):
    """Pydantic input model for the complete gateway configuration file."""

    model_config = ConfigDict(extra="forbid")

    connections: list[ConnectionConfigModel] = Field(min_length=1)
    tag_groups: list[TagGroupConfigModel] = Field(min_length=1)
    tags: list[TagConfigModel] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Validate connection -> group -> tag references and duplicate IDs."""
        connection_ids = _unique_ids("connections", self.connections)
        group_ids = _unique_ids("tag_groups", self.tag_groups)
        _unique_ids("tags", self.tags)

        for index, group in enumerate(self.tag_groups):
            if group.connection_id not in connection_ids:
                raise _reference_error(
                    f"tag_groups[{index}].connection_id",
                    f"references unknown connection '{group.connection_id}'",
                )

        for index, tag in enumerate(self.tags):
            if tag.tag_group_id not in group_ids:
                raise _reference_error(
                    f"tags[{index}].tag_group_id",
                    f"references unknown tag group '{tag.tag_group_id}'",
                )

        return self

    def to_domain(self) -> GatewayConfig:
        """Map the validated input model to framework-independent domain models."""
        try:
            return GatewayConfig(
                connections=tuple(
                    ConnectionConfig(
                        id=connection.id,
                        protocol=connection.protocol,
                        endpoint=connection.endpoint,
                        enabled=connection.enabled,
                        timeout_ms=connection.timeout_ms,
                        protocol_options=connection.protocol_options,
                    )
                    for connection in self.connections
                ),
                tag_groups=tuple(
                    TagGroupConfig(
                        id=group.id,
                        connection_id=group.connection_id,
                        interval_ms=group.interval_ms,
                        timeout_ms=group.timeout_ms,
                        overlap_policy=group.overlap_policy,
                        enabled=group.enabled,
                    )
                    for group in self.tag_groups
                ),
                tags=tuple(
                    TagConfig(
                        id=tag.id,
                        tag_group_id=tag.tag_group_id,
                        name=tag.name,
                        address=tag.address,
                        value_type=tag.value_type,
                        enabled=tag.enabled,
                        metadata=tag.metadata,
                    )
                    for tag in self.tags
                ),
            )
        except ConfigurationError as error:
            raise ConfigurationError(
                f"Invalid configuration: {error.message}",
                code="invalid_configuration",
                details=error.details,
            ) from error


def load_gateway_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> GatewayConfig:
    """Load a JSON gateway configuration file and apply environment overrides."""
    raw_config = _read_json_object(Path(path))
    _apply_environment_overrides(raw_config, os.environ if environ is None else environ)

    try:
        model = GatewayConfigModel.model_validate(raw_config)
    except ValidationError as error:
        raise ConfigurationError(
            _format_validation_error(error),
            code="invalid_configuration",
            details={"errors": error.errors(include_url=False)},
        ) from error

    return model.to_domain()


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.suffix.lower() != ".json":
        raise ConfigurationError(
            f"Unsupported configuration format at {path}: expected .json.",
            code="unsupported_configuration_format",
            details={"path": str(path)},
        )

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Configuration file not found at {path}.",
            code="configuration_not_found",
            details={"path": str(path)},
        ) from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}.",
            code="invalid_configuration_json",
            details={"path": str(path), "line": error.lineno, "column": error.colno},
        ) from error

    if not isinstance(loaded, dict):
        raise ConfigurationError(
            f"Configuration root at {path} must be a JSON object.",
            code="invalid_configuration_shape",
            details={"path": str(path), "location": "$"},
        )
    return loaded


def _apply_environment_overrides(
    raw_config: dict[str, Any],
    environ: Mapping[str, str],
) -> None:
    connections = raw_config.get("connections")
    if not isinstance(connections, list):
        return

    by_id = {
        str(connection.get("id")).upper(): connection
        for connection in connections
        if isinstance(connection, dict) and connection.get("id") is not None
    }
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX + "CONNECTIONS__"):
            continue

        parts = key.removeprefix(ENV_PREFIX).split("__")
        if len(parts) != 3:
            raise ConfigurationError(
                f"Invalid environment override '{key}': expected "
                "PLC_GATEWAY_CONNECTIONS__<ID>__<FIELD>.",
                code="invalid_environment_override",
                details={"environment_variable": key},
            )

        _, connection_id, field_name = parts
        connection = by_id.get(connection_id.upper())
        if connection is None:
            raise ConfigurationError(
                f"Invalid environment override '{key}': unknown connection "
                f"'{connection_id}'.",
                code="invalid_environment_override",
                details={"environment_variable": key, "connection_id": connection_id},
            )

        normalized_field = field_name.lower()
        if normalized_field not in {"endpoint", "enabled", "timeout_ms"}:
            raise ConfigurationError(
                f"Invalid environment override '{key}': unsupported field "
                f"'{field_name}'.",
                code="invalid_environment_override",
                details={"environment_variable": key, "field": field_name},
            )
        connection[normalized_field] = _coerce_override_value(normalized_field, value)


def _coerce_override_value(field_name: str, value: str) -> str | bool | int:
    if field_name == "enabled":
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ConfigurationError(
            f"Invalid boolean environment override for {field_name}: '{value}'.",
            code="invalid_environment_override",
            details={"field": field_name},
        )
    if field_name == "timeout_ms":
        try:
            return int(value)
        except ValueError as error:
            raise ConfigurationError(
                f"Invalid integer environment override for {field_name}: '{value}'.",
                code="invalid_environment_override",
                details={"field": field_name},
            ) from error
    return value


def _unique_ids(section: str, models: list[Any]) -> set[str]:
    seen: set[str] = set()
    for index, model in enumerate(models):
        item_id = model.id
        if item_id in seen:
            raise _reference_error(
                f"{section}[{index}].id", f"duplicate id '{item_id}'"
            )
        seen.add(item_id)
    return seen


def _reference_error(location: str, message: str) -> ConfigurationError:
    return ConfigurationError(
        f"Invalid configuration at {location}: {message}.",
        code="invalid_configuration_reference",
        details={"location": location},
    )


def _format_validation_error(error: ValidationError) -> str:
    first_error = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first_error["loc"]) or "$"
    return f"Invalid configuration at {location}: {first_error['msg']}."


def _connection_for_logging(connection: ConnectionConfig) -> dict[str, object]:
    return {
        "id": connection.id,
        "protocol": connection.protocol,
        "endpoint": _mask_endpoint(connection.endpoint),
        "enabled": connection.enabled,
        "timeout_ms": connection.timeout_ms,
        "protocol_options": _mask_mapping(connection.protocol_options),
    }


def _mask_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    masked: dict[str, object] = {}
    for key, value in mapping.items():
        if _is_sensitive_key(key):
            masked[key] = "***"
        elif isinstance(value, Mapping):
            masked[key] = _mask_mapping(value)
        elif isinstance(value, list):
            masked[key] = [_mask_sequence_item(item) for item in value]
        else:
            masked[key] = value
    return masked


def _mask_sequence_item(value: object) -> object:
    if isinstance(value, Mapping):
        return _mask_mapping(value)
    if isinstance(value, list):
        return [_mask_sequence_item(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _mask_endpoint(endpoint: str) -> str:
    split = urlsplit(endpoint)
    if "@" not in split.netloc:
        return endpoint
    _, host = split.netloc.rsplit("@", 1)
    return urlunsplit(
        SplitResult(
            scheme=split.scheme,
            netloc=f"***@{host}",
            path=split.path,
            query=split.query,
            fragment=split.fragment,
        ),
    )
