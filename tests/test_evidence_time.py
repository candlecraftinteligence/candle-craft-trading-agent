from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.analytics.evidence_time import (
    EvidenceTimestampError,
    parse_aware_utc_timestamp,
    try_parse_stored_timestamp,
    utc_iso,
    validate_window,
    window_contains,
)


def test_aware_offsets_normalize_to_utc() -> None:
    parsed = parse_aware_utc_timestamp("2026-09-01T02:00:00+02:00")
    assert parsed == datetime(2026, 9, 1, tzinfo=UTC)
    assert utc_iso(parsed) == "2026-09-01T00:00:00Z"


def test_naive_iso_is_rejected() -> None:
    with pytest.raises(EvidenceTimestampError, match="naive"):
        parse_aware_utc_timestamp("2026-09-01T00:00:00")


def test_half_open_window_and_sqlite_naive_policy() -> None:
    start = parse_aware_utc_timestamp("2026-09-01T00:00:00Z")
    cutoff = parse_aware_utc_timestamp("2026-09-02T00:00:00Z")
    validate_window(start, cutoff)
    assert window_contains(start, start=start, cutoff=cutoff) is True
    assert window_contains(cutoff, start=start, cutoff=cutoff) is False
    aware, status = try_parse_stored_timestamp("2026-09-01T00:00:00+00:00")
    assert status == "aware_utc"
    assert aware is not None
    naive, naive_status = try_parse_stored_timestamp("2026-09-01T00:00:00")
    assert naive is None
    assert naive_status == "ambiguous_naive"
    sqlite_ts, sqlite_status = try_parse_stored_timestamp(
        "2026-09-01 00:00:00",
        sqlite_utc_naive_allowed=True,
    )
    assert sqlite_status == "sqlite_utc_naive"
    assert sqlite_ts == start
