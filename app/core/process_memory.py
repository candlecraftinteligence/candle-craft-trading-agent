from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


WINDOWS_MEMORY_SOURCE = "windows:GetProcessMemoryInfo"
LINUX_MEMORY_SOURCE = "linux:/proc/self/statm"
UNSUPPORTED_MEMORY_SOURCE = "N/A"


@dataclass(frozen=True)
class ProcessMemoryReading:
    rss_bytes: int | None
    source: str
    error_code: str | None = None


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def read_process_rss() -> ProcessMemoryReading:
    source = _memory_source(sys.platform)
    if source == UNSUPPORTED_MEMORY_SOURCE:
        return ProcessMemoryReading(
            rss_bytes=None,
            source=source,
            error_code="unsupported_platform",
        )

    try:
        if source == WINDOWS_MEMORY_SOURCE:
            rss_bytes = _read_windows_rss_bytes()
        else:
            rss_bytes = _read_linux_rss_bytes_from_proc()
        if isinstance(rss_bytes, bool) or rss_bytes < 0:
            raise ValueError("RSS byte count must be a non-negative integer")
    except (AttributeError, IndexError, OSError, TypeError, ValueError) as exc:
        return ProcessMemoryReading(
            rss_bytes=None,
            source=source,
            error_code=type(exc).__name__,
        )
    return ProcessMemoryReading(rss_bytes=rss_bytes, source=source)


def _memory_source(platform: str) -> str:
    if platform == "win32":
        return WINDOWS_MEMORY_SOURCE
    if platform.startswith("linux"):
        return LINUX_MEMORY_SOURCE
    return UNSUPPORTED_MEMORY_SOURCE


def _read_windows_rss_bytes() -> int:
    kernel32, psapi = _windows_memory_apis()

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process_handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(process_handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.working_set_size)


@lru_cache(maxsize=1)
def _windows_memory_apis() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    return kernel32, psapi


def _read_linux_rss_bytes_from_proc() -> int:
    statm = Path("/proc/self/statm").read_text(encoding="ascii")
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    return _linux_rss_bytes(statm, page_size=page_size)


def _linux_rss_bytes(statm: str, *, page_size: int) -> int:
    fields = statm.split()
    if len(fields) < 2:
        raise ValueError("/proc/self/statm does not contain an RSS field")
    resident_pages = int(fields[1])
    if resident_pages < 0 or page_size <= 0:
        raise ValueError("RSS page inputs must be non-negative")
    return resident_pages * page_size


__all__ = [
    "LINUX_MEMORY_SOURCE",
    "ProcessMemoryReading",
    "UNSUPPORTED_MEMORY_SOURCE",
    "WINDOWS_MEMORY_SOURCE",
    "read_process_rss",
]
