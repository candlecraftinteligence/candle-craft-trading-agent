from __future__ import annotations

from app.core import process_memory
from app.core.process_memory import (
    LINUX_MEMORY_SOURCE,
    ProcessMemoryReading,
    read_process_rss,
)


def test_linux_rss_bytes_uses_resident_pages() -> None:
    assert process_memory._linux_rss_bytes("100 25 10 2 0 5 0", page_size=4096) == 102_400


def test_process_memory_failure_is_explicit_and_non_throwing(monkeypatch) -> None:
    monkeypatch.setattr(process_memory.sys, "platform", "linux")
    monkeypatch.setattr(
        process_memory,
        "_read_linux_rss_bytes_from_proc",
        lambda: (_ for _ in ()).throw(OSError("unavailable")),
    )

    reading = read_process_rss()

    assert reading == ProcessMemoryReading(
        rss_bytes=None,
        source=LINUX_MEMORY_SOURCE,
        error_code="OSError",
    )


def test_current_process_memory_is_positive_or_explicitly_unavailable() -> None:
    reading = read_process_rss()

    if reading.rss_bytes is None:
        assert reading.error_code is not None
    else:
        assert reading.rss_bytes > 0
        assert reading.error_code is None
