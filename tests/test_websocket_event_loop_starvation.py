"""Synthetic correlation only: no production database, scanner, or market access."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from websockets.sync.server import serve

from app.microstructure.service import BinanceWebSocketTransport
import scripts.run_scan as run_scan

pytestmark = pytest.mark.no_auto_epoch


def test_synchronous_lifecycle_boundary_delays_received_wire_frame(monkeypatch) -> None:
    blocking_started = threading.Event()
    frame_sent = threading.Event()
    processed = threading.Event()
    order = []
    result = object()

    def provider(connection):
        assert blocking_started.wait(5)
        connection.send("synthetic fixture frame")
        order.append("wire_frame_sent")
        frame_sent.set()
        assert connection.recv(timeout=5) == "fixture complete"

    def lifecycle_fixture(value, **kwargs):
        assert value is result
        order.append("synchronous_call_entered")
        blocking_started.set()
        assert frame_sent.wait(5)
        # Bytes have been sent on another thread, but the scanner's async loop
        # cannot process them while its real synchronous boundary is executing.
        assert not processed.is_set()
        order.append("synchronous_call_returned")
        return value

    monkeypatch.setattr(run_scan, "_lifecycle_enabled", lambda args: True)
    monkeypatch.setattr(run_scan, "apply_lifecycle_to_run_result", lifecycle_fixture)

    async def scenario(port):
        connection = await BinanceWebSocketTransport(url=f"ws://127.0.0.1:{port}").connect()
        async def receive():
            assert await connection.recv() == "synthetic fixture frame"
            order.append("frame_processed")
            processed.set()
        receiver = asyncio.create_task(receive())
        try:
            await asyncio.sleep(0)
            args = SimpleNamespace(database_path="unused-fixture-path")
            assert run_scan._apply_lifecycle_if_enabled(args, result, scan_run_id="fixture") is result
            await asyncio.wait_for(receiver, 5)
            await connection.send("fixture complete")
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await connection.close()

    with serve(provider, "127.0.0.1", 0, close_timeout=1) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(scenario(server.socket.getsockname()[1]))
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
    assert order == ["synchronous_call_entered", "wire_frame_sent", "synchronous_call_returned", "frame_processed"]
