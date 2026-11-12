"""Database-backed configuration with archive instead of delete."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, Row, select, update

from plc_data_collector.app.config import CollectorConfig
from plc_data_collector.app.secrets import SecretBox, merge_secrets, split_secrets
from plc_data_collector.domain import (
    ConfigurationError,
    ConnectionConfig,
    OverlapPolicy,
    TagConfig,
    TagGroupConfig,
    ValueType,
)
from plc_data_collector.persistence.app_settings import (
    CONFIG_REVISION_KEY,
    get_setting,
    set_setting,
)
from plc_data_collector.persistence.schema import (
    app_settings,
    connections,
    tag_groups,
    tags,
)
from plc_data_collector.persistence.sql import insert_for


class ConfigStore:
    """Stores Connections, Tag Groups and Tags; every write bumps the revision.

    Removed items are archived (``archived_at``), never deleted, so stored
    readings and spooled Poll Results keep their foreign keys.
    """

    def __init__(self, engine: Engine, secret_box: SecretBox) -> None:
        """Create a store over an engine whose schema is already migrated."""
        self._engine = engine
        self._box = secret_box

    # Reads

    def load_active(self) -> tuple[CollectorConfig, int]:
        """Return the non-archived configuration and its revision.

        The revision is read first, in the same connection, so a concurrent write
        can only make the data newer than the revision, never older.
        """
        with self._engine.connect() as connection:
            revision = _read_revision(connection)
            connection_rows = connection.execute(
                select(connections)
                .where(connections.c.archived_at.is_(None))
                .order_by(connections.c.id)
            ).all()
            group_rows = connection.execute(
                select(tag_groups)
                .where(tag_groups.c.archived_at.is_(None))
                .order_by(tag_groups.c.id)
            ).all()
            tag_rows = connection.execute(
                select(tags).where(tags.c.archived_at.is_(None)).order_by(tags.c.id)
            ).all()
        config = CollectorConfig(
            connections=tuple(self._connection_from_row(r) for r in connection_rows),
            tag_groups=tuple(_group_from_row(r) for r in group_rows),
            tags=tuple(_tag_from_row(r) for r in tag_rows),
        )
        return config, revision

    def revision(self) -> int:
        """Return the configuration revision; 0 means never written."""
        with self._engine.connect() as connection:
            return _read_revision(connection)

    def has_any_connection_rows(self) -> bool:
        """Return whether any Connection row exists, archived ones included."""
        with self._engine.connect() as connection:
            return (
                connection.execute(select(connections.c.id).limit(1)).first()
                is not None
            )

    def get_connection(self, connection_id: str) -> ConnectionConfig | None:
        """Return an active Connection, or None."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(connections).where(
                    connections.c.id == connection_id,
                    connections.c.archived_at.is_(None),
                )
            ).first()
        return None if row is None else self._connection_from_row(row)

    def get_tag_group(self, group_id: str) -> TagGroupConfig | None:
        """Return an active Tag Group, or None."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(tag_groups).where(
                    tag_groups.c.id == group_id, tag_groups.c.archived_at.is_(None)
                )
            ).first()
        return None if row is None else _group_from_row(row)

    def get_tag(self, tag_id: str) -> TagConfig | None:
        """Return an active Tag, or None."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(tags).where(tags.c.id == tag_id, tags.c.archived_at.is_(None))
            ).first()
        return None if row is None else _tag_from_row(row)

    def is_archived_tag(self, tag_id: str) -> bool | None:
        """Return whether the Tag is archived, or None when it is unknown."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(tags.c.archived_at).where(tags.c.id == tag_id)
            ).first()
        return None if row is None else row[0] is not None

    # Writes

    def upsert_connection(self, config: ConnectionConfig) -> None:
        """Create or update a Connection, restoring it when archived."""
        with self._engine.begin() as connection:
            revision = _lock_revision(connection)
            self._write_connection(connection, config)
            self._finish_write(connection, revision)

    def upsert_tag_group(self, group: TagGroupConfig) -> None:
        """Create or update a Tag Group; its Connection must be active."""
        with self._engine.begin() as connection:
            revision = _lock_revision(connection)
            _write_tag_group(connection, group)
            self._finish_write(connection, revision)

    def upsert_tag(self, tag: TagConfig) -> None:
        """Create or update a Tag; its Tag Group must be active."""
        with self._engine.begin() as connection:
            revision = _lock_revision(connection)
            _write_tag(connection, tag)
            self._finish_write(connection, revision)

    def archive_connection(self, connection_id: str) -> None:
        """Archive a Connection and, with it, its Tag Groups and Tags."""
        with self._engine.begin() as connection:
            revision = _lock_revision(connection)
            now = _now()
            _archive_connection(connection, connection_id, now)
            self._finish_write(connection, revision)

    def archive_tag_group(self, group_id: str) -> None:
        """Archive a Tag Group and its Tags."""
        with self._engine.begin() as connection:
            revision = _lock_revision(connection)
            _archive_group(connection, group_id, _now())
            self._finish_write(connection, revision)

    def archive_tag(self, tag_id: str) -> None:
        """Archive a Tag."""
        with self._engine.begin() as connection:
            revision = _lock_revision(connection)
            connection.execute(
                update(tags)
                .where(tags.c.id == tag_id, tags.c.archived_at.is_(None))
                .values(archived_at=_now())
            )
            self._finish_write(connection, revision)

    def replace_all(self, config: CollectorConfig) -> None:
        """Make ``config`` the active set: upsert it, archive everything else."""
        with self._engine.begin() as connection:
            revision = _lock_revision(connection)
            for item in config.connections:
                self._write_connection(connection, item)
            for group in config.tag_groups:
                _write_tag_group(connection, group)
            for tag in config.tags:
                _write_tag(connection, tag)

            now = _now()
            keep_connections = {c.id for c in config.connections}
            keep_groups = {g.id for g in config.tag_groups}
            keep_tags = {t.id for t in config.tags}
            for (row_id,) in connection.execute(
                select(connections.c.id).where(connections.c.archived_at.is_(None))
            ).all():
                if row_id not in keep_connections:
                    _archive_connection(connection, row_id, now)
            for (row_id,) in connection.execute(
                select(tag_groups.c.id).where(tag_groups.c.archived_at.is_(None))
            ).all():
                if row_id not in keep_groups:
                    _archive_group(connection, row_id, now)
            for (row_id,) in connection.execute(
                select(tags.c.id).where(tags.c.archived_at.is_(None))
            ).all():
                if row_id not in keep_tags:
                    connection.execute(
                        update(tags).where(tags.c.id == row_id).values(archived_at=now)
                    )
            self._finish_write(connection, revision)

    def encrypt_plaintext_secrets(self) -> int:
        """Move sensitive options from ``protocol_options`` into ``secret_options``.

        Handles 0.2.0 rows. Returns the number of Connections changed. This is not
        a configuration change, so the revision is not bumped.
        """
        changed = 0
        with self._engine.begin() as connection:
            rows = connection.execute(
                select(
                    connections.c.id,
                    connections.c.protocol_options,
                    connections.c.secret_options,
                )
            ).all()
            for row in rows:
                plain, found = split_secrets(row.protocol_options or {})
                if not found:
                    continue
                merged = merge_secrets(
                    self._box.decrypt(row.secret_options) if row.secret_options else {},
                    found,
                )
                connection.execute(
                    update(connections)
                    .where(connections.c.id == row.id)
                    .values(
                        protocol_options=plain,
                        secret_options=self._box.encrypt(merged),
                    )
                )
                changed += 1
        return changed

    # Internals

    def _connection_from_row(self, row: Row[tuple[object, ...]]) -> ConnectionConfig:
        options = merge_secrets(
            row.protocol_options or {},
            self._box.decrypt(row.secret_options) if row.secret_options else {},
        )
        return ConnectionConfig(
            id=row.id,
            name=row.name,
            protocol=row.protocol,
            endpoint=row.endpoint,
            enabled=row.enabled,
            timeout_ms=row.timeout_ms,
            protocol_options=options,
        )

    def _write_connection(
        self, connection: Connection, config: ConnectionConfig
    ) -> None:
        plain, secret = split_secrets(config.protocol_options)
        statement = insert_for(connection, connections).values(
            id=config.id,
            name=config.name,
            protocol=config.protocol,
            endpoint=config.endpoint,
            enabled=config.enabled,
            timeout_ms=config.timeout_ms,
            protocol_options=plain,
            secret_options=self._box.encrypt(secret) if secret else None,
            archived_at=None,
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[connections.c.id],
                set_={
                    "name": statement.excluded.name,
                    "protocol": statement.excluded.protocol,
                    "endpoint": statement.excluded.endpoint,
                    "enabled": statement.excluded.enabled,
                    "timeout_ms": statement.excluded.timeout_ms,
                    "protocol_options": statement.excluded.protocol_options,
                    "secret_options": statement.excluded.secret_options,
                    "archived_at": None,
                },
            )
        )

    def _finish_write(self, connection: Connection, revision: int) -> None:
        _validate_active_references(connection)
        set_setting(connection, CONFIG_REVISION_KEY, revision + 1)


def _lock_revision(connection: Connection) -> int:
    """Lock the revision row for this transaction and return its value.

    Called first in every write transaction. Concurrent writers queue on the row
    lock (PostgreSQL), so revisions are distinct and strictly increasing and
    reference checks never race with another write. SQLite serialises writers.
    """
    connection.execute(
        insert_for(connection, app_settings)
        .values(key=CONFIG_REVISION_KEY, value=0)
        .on_conflict_do_nothing(index_elements=[app_settings.c.key])
    )
    value = connection.execute(
        select(app_settings.c.value)
        .where(app_settings.c.key == CONFIG_REVISION_KEY)
        .with_for_update()
    ).scalar_one()
    return value if isinstance(value, int) else 0


def _now() -> datetime:
    return datetime.now(UTC)


def _read_revision(connection: Connection) -> int:
    value = get_setting(connection, CONFIG_REVISION_KEY)
    return value if isinstance(value, int) else 0


def _unknown_reference(kind: str, parent_id: str) -> ConfigurationError:
    return ConfigurationError(
        f"The {kind} '{parent_id}' does not exist or is archived.",
        code="unknown_reference",
        details={kind: parent_id},
    )


def _write_tag_group(connection: Connection, group: TagGroupConfig) -> None:
    if (
        connection.execute(
            select(connections.c.id).where(
                connections.c.id == group.connection_id,
                connections.c.archived_at.is_(None),
            )
        ).first()
        is None
    ):
        raise _unknown_reference("connection", group.connection_id)
    statement = insert_for(connection, tag_groups).values(
        id=group.id,
        connection_id=group.connection_id,
        interval_ms=group.interval_ms,
        timeout_ms=group.timeout_ms,
        overlap_policy=group.overlap_policy.value,
        enabled=group.enabled,
        archived_at=None,
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[tag_groups.c.id],
            set_={
                "connection_id": statement.excluded.connection_id,
                "interval_ms": statement.excluded.interval_ms,
                "timeout_ms": statement.excluded.timeout_ms,
                "overlap_policy": statement.excluded.overlap_policy,
                "enabled": statement.excluded.enabled,
                "archived_at": None,
            },
        )
    )


def _write_tag(connection: Connection, tag: TagConfig) -> None:
    if (
        connection.execute(
            select(tag_groups.c.id).where(
                tag_groups.c.id == tag.tag_group_id,
                tag_groups.c.archived_at.is_(None),
            )
        ).first()
        is None
    ):
        raise _unknown_reference("tag_group", tag.tag_group_id)
    statement = insert_for(connection, tags).values(
        id=tag.id,
        tag_group_id=tag.tag_group_id,
        name=tag.name,
        address=tag.address,
        value_type=tag.value_type.value,
        enabled=tag.enabled,
        metadata_json=dict(tag.metadata),
        archived_at=None,
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[tags.c.id],
            set_={
                "tag_group_id": statement.excluded.tag_group_id,
                "name": statement.excluded.name,
                "address": statement.excluded.address,
                "value_type": statement.excluded.value_type,
                "enabled": statement.excluded.enabled,
                "metadata_json": statement.excluded.metadata_json,
                "archived_at": None,
            },
        )
    )


def _archive_connection(
    connection: Connection, connection_id: str, now: datetime
) -> None:
    for (group_id,) in connection.execute(
        select(tag_groups.c.id).where(
            tag_groups.c.connection_id == connection_id,
            tag_groups.c.archived_at.is_(None),
        )
    ).all():
        _archive_group(connection, group_id, now)
    connection.execute(
        update(connections)
        .where(connections.c.id == connection_id, connections.c.archived_at.is_(None))
        .values(archived_at=now)
    )


def _archive_group(connection: Connection, group_id: str, now: datetime) -> None:
    connection.execute(
        update(tags)
        .where(tags.c.tag_group_id == group_id, tags.c.archived_at.is_(None))
        .values(archived_at=now)
    )
    connection.execute(
        update(tag_groups)
        .where(tag_groups.c.id == group_id, tag_groups.c.archived_at.is_(None))
        .values(archived_at=now)
    )


def _validate_active_references(connection: Connection) -> None:
    """Fail when an active Tag Group or Tag points at an archived or missing parent."""
    active_connections = set(
        connection.execute(
            select(connections.c.id).where(connections.c.archived_at.is_(None))
        ).scalars()
    )
    active_groups: dict[str, str] = {
        row.id: row.connection_id
        for row in connection.execute(
            select(tag_groups.c.id, tag_groups.c.connection_id).where(
                tag_groups.c.archived_at.is_(None)
            )
        )
    }
    for parent in active_groups.values():
        if parent not in active_connections:
            raise _unknown_reference("connection", parent)
    for row in connection.execute(
        select(tags.c.tag_group_id).where(tags.c.archived_at.is_(None))
    ):
        if row.tag_group_id not in active_groups:
            raise _unknown_reference("tag_group", row.tag_group_id)


def _group_from_row(row: Row[tuple[object, ...]]) -> TagGroupConfig:
    return TagGroupConfig(
        id=row.id,
        connection_id=row.connection_id,
        interval_ms=row.interval_ms,
        timeout_ms=row.timeout_ms,
        overlap_policy=OverlapPolicy(row.overlap_policy),
        enabled=row.enabled,
    )


def _tag_from_row(row: Row[tuple[object, ...]]) -> TagConfig:
    metadata: Mapping[str, object] = row.metadata_json or {}
    return TagConfig(
        id=row.id,
        tag_group_id=row.tag_group_id,
        name=row.name,
        address=row.address,
        value_type=ValueType(row.value_type),
        enabled=row.enabled,
        metadata=metadata,
    )
