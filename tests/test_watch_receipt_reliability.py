"""Bounded atomic Windows replacement, with synthetic receipts only."""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import watch_session as module
from app.watch_session import WatchSession, WatchSessionError


def denied(code):
    error = PermissionError(errno.EACCES, "synthetic Windows replacement denial")
    error.winerror = code
    return error


def clock(monkeypatch, *, oversleep=False):
    elapsed, sleeps = [0.0], []
    monkeypatch.setattr(module, "_WINDOWS_REPLACE_RETRY", True)
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])

    def sleep(delay):
        sleeps.append(delay)
        elapsed[0] += 1 if oversleep else delay

    monkeypatch.setattr(module.time, "sleep", sleep)
    return elapsed, sleeps


def seed(path):
    with WatchSession(path, interval_seconds=300) as owner:
        pass
    return owner.receipt_path, owner.receipt_path.read_bytes()


@pytest.mark.parametrize("code", [5, 32])
def test_transient_retry_uses_same_fsynced_file_and_retains_lease(tmp_path, monkeypatch, code):
    path = tmp_path / "synthetic.db"
    receipt, before = seed(path)
    _, sleeps = clock(monkeypatch)
    real = module.os.replace
    calls = []

    def replace(source, destination):
        calls.append((source, destination, source.read_bytes()))
        if len(calls) <= 2:
            assert receipt.read_bytes() == before
            with pytest.raises(WatchSessionError, match="lease unavailable"):
                with WatchSession(path, interval_seconds=300):
                    pytest.fail("retry released exclusive owner")
            raise denied(code)
        return real(source, destination)

    with monkeypatch.context() as local:
        local.setattr(module.os, "replace", replace)
        with WatchSession(path, interval_seconds=300):
            assert json.loads(receipt.read_bytes())["stage"] == "STARTING"
    assert len(calls) == 4  # Three startup attempts, one shutdown replacement.
    assert calls[0] == calls[1] == calls[2]
    assert sleeps == [0.01, 0.02]
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("code", [5, 32])
def test_exhaustion_preserves_prior_and_releases_owner(tmp_path, monkeypatch, code):
    path = tmp_path / "synthetic.db"
    receipt, before = seed(path)
    _, sleeps = clock(monkeypatch)
    calls = []
    error = denied(code)

    def replace(*args):
        calls.append(args)
        raise error

    with monkeypatch.context() as local:
        local.setattr(module.os, "replace", replace)
        failed = WatchSession(path, interval_seconds=300)
        with pytest.raises(PermissionError) as caught:
            with failed:
                pytest.fail("failed write faked startup success")
        assert caught.value is error
    assert len(calls) == 5 and len(set(calls)) == 1
    assert sleeps == [0.01, 0.02, 0.04, 0.08]
    assert failed._handle is None and not failed._lease_acquired
    assert receipt.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []
    with WatchSession(path, interval_seconds=300):
        pass


def test_retry_budget_expires_after_scheduler_delay_without_new_attempt(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    receipt, before = seed(path)
    _, sleeps = clock(monkeypatch, oversleep=True)
    calls = []

    def replace(*args):
        calls.append(args)
        raise denied(5)

    with monkeypatch.context() as local:
        local.setattr(module.os, "replace", replace)
        with pytest.raises(PermissionError):
            with WatchSession(path, interval_seconds=300):
                pass
    assert len(calls) == 1 and sleeps == [0.01]
    assert receipt.read_bytes() == before and not list(tmp_path.glob("*.tmp"))
    with WatchSession(path, interval_seconds=300):
        pass


@pytest.mark.parametrize("io_elapsed", [0.245, 0.3])
def test_slow_failed_os_call_cannot_admit_retry_past_budget(tmp_path, monkeypatch, io_elapsed):
    elapsed, sleeps = clock(monkeypatch)
    calls = []

    def replace(*args):
        calls.append(args)
        elapsed[0] = io_elapsed
        raise denied(5)

    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(PermissionError):
        module._replace_receipt(tmp_path / "new.tmp", tmp_path / "receipt.json")
    assert len(calls) == 1 and not sleeps


def test_shutdown_exhaustion_preserves_prior_and_original_failure(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    clock(monkeypatch)
    failed = WatchSession(path, interval_seconds=300)
    with monkeypatch.context() as local:
        with pytest.raises(WatchSessionError, match="original failure.*PermissionError"):
            with failed:
                before = failed.receipt_path.read_bytes()
                local.setattr(module.os, "replace", lambda *args: (_ for _ in ()).throw(denied(5)))
                raise RuntimeError("original failure")
    assert failed.receipt_path.read_bytes() == before
    assert failed._handle is None and not failed._lease_acquired
    assert not list(tmp_path.glob("*.tmp"))
    with WatchSession(path, interval_seconds=300):
        pass


@pytest.mark.parametrize("windows,code", [(False, 5), (True, 2), (True, 3), (True, 87), (True, None)])
def test_other_platforms_and_errors_are_not_retried(tmp_path, monkeypatch, windows, code):
    source, dest = tmp_path / "new.tmp", tmp_path / "receipt.json"
    source.write_bytes(b"new"); dest.write_bytes(b"old")
    _, sleeps = clock(monkeypatch)
    monkeypatch.setattr(module, "_WINDOWS_REPLACE_RETRY", windows)
    calls = []
    error = denied(code)

    def replace(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(PermissionError) as caught:
        module._replace_receipt(source, dest)
    assert caught.value is error and len(calls) == 1 and sleeps == []
    assert source.read_bytes() == b"new" and dest.read_bytes() == b"old"


def test_interrupt_during_retry_releases_lease_and_cleans_owned_temp(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    receipt, before = seed(path)
    monkeypatch.setattr(module, "_WINDOWS_REPLACE_RETRY", True)
    failed = WatchSession(path, interval_seconds=300)
    with monkeypatch.context() as local:
        local.setattr(module.os, "replace", lambda *args: (_ for _ in ()).throw(denied(5)))
        local.setattr(module.time, "sleep", lambda delay: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            with failed:
                pass
    assert receipt.read_bytes() == before and not list(tmp_path.glob("*.tmp"))
    assert failed._handle is None and not failed._lease_acquired
    with WatchSession(path, interval_seconds=300):
        pass


def holder(receipt, ready):
    # Explicit Windows handle sharing: reads/writes allowed, delete/rename denied.
    script = r'''
import ctypes as c, sys
from ctypes import wintypes as w
from pathlib import Path
k=c.WinDLL('kernel32',use_last_error=True)
k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
k.CreateFileW.restype=w.HANDLE
k.CloseHandle.argtypes=[w.HANDLE]
h=k.CreateFileW(sys.argv[1],0x80000000,3,None,3,0x80,None)
if h==w.HANDLE(-1).value: raise c.WinError(c.get_last_error())
try:
    Path(sys.argv[2]).write_text('ready')
    sys.stdin.readline()
finally: k.CloseHandle(h)
'''
    return subprocess.Popen([sys.executable, "-c", script, str(receipt), str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows CreateFile sharing semantics")
@pytest.mark.parametrize("release", [True, False], ids=["transient-reader", "persistent-reader"])
def test_independent_windows_reader_atomicity_and_ownership(tmp_path, monkeypatch, release):
    path = tmp_path / "synthetic.db"
    receipt, before = seed(path)
    ready = tmp_path / "reader-ready"
    child = holder(receipt, ready)
    real_sleep = time.sleep
    calls, sleeps = [], []
    real_replace = module.os.replace
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            real_sleep(0.005)
        assert ready.exists(), child.communicate(timeout=5)

        def replace(*args):
            calls.append(args)
            return real_replace(*args)

        def sleep(delay):
            sleeps.append(delay)
            assert receipt.read_bytes() == before
            with pytest.raises(WatchSessionError, match="lease unavailable"):
                with WatchSession(path, interval_seconds=300):
                    pytest.fail("another owner entered during retry")
            if release and child.poll() is None:
                child.communicate("release\n", timeout=5)
                assert child.returncode == 0
            real_sleep(delay)

        with monkeypatch.context() as local:
            local.setattr(module.os, "replace", replace)
            local.setattr(module.time, "sleep", sleep)
            if release:
                with WatchSession(path, interval_seconds=300):
                    assert json.loads(receipt.read_bytes())["stage"] == "STARTING"
                assert 3 <= len(calls) <= 6 and sleeps
            else:
                failed = WatchSession(path, interval_seconds=300)
                with pytest.raises(PermissionError):
                    with failed:
                        pytest.fail("persistent reader faked durable success")
                # Scheduler delay may exhaust the time budget before the count
                # cap. Fake-clock tests above prove the exact count cap.
                assert 1 <= len(calls) <= 5 and 1 <= len(sleeps) <= 4
                assert receipt.read_bytes() == before
                assert failed._handle is None and not failed._lease_acquired
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        try:
            if child.poll() is None:
                child.communicate("release\n", timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
    with WatchSession(path, interval_seconds=300):
        pass
