"""One bounded local point observation; never a deployment/capacity approval."""

from __future__ import annotations

import ctypes
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import PureWindowsPath

from app.core.process_memory import read_process_rss
from app.data.dtos import NA
from app.storage.runtime_checkpoint_readiness import planning_reserve_floor_bytes


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("load_percent", ctypes.c_uint32),
                ("total_physical", ctypes.c_uint64), ("available_physical", ctypes.c_uint64),
                ("total_pagefile", ctypes.c_uint64), ("available_pagefile", ctypes.c_uint64),
                ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
                ("available_extended_virtual", ctypes.c_uint64)]


def _windows_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    api.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    api.GetSystemDirectoryW.restype = ctypes.c_uint32
    api.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    api.GetDriveTypeW.restype = ctypes.c_uint32
    return api


def _unavailable(reason: str, source: str = NA):
    return {"status": "Unverified", "source": source, "value": NA, "reason": reason}


def _memory(api):
    value = _MemoryStatus()
    value.length = ctypes.sizeof(value)
    if not api.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise OSError("GlobalMemoryStatusEx failed")
    if not (0 < value.total_physical and value.available_physical <= value.total_physical and value.load_percent <= 100):
        raise ValueError("invalid physical memory observation")
    return {"status": "OBSERVED", "source": "windows:GlobalMemoryStatusEx",
            "total_bytes": int(value.total_physical), "available_bytes": int(value.available_physical),
            "load_percent": int(value.load_percent), "safety_verdict": "Unverified"}


def _system_disk(api):
    buffer = ctypes.create_unicode_buffer(512)
    length = api.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ValueError("system directory unavailable or exceeds bound")
    drive = PureWindowsPath(buffer.value).drive
    if re.fullmatch(r"[A-Za-z]:", drive) is None:
        raise ValueError("system directory is not a drive-letter path")
    root = drive.upper() + "\\"
    # Never stat a remote/unknown/removable path or any configured database path.
    if api.GetDriveTypeW(root) != 3:
        return _unavailable("system_drive_not_proven_fixed_local", "windows:GetDriveTypeW")
    usage = shutil.disk_usage(root)
    if not (type(usage.total) is int and type(usage.free) is int and 0 <= usage.free <= usage.total and usage.total > 0):
        raise ValueError("invalid system disk byte counts")
    floor = planning_reserve_floor_bytes(usage.total)
    return {"status": "OBSERVED", "source": "shutil.disk_usage:fixed_local_system_root",
            "root": root, "total_bytes": usage.total, "available_bytes": usage.free,
            "planning_reserve_floor_bytes": floor,
            "floor_observation": "BELOW_PLANNING_FLOOR" if usage.free < floor else "AT_OR_ABOVE_PLANNING_FLOOR",
            "growth_restore_and_volume_identity": "Unverified", "capacity_approval": False}


def observe_local_resources():
    try:
        observed_at = datetime.now(timezone.utc).isoformat()
    except Exception:
        observed_at = NA
    result = {"contract": "cci-local-resource-point-v1", "observed_at": observed_at,
              "timestamp_status": "OBSERVED" if observed_at != NA else "Unverified",
              "scope": "one_point_not_peak_or_stability_proof", "capacity_approval": False,
              "physical_memory": _unavailable("unsupported_platform"),
              "system_disk": _unavailable("unsupported_platform")}
    try:
        rss = read_process_rss()
        result["process_rss"] = {"bytes": rss.rss_bytes if rss.rss_bytes is not None else NA,
                                 "source": rss.source, "error": rss.error_code or NA}
    except Exception as exc:
        result["process_rss"] = {"bytes": NA, "source": NA, "error": type(exc).__name__}
    if sys.platform != "win32":
        return result
    try:
        api = _windows_api()
    except Exception as exc:
        result["physical_memory"] = _unavailable(type(exc).__name__)
        result["system_disk"] = _unavailable(type(exc).__name__)
        return result
    for name, collect in (("physical_memory", _memory), ("system_disk", _system_disk)):
        try:
            result[name] = collect(api)
        except Exception as exc:
            result[name] = _unavailable(type(exc).__name__)
    return result
