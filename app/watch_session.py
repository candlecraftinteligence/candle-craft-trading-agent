"""Cooperating watch-process lease and bounded durable session observations.

No database access, process killing, restart policy, or market-data acquisition.
The OS lock is authority; timestamps and PIDs are observations only.
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from app.data.dtos import NA
from app.watch_supervisor import FatalWatchIterationError

CONTRACT = "cci-watch-session-observation-v1"
MAX_RECEIPT_BYTES = 32768
MAX_HEADER_BYTES = 256
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
JOURNAL_TERMINAL_RESERVE_BYTES = 256
_WINDOWS_REPLACE_RETRY = os.name == "nt"
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08)
_REPLACE_RETRY_BUDGET_SECONDS = 0.25


class WatchSessionError(FatalWatchIterationError):
    pass


def _replace_receipt(temporary: Path, receipt: Path) -> None:
    """Retry only Windows access/sharing failures of the same atomic rename.

    The caller keeps its lease and the already-fsynced temporary file. Both
    attempt count and retry admission time are bounded; an OS call itself is
    synchronous and cannot be given a hard completion deadline here.
    """
    deadline = time.monotonic() + _REPLACE_RETRY_BUDGET_SECONDS
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(temporary, receipt)
            return
        except PermissionError as exc:
            if (not _WINDOWS_REPLACE_RETRY or getattr(exc, "winerror", None) not in {5, 32}
                    or attempt == len(_REPLACE_RETRY_DELAYS)):
                raise
            delay = _REPLACE_RETRY_DELAYS[attempt]
            if time.monotonic() + delay >= deadline:
                raise
            time.sleep(delay)
            if time.monotonic() >= deadline:
                raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("session timestamps require a timezone")
    return parsed


def _lock(handle: BinaryIO, *, release: bool = False) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


def append_session_observation(database_path: str | Path, row: Mapping[str, Any]) -> dict[str, Any]:
    """Under the caller's lease, stop at the total budget without deleting history."""
    path = Path(f"{Path(database_path).resolve()}.watch-observations.jsonl")
    header = {"contract": CONTRACT, "database_id": row["database_id"], "record_kind": "journal_header"}
    terminal = json.dumps({"contract": CONTRACT, "database_id": row["database_id"],
                           "record_kind": "watch_journal_budget_exhausted", "scan_completion_record": False}, sort_keys=True).encode("utf-8") + b"\n"
    if len(terminal) > JOURNAL_TERMINAL_RESERVE_BYTES:
        raise WatchSessionError("journal terminal marker exceeds reserved bound")
    suspended = row.get("journal_recording_suspended") is True
    established = suspended or row.get("journal_previously_established") is True
    payload = json.dumps({"contract": CONTRACT, **{key: value for key, value in row.items() if key not in {"journal_recording_suspended", "journal_previously_established"}}}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_RECEIPT_BYTES:
        raise WatchSessionError("watch journal record exceeds bound")
    if path.is_symlink():
        raise WatchSessionError("watch journal must not be a symlink")
    if not established:
        try:
            with path.open("xb") as handle:
                handle.write(json.dumps(header, sort_keys=True).encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
    # Even a suspended journal must remain recognized and have a complete tail.
    # r+b never recreates a missing file or resets a prior exhaustion latch.
    try:
        handle = path.open("r+b")
    except OSError as exc:
        raise WatchSessionError(f"watch journal unavailable: {type(exc).__name__}; refusing append") from exc
    with handle:
        try:
            stored_header = handle.readline(MAX_HEADER_BYTES + 1)
            if len(stored_header) > MAX_HEADER_BYTES or json.loads(stored_header) != header:
                raise ValueError("unknown journal header")
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                raise ValueError("incomplete journal tail")
        except (OSError, ValueError, TypeError) as exc:
            raise WatchSessionError(f"unknown or corrupt watch journal: {type(exc).__name__}; refusing append") from exc
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - JOURNAL_TERMINAL_RESERVE_BYTES - 1))
        tail = handle.read(JOURNAL_TERMINAL_RESERVE_BYTES + 1)
        terminal_present = tail.splitlines(keepends=True)[-1] == terminal
        near_cap = size + JOURNAL_TERMINAL_RESERVE_BYTES >= MAX_JOURNAL_BYTES
        if suspended or terminal_present or near_cap or size + len(payload) + 1 + JOURNAL_TERMINAL_RESERVE_BYTES > MAX_JOURNAL_BYTES:
            if not terminal_present and size + len(terminal) <= MAX_JOURNAL_BYTES:
                handle.seek(0, os.SEEK_END)
                handle.write(terminal)
                handle.flush()
                os.fsync(handle.fileno())
                size += len(terminal)
                terminal_present = True
            return {"status": "BUDGET_EXHAUSTED", "row_appended": False,
                    "terminal_marker_present": terminal_present,
                    "bytes": size, "limit_bytes": MAX_JOURNAL_BYTES}
        handle.seek(0, os.SEEK_END)
        handle.write(payload + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
        return {"status": "APPENDED", "row_appended": True,
                "terminal_marker_present": False,
                "bytes": size + len(payload) + 1, "limit_bytes": MAX_JOURNAL_BYTES}


class WatchSession:
    def __init__(
        self, database_path: str | Path, *, interval_seconds: float,
        on_observation: Callable[[Mapping[str, Any]], Any] | None = None,
        on_advisory: Callable[[str], None] | None = None,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        identity = os.path.normcase(str(self.database_path))
        self.database_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.lock_path = Path(f"{self.database_path}.watch.lock")
        self.receipt_path = Path(f"{self.database_path}.watch-session.json")
        self.interval_seconds = float(interval_seconds)
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("watch interval must be positive")
        self._now = now
        self._on_observation = on_observation
        self._on_advisory = on_advisory
        self._budget_advisory_emitted = False
        self._handle: BinaryIO | None = None
        self._lease_acquired = False
        self.state: dict[str, Any] = {}
        self.shutdown_outcome = "COMPLETED"

    def __enter__(self) -> WatchSession:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            if self.lock_path.is_symlink() or self.receipt_path.is_symlink():
                raise WatchSessionError("watch observation sidecars must not be symlinks")
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            self._handle = os.fdopen(descriptor, "r+b", buffering=0)
            try:
                _lock(self._handle)
                self._lease_acquired = True
            except OSError as exc:
                raise WatchSessionError(f"watch lease unavailable: {type(exc).__name__}; no watch startup authorized") from exc
            expected = f"{CONTRACT}\n{self.database_id}\n".encode("ascii")
            header = self._handle.read(MAX_HEADER_BYTES + 1)
            if not header:
                # Initialize an empty reserved lease marker only after acquiring
                # its OS lock. Concurrent first starts cannot strand an empty
                # marker by both rejecting a placeholder created by the other.
                self._handle.seek(0)
                self._handle.write(expected)
                os.fsync(self._handle.fileno())
            elif header != expected:
                raise WatchSessionError("unknown or corrupt watch lease header; refusing overwrite")
            prior = self._read_prior()
            started = self._now()
            _timestamp(started)
            self.state = {
                "contract": CONTRACT, "database_id": self.database_id,
                "session_id": uuid4().hex, "pid": os.getpid(),
                "session_started_at": started, "interval_seconds": self.interval_seconds,
                "stage": "STARTING", "latest_iteration": {},
                "shutdown_finalization_observed_at": NA, "shutdown_outcome": NA,
                "process_exit_cause": "Unverified", "previous_session": {},
                "journal_established": False,
            }
            if prior is not None:
                journal = prior.get("observation_journal", {})
                self.state["journal_established"] = prior.get(
                    "journal_established", journal.get("status") in {"APPENDED", "BUDGET_EXHAUSTED"}
                    or "observation_journal" not in prior,
                )
                if journal.get("status") == "BUDGET_EXHAUSTED":
                    self.state["observation_journal"] = {**journal, "omitted_this_session": 0}
                compact = {key: prior[key] for key in (
                    "session_id", "pid", "session_started_at", "shutdown_finalization_observed_at",
                    "shutdown_outcome", "stage", "latest_iteration",
                )}
                self.state["previous_session"] = compact
                boundary = (prior["latest_iteration"].get("finalization_observed_at")
                            or prior["latest_iteration"].get("actual_start") or prior["session_started_at"])
                elapsed = (_timestamp(started) - _timestamp(boundary)).total_seconds()
                discontinuity = {
                    "record_kind": "watch_session_boundary_observation",
                    "scan_completion_record": False, "database_id": self.database_id,
                    "session_id": self.state["session_id"], "previous_session": compact,
                    "prior_boundary_observed_at": boundary, "session_started_at": started,
                    "wall_clock_elapsed_seconds": elapsed if elapsed >= 0 else NA,
                    "clock_and_coverage_status": "Unverified", "missed_interval_count": NA,
                    "prior_shutdown_unobserved": prior["shutdown_finalization_observed_at"] == NA,
                    "prior_iteration_completion_unobserved": bool(prior["latest_iteration"]) and
                        prior["latest_iteration"].get("completion_observed") is not True,
                    "cause": "Unverified",
                }
                self._observe(discontinuity)
            self._observe({
                    "record_kind": "watch_session_start_observation", "scan_completion_record": False,
                    "database_id": self.database_id, "session_id": self.state["session_id"],
                    "session_started_at": started, "pid": self.state["pid"],
            })
            self._write()
            return self
        except BaseException as original:
            try:
                self._release()
            except Exception as release_error:
                raise WatchSessionError(f"{type(original).__name__}: {original}; {release_error}") from original
            raise

    def _observe(self, row: Mapping[str, Any]) -> None:
        if self._on_observation is None:
            if self.state.get("observation_journal", {}).get("status") != "BUDGET_EXHAUSTED":
                self.state["observation_journal"] = {"status": "NOT_CONFIGURED", "coverage_complete": False}
            return
        prior = self.state.get("observation_journal", {})
        result = self._on_observation({
            **row, "journal_recording_suspended": prior.get("status") == "BUDGET_EXHAUSTED",
            "journal_previously_established": self.state.get("journal_established", False),
        })
        if isinstance(result, dict) and result.get("status") in {"APPENDED", "BUDGET_EXHAUSTED"}:
            self.state["journal_established"] = True
            omitted = int(prior.get("omitted_this_session", 0))
            total_omitted = int(prior.get("omitted_record_count", 0))
            if result["status"] == "BUDGET_EXHAUSTED":
                omitted = min(omitted + 1, 2**63 - 1)
                total_omitted = min(total_omitted + 1, 2**63 - 1)
            self.state["observation_journal"] = {
                **result, "coverage_complete": result["row_appended"] and total_omitted == 0,
                "omitted_this_session": omitted, "omitted_record_count": total_omitted,
                "omitted_count_scope": "observed_lower_bound_abrupt_exit_may_precede_receipt",
                "advisory_status": prior.get("advisory_status", "NOT_REQUESTED"),
                "coverage_scope": "recording_budget_only_not_scan_or_historical_integrity",
                "first_omitted_at": (prior["first_omitted_at"] if prior.get("omitted_record_count") else self._now()) if total_omitted else NA,
            }
            if total_omitted and not self._budget_advisory_emitted:
                self._budget_advisory_emitted = True
                try:
                    if self._on_advisory is not None:
                        self._on_advisory("Watch observation journal budget exhausted; history coverage incomplete. Current receipt continues; evidence retained. Operator archival requires separate approval.")
                        self.state["observation_journal"]["advisory_status"] = "EMITTED"
                except Exception:
                    self.state["observation_journal"]["advisory_status"] = "Unverified"
        else:
            if prior.get("status") != "BUDGET_EXHAUSTED":
                self.state["observation_journal"] = {"status": "CALLBACK_UNATTESTED", "coverage_complete": False}

    def _read_prior(self) -> dict[str, Any] | None:
        if not self.receipt_path.exists():
            return None
        try:
            with self.receipt_path.open("rb") as handle:
                raw = handle.read(MAX_RECEIPT_BYTES + 1)
            if len(raw) > MAX_RECEIPT_BYTES:
                raise ValueError("receipt exceeds bound")
            prior = json.loads(raw)
            if prior["contract"] != CONTRACT or prior["database_id"] != self.database_id:
                raise ValueError("unknown contract/database")
            if re.fullmatch(r"[0-9a-f]{32}", prior["session_id"]) is None:
                raise ValueError("invalid session identity")
            if type(prior["pid"]) is not int or prior["pid"] < 1:
                raise ValueError("invalid process observation")
            _timestamp(prior["session_started_at"])
            if prior["shutdown_finalization_observed_at"] != NA:
                _timestamp(prior["shutdown_finalization_observed_at"])
            if prior["stage"] not in {"STARTING", "RUNNING", "FINALIZED"}:
                raise ValueError("unknown stage")
            if not isinstance(prior["latest_iteration"], dict) or "shutdown_outcome" not in prior:
                raise ValueError("missing observation fields")
            latest = prior["latest_iteration"]
            closed = prior["shutdown_finalization_observed_at"] != NA
            if (prior["stage"] == "FINALIZED") != closed:
                raise ValueError("inconsistent shutdown observation")
            allowed_outcomes = {"COMPLETED", "CANCELLED", "STARTUP_FAILED", "FAILED"}
            if (closed and prior["shutdown_outcome"] not in allowed_outcomes) or (not closed and prior["shutdown_outcome"] != NA):
                raise ValueError("unknown shutdown outcome")
            if prior["stage"] == "RUNNING" and not latest:
                raise ValueError("missing running iteration")
            if latest:
                if type(latest["iteration"]) is not int or latest["iteration"] < 1 or type(latest["completion_observed"]) is not bool:
                    raise ValueError("invalid iteration observation")
                _timestamp(latest["actual_start"])
                _timestamp(latest["scheduled_start"])
                if latest["completion_observed"]:
                    _timestamp(latest["finalization_observed_at"])
            journal = prior.get("observation_journal")
            if "journal_established" in prior and type(prior["journal_established"]) is not bool:
                raise ValueError("invalid journal ownership observation")
            if journal is not None:
                if not isinstance(journal, dict) or journal.get("status") not in {"APPENDED", "BUDGET_EXHAUSTED", "NOT_CONFIGURED", "CALLBACK_UNATTESTED"}:
                    raise ValueError("unknown journal observation")
                if journal["status"] in {"APPENDED", "BUDGET_EXHAUSTED"}:
                    if prior.get("journal_established") is False:
                        raise ValueError("inconsistent established journal observation")
                    for field in ("bytes", "limit_bytes", "omitted_record_count", "omitted_this_session"):
                        if type(journal[field]) is not int or not 0 <= journal[field] <= 2**63 - 1:
                            raise ValueError("invalid journal count")
                    exhausted = journal["status"] == "BUDGET_EXHAUSTED"
                    if journal.get("coverage_complete") is not (not exhausted) or bool(journal["omitted_record_count"]) != exhausted:
                        raise ValueError("inconsistent journal coverage")
                    if exhausted:
                        _timestamp(journal["first_omitted_at"])
            return prior
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise WatchSessionError(f"unknown or corrupt watch receipt: {type(exc).__name__}; refusing overwrite") from exc

    def _write(self) -> None:
        payload = json.dumps(self.state, sort_keys=True, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RECEIPT_BYTES:
            raise WatchSessionError("watch receipt exceeds bounded contract")
        temporary = self.receipt_path.with_name(f"{self.receipt_path.name}.{uuid4().hex}.tmp")
        created = False
        original: BaseException | None = None
        try:
            with temporary.open("xb") as handle:
                created = True
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_receipt(temporary, self.receipt_path)
        except BaseException as exc:
            original = exc
            raise
        finally:
            # Only this invocation's exclusively created temporary path is disposable.
            if created:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    detail = f"{type(original).__name__}: {original}; " if original is not None else ""
                    raise WatchSessionError(f"{detail}temporary cleanup failed: {cleanup_error}") from original or cleanup_error

    def start_iteration(self, *, iteration: int, actual_start: str, scheduled_start: str) -> None:
        _timestamp(actual_start)
        _timestamp(scheduled_start)
        self.state["stage"] = "RUNNING"
        self.state["latest_iteration"] = {
            "iteration": iteration, "actual_start": actual_start, "scheduled_start": scheduled_start,
            "completion_observed": False, "status": "STARTED",
        }
        self._write()

    def finish_iteration(
        self, *, status: str, phases: Mapping[str, str], schedule: Any, iteration_id: str = NA,
        consecutive_failures: int = 0, backoff_seconds: float = 0, will_continue: bool = True,
        phase_profile: Mapping[str, Any] | None = None,
        resource_observation: Mapping[str, Any] | None = None,
        retry_policy: Mapping[str, Any] | None = None,
    ) -> None:
        self.state["latest_iteration"].update({
            "completion_observed": True, "status": status,
            "iteration_id": iteration_id,
            "finalization_observed_at": self._now(),
            "duration_scope": "through_manifest_reporting_and_presentation_before_receipt_write",
            "duration_seconds": schedule.duration_seconds,
            "cadence_lag_seconds": schedule.cadence_lag_seconds,
            "missed_interval_count": schedule.missed_interval_count,
            "overrun_seconds": schedule.overrun_seconds,
            "phase_statuses": dict(phases),
            "consecutive_failure_count": consecutive_failures,
            "selected_backoff_seconds": backoff_seconds,
            "next_attempt_requested": will_continue,
            "sleep_seconds_at_observation": schedule.sleep_seconds if will_continue else NA,
        })
        if phase_profile is not None:
            self.state["latest_iteration"]["phase_profile"] = dict(phase_profile)
        if resource_observation is not None:
            self.state["latest_iteration"]["resource_observation"] = dict(resource_observation)
        if retry_policy is not None:
            self.state["latest_iteration"].update(retry_policy)
        self._observe({
                "record_kind": "watch_iteration_finalization_observation",
                "scan_completion_record": False,
                "database_id": self.database_id, "session_id": self.state["session_id"],
                **self.state["latest_iteration"],
        })
        self._write()

    def _release(self) -> None:
        errors = []
        if self._handle is not None:
            try:
                if self._lease_acquired:
                    _lock(self._handle, release=True)
            except OSError as exc:
                errors.append(f"unlock: {exc}")
            finally:
                try:
                    self._handle.close()
                except OSError as exc:
                    errors.append(f"close: {exc}")
                finally:
                    self._handle = None
                    self._lease_acquired = False
        if errors:
            raise WatchSessionError("lease release failed: " + "; ".join(errors))

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        errors = []
        cause = exc
        try:
            self.state["stage"] = "FINALIZED"
            self.state["shutdown_finalization_observed_at"] = self._now()
            self.state["shutdown_outcome"] = (
                "CANCELLED" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
                else "STARTUP_FAILED" if exc is not None and not self.state["latest_iteration"]
                else "FAILED" if exc is not None else self.shutdown_outcome
            )
            self.state["failure_type"] = type(exc).__name__ if exc is not None else NA
            self._write()
        except BaseException as observation_error:
            errors.append(f"shutdown observation failed: {type(observation_error).__name__}: {observation_error}")
            cause = cause or observation_error
        try:
            self._release()
        except Exception as release_error:
            errors.append(str(release_error))
            cause = cause or release_error
        if errors:
            original = f"{type(exc).__name__}: {exc}; " if exc is not None else ""
            raise WatchSessionError(original + "; ".join(errors)) from cause
        return False
