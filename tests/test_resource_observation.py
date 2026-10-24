from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from app.core import resource_observation as resources
from app.storage.runtime_checkpoint_readiness import planning_reserve_floor_bytes


class Api:
    def __init__(self, *, drive=3, available=2 * 1024**3, memory_ok=True, directory=r"C:\Windows\System32"):
        self.drive, self.available, self.memory_ok, self.directory = drive, available, memory_ok, directory
    def GlobalMemoryStatusEx(self, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(resources._MemoryStatus)).contents
        assert value.length == 64
        value.total_physical, value.available_physical, value.load_percent = 4 * 1024**3, self.available, 50
        return self.memory_ok
    def GetSystemDirectoryW(self, buffer, size):
        buffer.value = self.directory
        return len(self.directory)
    def GetDriveTypeW(self, root):
        assert root == "C:\\"
        return self.drive


def _install(monkeypatch, api):
    monkeypatch.setattr(resources.sys, "platform", "win32")
    monkeypatch.setattr(resources, "_windows_api", lambda: api)
    monkeypatch.setattr(resources, "read_process_rss", lambda: SimpleNamespace(rss_bytes=123, source="synthetic", error_code=None))


def test_windows_memory_layout_and_reused_disk_floor_do_not_claim_capacity_approval(monkeypatch):
    _install(monkeypatch, Api())
    roots = []
    def usage(root):
        roots.append(root)
        return SimpleNamespace(total=100 * 1024**3, free=1024**3)
    monkeypatch.setattr(resources.shutil, "disk_usage", usage)
    result = resources.observe_local_resources()
    assert ctypes.sizeof(resources._MemoryStatus) == 64
    assert result["physical_memory"]["available_bytes"] == 2 * 1024**3
    disk = result["system_disk"]
    assert disk["planning_reserve_floor_bytes"] == planning_reserve_floor_bytes(100 * 1024**3)
    assert disk["floor_observation"] == "BELOW_PLANNING_FLOOR"
    assert roots == ["C:\\"]
    assert disk["capacity_approval"] is result["capacity_approval"] is False
    assert result["physical_memory"]["safety_verdict"] == "Unverified"
    assert result["process_rss"]["bytes"] == 123


@pytest.mark.parametrize("drive", [0, 1, 2, 4, 5, 6])
def test_unproven_fixed_system_drive_is_never_statted(monkeypatch, drive):
    _install(monkeypatch, Api(drive=drive))
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda root: pytest.fail("unproven drive accessed"))
    result = resources.observe_local_resources()
    assert result["system_disk"]["status"] == "Unverified"
    assert result["system_disk"]["value"] == "N/A"


@pytest.mark.parametrize("api", [Api(memory_ok=False), Api(available=5 * 1024**3)], ids=["failed", "invalid"])
def test_unavailable_memory_is_independent_of_valid_disk(monkeypatch, api):
    _install(monkeypatch, api)
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda root: SimpleNamespace(total=100 * 1024**3, free=50 * 1024**3))
    result = resources.observe_local_resources()
    assert result["physical_memory"]["value"] == "N/A"
    assert result["system_disk"]["floor_observation"] == "AT_OR_ABOVE_PLANNING_FLOOR"
    assert result["system_disk"]["capacity_approval"] is False


@pytest.mark.parametrize("directory", [r"\\host\share\Windows", "relative", ""])
def test_invalid_system_directory_fails_before_drive_or_disk_access(monkeypatch, directory):
    _install(monkeypatch, Api(directory=directory))
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda root: pytest.fail("invalid root accessed"))
    assert resources.observe_local_resources()["system_disk"]["value"] == "N/A"


def test_unsupported_platform_and_api_failure_are_explicit(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "unsupported")
    monkeypatch.setattr(resources, "_windows_api", lambda: pytest.fail("unsupported API called"))
    assert resources.observe_local_resources()["physical_memory"]["value"] == "N/A"
    monkeypatch.setattr(resources.sys, "platform", "win32")
    def fail():
        raise OSError("unavailable")
    monkeypatch.setattr(resources, "_windows_api", fail)
    monkeypatch.setattr(resources, "read_process_rss", fail)
    result = resources.observe_local_resources()
    assert result["process_rss"]["bytes"] == "N/A"
    assert result["physical_memory"]["value"] == result["system_disk"]["value"] == "N/A"


@pytest.mark.parametrize("total,free", [(0, 0), (100, 101), (100, -1), (True, 0)])
def test_bad_disk_responses_are_unavailable(monkeypatch, total, free):
    _install(monkeypatch, Api())
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda root: SimpleNamespace(total=total, free=free))
    assert resources.observe_local_resources()["system_disk"]["value"] == "N/A"


def test_timestamp_failure_preserves_other_readings(monkeypatch):
    _install(monkeypatch, Api())
    class BrokenTimestamp:
        @staticmethod
        def now(*args):
            raise OSError("synthetic clock failure")
    monkeypatch.setattr(resources, "datetime", BrokenTimestamp)
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda root: SimpleNamespace(total=100 * 1024**3, free=50 * 1024**3))
    result = resources.observe_local_resources()
    assert result["observed_at"] == "N/A" and result["timestamp_status"] == "Unverified"
    assert result["physical_memory"]["available_bytes"] == 2 * 1024**3
    assert result["system_disk"]["status"] == "OBSERVED"
