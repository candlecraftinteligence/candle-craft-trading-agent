"""A blocked synthetic analysis cannot block feed tasks or queue more workers."""
import asyncio
import threading

import pytest

from app.core.cpu_analysis import AnalysisWorker

pytestmark = pytest.mark.no_auto_epoch


def test_analysis_is_off_loop_and_cancellation_preserves_single_worker_bound():
    async def exercise():
        worker = AnalysisWorker()
        started, release = threading.Event(), threading.Event()
        threads = []
        def blocked():
            threads.append(threading.get_ident())
            started.set()
            assert release.wait(5)
            return 7
        first = asyncio.create_task(worker.run(blocked))
        while not started.is_set():
            await asyncio.sleep(.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(worker.run(lambda: 9))
        await asyncio.sleep(.02)  # The event loop remains usable while analysis is blocked.
        assert not second.done() and worker.health()["active"]
        assert threads == [threads[0]] and threads[0] != threading.get_ident()
        release.set()
        assert await asyncio.wait_for(second, 2) == 9
        await asyncio.sleep(0)
        assert worker.health()["completed"] == 2
        assert worker.health()["concurrency_bound"] == 1
    asyncio.run(exercise())
