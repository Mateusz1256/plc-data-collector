"""Audit log lines for Panel actions: codes and IDs only, never secrets."""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from plc_data_collector.app.stores import Stores
from plc_data_collector.domain import StorageError

LOGGER = logging.getLogger(__name__)


def configuration_changed(stores: Stores, kind: str, item_id: str, action: str) -> None:
    """Log a saved configuration change and the revision it produced.

    Blocking: reads the revision, so call it in a thread from async code. The
    change is already saved, so a failed revision read is logged as None
    instead of failing the request.
    """
    LOGGER.info(
        "configuration_changed",
        extra={
            "kind": kind,
            "id": item_id,
            "action": action,
            "revision": _revision(stores),
        },
    )


def configuration_imported(stores: Stores) -> None:
    """Log a confirmed import and its revision; blocking like the above."""
    LOGGER.info("configuration_imported", extra={"revision": _revision(stores)})


def _revision(stores: Stores) -> int | None:
    try:
        return stores.config.revision()
    except (SQLAlchemyError, StorageError):
        return None
