"""Tests for the Fernet SecretBox and the secret-related settings."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from plc_data_collector.app.secrets import (
    SecretBox,
    is_sensitive_key,
    merge_secrets,
    split_secrets,
)
from plc_data_collector.app.settings import RuntimeSettings
from plc_data_collector.domain import ConfigurationError


def _settings(tmp_path: Path, **kwargs: object) -> RuntimeSettings:
    return RuntimeSettings(
        database_url="sqlite://",
        data_dir=tmp_path,
        **kwargs,  # type: ignore[arg-type]
    )


def test_round_trip() -> None:
    box = SecretBox(Fernet.generate_key())

    token = box.encrypt({"password": "p"})

    assert '"p"' not in token
    assert box.decrypt(token) == {"password": "p"}


def test_decrypt_with_wrong_key_raises_coded_error() -> None:
    key_a, key_b = Fernet.generate_key(), Fernet.generate_key()
    token = SecretBox(key_a).encrypt({"password": "p"})

    with pytest.raises(ConfigurationError) as error:
        SecretBox(key_b).decrypt(token)

    assert error.value.code == "secret_decryption_failed"
    assert "PLC_DATA_COLLECTOR_SECRET_KEY" in str(error.value)
    assert key_a.decode() not in str(error.value)
    assert key_b.decode() not in str(error.value)


def test_from_settings_uses_env_key(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode()
    box = SecretBox.from_settings(_settings(tmp_path, secret_key=key))

    assert SecretBox(key.encode()).decrypt(box.encrypt({"a": 1})) == {"a": 1}
    assert not (tmp_path / "secret.key").exists()


def test_from_settings_generates_key_file_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path / "data")
    with caplog.at_level(logging.WARNING):
        first = SecretBox.from_settings(settings)
        second = SecretBox.from_settings(settings)

    path = settings.secret_key_path
    assert path == tmp_path / "data" / "secret.key"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert second.decrypt(first.encrypt({"a": 1})) == {"a": 1}
    generated = [r for r in caplog.records if r.getMessage() == "secret_key_generated"]
    assert len(generated) == 1
    assert path.read_text(encoding="utf-8").strip() not in caplog.text


def test_invalid_key_raises() -> None:
    with pytest.raises(ConfigurationError) as error:
        SecretBox(b"not-a-fernet-key")

    assert error.value.code == "invalid_secret_key"
    assert "PLC_DATA_COLLECTOR_SECRET_KEY" in str(error.value)
    assert "not-a-fernet-key" not in str(error.value)


def test_invalid_env_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as error:
        SecretBox.from_settings(_settings(tmp_path, secret_key="bad"))

    assert error.value.code == "invalid_secret_key"


def test_derive_is_stable_and_purpose_specific() -> None:
    box = SecretBox(Fernet.generate_key())

    assert box.derive(b"panel-session") == box.derive(b"panel-session")
    assert box.derive(b"panel-session") != box.derive(b"other")
    assert len(box.derive(b"panel-session")) == 32


def test_split_secrets() -> None:
    plain, secret = split_secrets(
        {"username": "u", "password": "p", "auto_reconnect": False}
    )

    assert plain == {"username": "u", "auto_reconnect": False}
    assert secret == {"password": "p"}
    assert is_sensitive_key("API_Token")
    assert not is_sensitive_key("username")


def test_settings_repr_hides_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path, secret_key="KEY-VALUE", admin_password="ADMIN-PW")

    assert "KEY-VALUE" not in repr(settings)
    assert "ADMIN-PW" not in repr(settings)


def test_settings_without_config_file(tmp_path: Path) -> None:
    settings = RuntimeSettings.from_env(
        {
            "PLC_DATA_COLLECTOR_DATA_DIR": str(tmp_path),
            "PLC_DATA_COLLECTOR_SECRET_KEY": "k",
            "PLC_DATA_COLLECTOR_ADMIN_PASSWORD": "pw",
        }
    )

    assert settings.config_file is None
    assert settings.secret_key == "k"
    assert settings.admin_password == "pw"


def test_nested_split_and_merge_round_trip() -> None:
    options = {
        "auth": {"user": "u", "password": "x", "deep": {"api_token": "t", "n": 1}},
        "items": [{"password": "left-alone"}],
        "plain": {"a": 1},
    }

    plain, secret = split_secrets(options)

    assert plain == {
        "auth": {"user": "u", "deep": {"n": 1}},
        "items": [{"password": "left-alone"}],
        "plain": {"a": 1},
    }
    assert secret == {"auth": {"password": "x", "deep": {"api_token": "t"}}}
    assert merge_secrets(plain, secret) == options


def test_merge_secrets_secret_wins() -> None:
    merged = merge_secrets(
        {"a": {"password": "old", "u": 1}}, {"a": {"password": "new"}}
    )

    assert merged == {"a": {"password": "new", "u": 1}}
