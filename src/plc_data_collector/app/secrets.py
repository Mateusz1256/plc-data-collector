"""Encryption of Connection secrets at rest."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from plc_data_collector.domain import ConfigurationError

if TYPE_CHECKING:
    from plc_data_collector.app.settings import RuntimeSettings

LOGGER = logging.getLogger(__name__)

_KEY_VARIABLE = "PLC_DATA_COLLECTOR_SECRET_KEY"
_SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "key",
    "credential",
)


def is_sensitive_key(key: str) -> bool:
    """Return whether an option key names a secret value."""
    normalized = key.lower()
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def split_secrets(
    options: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Split top-level options into ``(plain, secret)`` by key name."""
    plain: dict[str, object] = {}
    secret: dict[str, object] = {}
    for key, value in options.items():
        (secret if is_sensitive_key(key) else plain)[key] = value
    return plain, secret


class SecretBox:
    """Fernet encryption of small JSON objects, plus key derivation."""

    def __init__(self, key: bytes) -> None:
        """Create a box from a urlsafe-base64 Fernet key."""
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise ConfigurationError(
                f"The secret key is not a valid Fernet key; check {_KEY_VARIABLE}.",
                code="invalid_secret_key",
            ) from None
        self._key = key

    def encrypt(self, values: Mapping[str, object]) -> str:
        """Encrypt a JSON-serialisable mapping into a token string."""
        payload = json.dumps(dict(values), sort_keys=True).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, token: str) -> dict[str, object]:
        """Decrypt a token produced by :meth:`encrypt`."""
        try:
            payload = self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, ValueError):
            raise ConfigurationError(
                "Stored secrets cannot be decrypted with the current secret key; "
                f"check {_KEY_VARIABLE}.",
                code="secret_decryption_failed",
            ) from None
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ConfigurationError(
                f"Stored secrets are malformed; check {_KEY_VARIABLE}.",
                code="secret_decryption_failed",
            )
        return decoded

    def derive(self, purpose: bytes) -> bytes:
        """Derive a purpose-specific 32-byte key (HMAC-SHA256 of the key)."""
        return hmac.new(self._key, purpose, hashlib.sha256).digest()

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> SecretBox:
        """Build a box from the configured key, generating a key file if unset."""
        if settings.secret_key:
            return cls(settings.secret_key.encode("utf-8"))
        path = settings.secret_key_path
        if not path.exists():
            _write_new_key_file(path)
            LOGGER.warning(
                "secret_key_generated",
                extra={"path": str(path), "hint": f"set {_KEY_VARIABLE} to override"},
            )
        return cls(path.read_bytes().strip())


def _write_new_key_file(path: Path) -> None:
    """Create the key file atomically with mode 0600; keep an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".secret-key-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(Fernet.generate_key() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        # link() fails if another process created the file first.
        with contextlib.suppress(FileExistsError):
            os.link(temp_name, path)
    finally:
        os.unlink(temp_name)
