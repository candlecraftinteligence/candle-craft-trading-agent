"""One active deterministic analysis per event loop; cancellation cannot queue work."""
from __future__ import annotations

import asyncio
import time
import weakref

_workers = weakref.WeakKeyDictionary()


class AnalysisWorker:
    def __init__(self):
        self.slot = asyncio.Lock()
        self.completed = 0
        self.active = False
        self.max_queue_seconds = 0.0
        self.max_work_seconds = 0.0

    async def run(self, function, *args):
        queued = time.monotonic()
        await self.slot.acquire()
        self.max_queue_seconds = max(self.max_queue_seconds, time.monotonic()-queued)
        self.active = True
        started = time.monotonic()
        task = asyncio.create_task(asyncio.to_thread(function, *args))

        def finished(completed):
            self.max_work_seconds = max(self.max_work_seconds, time.monotonic()-started)
            self.completed += 1
            self.active = False
            if not completed.cancelled():
                completed.exception()  # Observe an exception even if its waiter timed out.
            self.slot.release()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def health(self):
        return {"concurrency_bound": 1, "active": self.active, "completed": self.completed,
            "max_queue_seconds": round(self.max_queue_seconds, 6),
            "max_work_seconds": round(self.max_work_seconds, 6)}


def analysis_worker():
    loop = asyncio.get_running_loop()
    if loop not in _workers:
        _workers[loop] = AnalysisWorker()
    return _workers[loop]
