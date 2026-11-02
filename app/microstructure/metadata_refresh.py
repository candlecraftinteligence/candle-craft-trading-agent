"""One bounded public metadata lease maintainer per long-lived HTTP client."""
from __future__ import annotations

import asyncio
import time

from app.data.exceptions import ExchangeResponseError
from app.microstructure.stream_evidence import METADATA_MAX_AGE_SECONDS, StreamEvidence

# A genuine response by age 240 leaves 60 seconds for scheduling and recovery.
METADATA_REFRESH_AFTER_SECONDS = METADATA_MAX_AGE_SECONDS - 60
METADATA_REFRESH_TIMEOUT_SECONDS = 10


class StreamMetadataRefresher:
    def __init__(self, fetch, *, sleep=asyncio.sleep, monotonic=time.monotonic):
        self.fetch, self.sleep, self.monotonic = fetch, sleep, monotonic
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._task = None
        self._ready = asyncio.Event()
        self._services = ()
        self._snapshot = ({}, None)
        self._next_attempt = 0.0
        self._clock = None
        self._failures = 0
        self._attempts = 0
        self._renewals = 0
        self._last_error = None

    async def start(self, services, *, clock):
        # All scanner iterations register the same watch-owned services. No
        # subscription/connection ownership is transferred to this maintainer.
        async with self._lifecycle_lock:
            self._services = tuple(services)
            self._clock = clock
            self._publish()
            if self._task is None or self._task.done():
                self._ready.clear()
                self._task = asyncio.create_task(self._run(), name="stream-contract-metadata")
        # Only the owned task performs acquisition. Cancelling a scanner waiter
        # cannot cancel shared renewal; closing the HTTP owner always can.
        await self._ready.wait()
        self._publish()

    async def stop(self):
        async with self._lifecycle_lock:
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # Restart must check the source cache, not revive an old service lease.
            self._next_attempt = 0.0

    def _publish(self):
        health = {"attempts": self._attempts, "renewals": self._renewals,
                  "consecutive_failures": self._failures, "last_error": self._last_error,
                  "refresh_after_seconds": METADATA_REFRESH_AFTER_SECONDS,
                  "timeout_seconds": METADATA_REFRESH_TIMEOUT_SECONDS}
        for service in self._services:
            service.evidence.metadata_refresh = dict(health)
            if self._snapshot is not None:
                service.evidence.set_metadata(*self._snapshot)

    async def refresh(self):
        async with self._lock:
            if self.monotonic() < self._next_attempt:
                return
            self._attempts += 1
            try:
                async with asyncio.timeout(METADATA_REFRESH_TIMEOUT_SECONDS):
                    snapshot = await self.fetch(capture_clock=self._clock,
                        max_age_seconds=METADATA_REFRESH_AFTER_SECONDS)
                payload, acquired = snapshot
                self._snapshot = (payload, acquired)
                age = (self._clock() - acquired).total_seconds()
                if not 0 <= age < METADATA_REFRESH_AFTER_SECONDS:
                    raise ValueError("metadata_refresh_did_not_acquire_current_source")
                validation = StreamEvidence(self._clock)
                validation.set_metadata(payload, acquired)
                if validation.metadata_issue:
                    raise ValueError(validation.metadata_issue)
                self._renewals += 1
                self._failures, self._last_error = 0, None
                self._next_attempt = self.monotonic() + METADATA_REFRESH_AFTER_SECONDS - age
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, ExchangeResponseError):
                    self._snapshot = ({}, None)
                self._failures += 1
                self._last_error = type(exc).__name__
                delay = min(5 * 2 ** min(self._failures - 1, 3), 30)
                # The client also enforces the provider's Retry-After cooldown.
                self._next_attempt = self.monotonic() + delay
            self._publish()

    async def _run(self):
        try:
            while True:
                await self.refresh()
                self._ready.set()
                await self.sleep(max(0.01, self._next_attempt - self.monotonic()))
        finally:
            self._ready.set()
