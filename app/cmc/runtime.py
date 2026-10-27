"""Lazy per-event-loop service. OFF does no provider/storage work."""
from __future__ import annotations

import asyncio
import logging
import time
import weakref
from datetime import UTC, datetime
from pathlib import Path

from app.cmc.client import CmcClient
from app.cmc.collection import Collector, load_manifest
from app.cmc.models import Mode, Snapshot
from app.cmc.storage import Sidecar
from app.cmc.profiles import profile_for

logger = logging.getLogger(__name__)
_services = weakref.WeakKeyDictionary()


class Service:
    def __init__(self, collector, store, *, interval=300):
        self.collector, self.store, self.interval = collector, store, interval
        self.snapshot = Snapshot()
        self.task = None
        self.next_refresh = 0
        self.closed = False

    def schedule(self):
        if not self.closed and (self.task is None or self.task.done()) and time.monotonic() >= self.next_refresh:
            self.next_refresh = time.monotonic() + self.interval
            self.task = asyncio.create_task(self._worker())

    async def _worker(self):
        try:
            while True:
                await self._refresh()
                await asyncio.sleep(self.interval)
        finally:
            await self.collector.client.aclose()

    async def _refresh(self):
        try:
            self.snapshot = await asyncio.wait_for(self.collector.collect(), timeout=30)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep previous evidence, whose timestamps still expire. Never retain an old
            # enforcement decision and never log provider exception text / credentials.
            logger.warning("CMC collection unavailable: %s", type(exc).__name__)

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.collector.client.aclose()


def _construct(settings):
    profile = profile_for(settings.cmc_collection_profile, legacy_seconds=settings.cmc_poll_seconds,
                          custom_path=settings.cmc_collection_profile_path)
    store = Sidecar(settings.cmc_sidecar_path, max_bytes=settings.cmc_storage_max_bytes,
                    max_records=settings.cmc_storage_max_records,
                    retention_days=min(settings.cmc_retention_days, profile.retention_days))
    manifest = load_manifest(settings.cmc_identity_manifest)
    return store, manifest, profile


async def service_for(settings):
    if Mode(settings.cmc_mode) == Mode.OFF:
        return None
    loop = asyncio.get_running_loop()
    key = (str(settings.cmc_sidecar_path), str(settings.cmc_identity_manifest), settings.cmc_keyless,
           settings.cmc_daily_credits, settings.cmc_monthly_credits,
           settings.cmc_collection_profile, str(settings.cmc_collection_profile_path), settings.cmc_poll_seconds,
           settings.cmc_storage_max_bytes, settings.cmc_storage_max_records, settings.cmc_retention_days)
    services = _services.setdefault(loop, {})
    if key not in services:
        if services:
            # Configuration changes require process restart; never accumulate collectors.
            return None
        try:
            store, manifest, profile = await asyncio.wait_for(asyncio.to_thread(_construct, settings), timeout=0.25)
            client = CmcClient(store=store, api_key=settings.cmc_api_key, keyless=settings.cmc_keyless,
                daily_credits=settings.cmc_daily_credits, monthly_credits=settings.cmc_monthly_credits,
                concurrency=profile.concurrency, max_entries=profile.cache_entries, max_body_bytes=profile.max_body_bytes,
                requests_per_minute=profile.requests_per_minute)
            service = Service(Collector(client, store, manifest=manifest, profile=profile), store,
                              interval=min(60, profile.tick_seconds))
            # Only completed, originally acquired batches are restored. The decision
            # layer rechecks every observation's source and acquisition timestamps.
            saved = await asyncio.wait_for(asyncio.to_thread(store.causal, "snapshot", datetime.now(UTC), limit=1), timeout=0.1)
            if saved:
                service.snapshot = Snapshot.model_validate(saved[0])
            services[key] = service
        except Exception as exc:
            logger.warning("CMC sidecar unavailable: %s", type(exc).__name__)
            return None
    return services[key]


async def begin_collection(settings=None):
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()
    service = await service_for(settings)
    if service is not None:
        service.schedule()
    return service
