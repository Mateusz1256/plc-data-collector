"""Retention: delete stored readings older than the configured number of days.

Only ``tag_readings`` and ``poll_executions`` are pruned. Configuration
(connections, tag groups, tags, application settings, API keys) is never
touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from plc_data_collector.app.runtime import error_code
from plc_data_collector.app.stores import Stores

LOGGER = logging.getLogger(__name__)
DEFAULT_BATCH_SIZE = 10_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RetentionJob:
    """Prune old readings once at startup and then on a fixed interval.

    ``run()`` waits ``first_run_delay_s`` (so startup is not slowed down), then
    calls ``run_once()`` in a thread every ``interval_s``. ``trigger()`` wakes
    it early, for example right after the retention setting is saved.
    """

    def __init__(
        self,
        stores: Stores,
        *,
        clock: Callable[[], datetime] = _utc_now,
        interval_s: float = 3600.0,
        first_run_delay_s: float = 60.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        """Create a job over the given stores; nothing runs until ``run()``."""
        if interval_s <= 0:
            raise ValueError("interval_s must be positive.")
        if first_run_delay_s < 0:
            raise ValueError("first_run_delay_s must not be negative.")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1.")
        self._stores = stores
        self._clock = clock
        self._interval_s = interval_s
        self._first_run_delay_s = first_run_delay_s
        self._batch_size = batch_size
        self._wake = asyncio.Event()
        # Set when run() is cancelled, so a delete in its thread stops after
        # the current batch instead of outliving the database engine.
        self._abort = threading.Event()
        self._runs = 0

    @property
    def runs(self) -> int:
        """Return how many runs ``run()`` has finished, failed ones included."""
        return self._runs

    def trigger(self) -> None:
        """Ask ``run()`` to run now; call it on the event loop thread."""
        self._wake.set()

    async def run(self) -> None:
        """Run until cancelled; a failed run is logged and the loop goes on."""
        delay = self._first_run_delay_s
        while True:
            await self._sleep(delay)
            delay = self._interval_s
            work = asyncio.ensure_future(asyncio.to_thread(self.run_once))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                self._abort.set()
                await asyncio.wait({work})
                if not work.cancelled():
                    work.exception()
                raise
            except Exception as error:
                LOGGER.error(
                    "retention_failed",
                    extra={
                        "error_code": error_code(error),
                        "error_type": type(error).__name__,
                    },
                )
            finally:
                self._runs += 1

    def run_once(self) -> int:
        """Delete readings and poll executions older than the retention days.

        Does nothing when retention is off. Returns how many readings were
        deleted. Blocking: call it through ``asyncio.to_thread``.
        """
        settings = self._stores.settings.retention()
        if not settings.enabled:
            return 0
        cutoff = self._clock() - timedelta(days=settings.days)
        readings = self._stores.readings
        stop = self._abort.is_set
        deleted_readings = readings.delete_older_than(
            cutoff, self._batch_size, should_stop=stop
        )
        deleted_executions = readings.delete_poll_executions_older_than(
            cutoff, self._batch_size, should_stop=stop
        )
        LOGGER.info(
            "retention_deleted",
            extra={
                "days": settings.days,
                "readings": deleted_readings,
                "poll_executions": deleted_executions,
            },
        )
        return deleted_readings

    async def _sleep(self, delay: float) -> None:
        """Wait ``delay`` seconds or until ``trigger()``, whichever comes first."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        # Cleared before the run, so a trigger() during the run starts another.
        self._wake.clear()
