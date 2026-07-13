"""Persistence-facing records."""

from __future__ import annotations

from dataclasses import dataclass

from plc_gateway.domain import TagResult


@dataclass(frozen=True, slots=True)
class TagReadingRecord:
    """A tag reading ready for relational persistence."""

    event_id: str
    connection_id: str
    tag_group_id: str
    result: TagResult

    def __post_init__(self) -> None:
        """Validate persistence record identifiers."""
        if not self.event_id.strip():
            raise ValueError("event_id cannot be empty.")
        if not self.connection_id.strip():
            raise ValueError("connection_id cannot be empty.")
        if not self.tag_group_id.strip():
            raise ValueError("tag_group_id cannot be empty.")
