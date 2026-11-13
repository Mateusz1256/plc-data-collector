"""Per-application API keys for the data API.

Only the SHA-256 hash of a key is stored; the plaintext exists once, in the
return value of ``create``. Keys are 192 random bits, so an unsalted fast hash
is enough and lets ``authenticate`` look a key up through the unique index.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, and_, select, update

from plc_data_collector.domain import ConfigurationError
from plc_data_collector.persistence.schema import api_keys

KEY_PREFIX = "pdc_"
KEY_LENGTH = len(KEY_PREFIX) + 48
SHOWN_PREFIX_LENGTH = 8
MAX_NAME_LENGTH = 128
LAST_USED_INTERVAL = timedelta(seconds=60)
# Upper bound for the in-memory last-used map; revoked keys are dropped first.
MAX_TRACKED_KEYS = 1024


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    """An API key as shown in lists; never carries the key itself."""

    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


def hash_key(key: str) -> str:
    """Return the hex SHA-256 of a key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _info(row: object) -> ApiKeyInfo:
    values = row._mapping  # type: ignore[attr-defined]
    return ApiKeyInfo(
        id=values["id"],
        name=values["name"],
        prefix=values["prefix"],
        created_at=values["created_at"],
        last_used_at=values["last_used_at"],
        revoked_at=values["revoked_at"],
    )


class ApiKeyStore:
    """Create, list, revoke and verify API keys."""

    def __init__(
        self, engine: Engine, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        """Create a store; ``clock`` returns the current UTC time."""
        self._engine = engine
        self._clock = clock
        self._lock = threading.Lock()
        # key id -> last time last_used_at was written; one entry per key.
        self._last_written: dict[str, datetime] = {}

    def create(self, name: str) -> tuple[ApiKeyInfo, str]:
        """Create a key and return it with its plaintext, which is not stored."""
        name = name.strip()
        if not 1 <= len(name) <= MAX_NAME_LENGTH:
            raise ConfigurationError(
                "The application name must be 1 to 128 characters.",
                code="invalid_api_key_name",
            )
        key = KEY_PREFIX + secrets.token_hex(24)
        info = ApiKeyInfo(
            id=str(uuid.uuid4()),
            name=name,
            prefix=key[:SHOWN_PREFIX_LENGTH],
            created_at=self._clock(),
            last_used_at=None,
            revoked_at=None,
        )
        with self._engine.begin() as connection:
            taken = connection.execute(
                select(api_keys.c.id).where(
                    and_(api_keys.c.name == name, api_keys.c.revoked_at.is_(None))
                )
            ).first()
            if taken is not None:
                raise ConfigurationError(
                    f"An active API key named {name} already exists.",
                    code="duplicate_api_key_name",
                )
            connection.execute(
                api_keys.insert().values(
                    id=info.id,
                    name=info.name,
                    prefix=info.prefix,
                    key_hash=hash_key(key),
                    created_at=info.created_at,
                    last_used_at=None,
                    revoked_at=None,
                )
            )
        return info, key

    def list(self) -> list[ApiKeyInfo]:
        """Return all keys, newest first, revoked ones included."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(api_keys).order_by(
                    api_keys.c.created_at.desc(), api_keys.c.id.desc()
                )
            ).all()
        return [_info(row) for row in rows]

    def revoke(self, key_id: str) -> None:
        """Revoke a key; revoking an unknown or revoked key does nothing."""
        with self._engine.begin() as connection:
            connection.execute(
                update(api_keys)
                .where(and_(api_keys.c.id == key_id, api_keys.c.revoked_at.is_(None)))
                .values(revoked_at=self._clock())
            )
        with self._lock:
            self._last_written.pop(key_id, None)

    def authenticate(self, key: str) -> ApiKeyInfo | None:
        """Return the active key matching ``key``, or None."""
        if len(key) != KEY_LENGTH or not key.startswith(KEY_PREFIX):
            return None
        with self._engine.connect() as connection:
            row = connection.execute(
                select(api_keys).where(api_keys.c.key_hash == hash_key(key))
            ).first()
        if row is None:
            return None
        info = _info(row)
        if info.revoked_at is not None:
            return None
        return self._touch(info)

    def _touch(self, info: ApiKeyInfo) -> ApiKeyInfo:
        now = self._clock()
        with self._lock:
            last = self._last_written.get(info.id)
            if last is not None and now - last < LAST_USED_INTERVAL:
                return info
            self._last_written[info.id] = now
            self._prune()
        with self._engine.begin() as connection:
            connection.execute(
                update(api_keys)
                .where(api_keys.c.id == info.id)
                .values(last_used_at=now)
            )
        return ApiKeyInfo(
            id=info.id,
            name=info.name,
            prefix=info.prefix,
            created_at=info.created_at,
            last_used_at=now,
            revoked_at=None,
        )

    def _prune(self) -> None:
        """Keep the map bounded (caller holds the lock): drop the oldest entries."""
        excess = len(self._last_written) - MAX_TRACKED_KEYS
        if excess <= 0:
            return
        oldest = sorted(self._last_written, key=self._last_written.__getitem__)
        for key_id in oldest[:excess]:
            del self._last_written[key_id]
