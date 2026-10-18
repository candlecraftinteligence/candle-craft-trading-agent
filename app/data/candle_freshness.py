"""Essential closed-candle coverage at a scan decision cutoff.

Policy version: essential-candle-freshness-v1.

A successful fetch, cache insert, processing time, or scan id does not prove
that candles are current. For a supported fixed-duration feed anchored at the
Unix epoch in UTC, the latest required scheduled close is:

    E = A + floor((D - allowance - A) / delta) * delta

with A = 1970-01-01 00:00:00 UTC. Essential coverage passes only when the
latest closed candle's canonical scheduled close is at or after E. The
scheduled close is the candle open plus the timeframe duration. A venue close
timestamp one millisecond before that boundary is the same scheduled close.

No feed contract in this repository documents a bounded publication allowance.
Cache TTL is not an allowance. The strict default allowance is zero. A scan
whose decision cutoff falls between the scheduled close and venue publication
can reject that interval until the candle arrives. That early rejection is an
operational tradeoff of the strict rule, not a tolerance chosen from results.

Weekly intervals are unsupported: this codebase does not document a Monday
anchor for them. Synthetic 2d candles use the same Unix-epoch UTC anchor as
``resample_ohlcv_candles``. A 12h or 2d close is judged on its own delta, never
on the 15m delta.

This module does not replace ``closed_candles_as_of``. Historical prefixes and
outcome catch-up keep using that utility. Discovery callers opt into this check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from app.data.candle_integrity import (
    CandleIntegrityError,
    closed_candles_as_of,
    normalize_utc_timestamp,
    timeframe_duration,
)
from app.data.dtos import NA

POLICY_VERSION = "essential-candle-freshness-v1"
ANCHOR_UNIX_EPOCH_UTC = "unix_epoch_utc"
PUBLICATION_ALLOWANCE = timedelta(0)
FRESHNESS_TOKEN = "essential_candle_freshness"
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)
_SUPPORTED_UNITS = frozenset({"m", "h", "d"})


class CandleFreshnessStatus(str, Enum):
    PASS = "pass"
    STALE = "stale"
    MISSING = "missing"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class CandleFreshnessAssessment:
    status: CandleFreshnessStatus
    reason: str
    symbol: str
    timeframe: str
    role: str
    decision_cutoff: datetime | None
    latest_observed_close: datetime | None
    required_close: datetime | None
    lag_intervals: int | None
    policy_version: str = POLICY_VERSION
    allowance: timedelta = PUBLICATION_ALLOWANCE
    anchor: str = ANCHOR_UNIX_EPOCH_UTC

    @property
    def passed(self) -> bool:
        return self.status is CandleFreshnessStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "symbol": self.symbol or NA,
            "timeframe": self.timeframe or NA,
            "role": self.role or NA,
            "decision_cutoff": _iso_or_na(self.decision_cutoff),
            "latest_observed_close": _iso_or_na(self.latest_observed_close),
            "required_close": _iso_or_na(self.required_close),
            "lag_intervals": self.lag_intervals if self.lag_intervals is not None else NA,
            "policy_version": self.policy_version,
            "allowance_ms": _timedelta_milliseconds(self.allowance),
            "anchor": self.anchor or NA,
        }

    def diagnostic_line(self) -> str | None:
        """Return a required-data line, or None when coverage passed.

        Missing coverage is N/A. Unverifiable coverage is Unverified. A pass is
        not a diagnostic failure line.
        """

        if self.passed:
            return None
        label = "Unverified" if self.status is CandleFreshnessStatus.UNVERIFIED else NA
        payload = self.as_dict()
        detail = (
            f"role={payload['role']},status={payload['status']},reason={payload['reason']},"
            f"decision_cutoff={payload['decision_cutoff']},"
            f"latest_observed_close={payload['latest_observed_close']},"
            f"required_close={payload['required_close']},"
            f"lag_intervals={payload['lag_intervals']},"
            f"policy_version={payload['policy_version']},"
            f"allowance_ms={payload['allowance_ms']},"
            f"anchor={payload['anchor']}"
        )
        return f"{FRESHNESS_TOKEN}[{self.timeframe or 'unknown'}]: {label} ({detail})"


@dataclass(frozen=True)
class EssentialFreshnessBundle:
    assessments: tuple[CandleFreshnessAssessment, ...]
    policy_version: str = POLICY_VERSION

    @property
    def passed(self) -> bool:
        return bool(self.assessments) and all(item.passed for item in self.assessments)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.assessments]

    def receipt(self) -> dict[str, Any]:
        missing, unverified = self.diagnostic_lines()
        return {
            "discovery_passed": self.passed,
            "policy_version": self.policy_version,
            "allowance_ms": _timedelta_milliseconds(PUBLICATION_ALLOWANCE),
            "anchor": ANCHOR_UNIX_EPOCH_UTC,
            "assessments": self.as_dicts(),
            "missing_lines": list(missing),
            "unverified_lines": list(unverified),
            **(
                {"first_failed_gate": FRESHNESS_TOKEN}
                if not self.passed
                else {}
            ),
        }

    def diagnostic_lines(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        missing: list[str] = []
        unverified: list[str] = []
        if not self.assessments:
            unverified.append(
                f"{FRESHNESS_TOKEN}: Unverified (reason=assessment_missing,"
                f"policy_version={self.policy_version},allowance_ms=0)"
            )
            return tuple(missing), tuple(unverified)
        for item in self.assessments:
            line = item.diagnostic_line()
            if line is None:
                continue
            if item.status is CandleFreshnessStatus.UNVERIFIED:
                unverified.append(line)
            else:
                missing.append(line)
        return tuple(missing), tuple(unverified)


def essential_discovery_roles(
    *,
    strategy_enabled: bool,
    primary_timeframe: str,
    execution_timeframe: str,
    confirmation_timeframe: str,
    bias_timeframe: str,
    structure_timeframe: str,
    htf_timeframe: str,
) -> tuple[tuple[str, str], ...]:
    """Roles whose candles must cover the decision cutoff before a new idea.

    Shared timeframes stay one series. Each role is still named so a failure
    says which input was uncovered. Optional 4h/1h enrichment is not promoted
    into a mandatory role unless it is one of these configured inputs.
    """

    primary = _normalize_timeframe(primary_timeframe)
    if not strategy_enabled:
        return ((primary, "primary_technical"),) if primary else ()
    specs = (
        (primary, "primary_technical"),
        (_normalize_timeframe(execution_timeframe), "execution"),
        (_normalize_timeframe(confirmation_timeframe), "confirmation"),
        (_normalize_timeframe(bias_timeframe), "bias"),
        (_normalize_timeframe(structure_timeframe), "structure"),
    )
    roles: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for timeframe, role in specs:
        if not timeframe or (timeframe, role) in seen:
            continue
        seen.add((timeframe, role))
        roles.append((timeframe, role))
    htf = _normalize_timeframe(htf_timeframe)
    if htf == "2d":
        for timeframe, role in (("2d", "htf"), ("1d", "synthetic_2d_source")):
            if (timeframe, role) not in seen:
                seen.add((timeframe, role))
                roles.append((timeframe, role))
    elif htf and (htf, "htf") not in seen:
        roles.append((htf, "htf"))
    return tuple(roles)


def assess_essential_discovery(
    *,
    symbol: str,
    decision_timestamp: Any,
    roles: Sequence[tuple[str, str]],
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    daily_source_candles: Sequence[Any] | None = None,
) -> EssentialFreshnessBundle:
    """Assess every essential role. An empty role list is not a pass."""

    if not roles:
        return EssentialFreshnessBundle(
            assessments=(
                _unverified(
                    symbol=symbol,
                    timeframe="",
                    role="essential",
                    reason="no_essential_roles",
                    decision_cutoff=_optional_decision(decision_timestamp),
                ),
            )
        )
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for timeframe, role in roles:
        if timeframe not in grouped:
            grouped[timeframe] = []
            order.append(timeframe)
        grouped[timeframe].append(role)
    assessments: list[CandleFreshnessAssessment] = []
    for timeframe in order:
        series = candles_by_timeframe.get(timeframe)
        if (series is None or len(tuple(series)) == 0) and timeframe == "1d" and daily_source_candles is not None:
            series = daily_source_candles
        base = assess_candle_freshness(
            series or (),
            symbol=symbol,
            timeframe=timeframe,
            role=grouped[timeframe][0],
            decision_timestamp=decision_timestamp,
        )
        if timeframe == "2d" and "htf" in grouped[timeframe]:
            pair = _two_day_pair_assessment(
                base,
                symbol=symbol,
                decision_timestamp=decision_timestamp,
                daily_source_candles=daily_source_candles if daily_source_candles is not None else candles_by_timeframe.get("1d"),
            )
            if pair is not None:
                base = pair
        for role in grouped[timeframe]:
            assessments.append(
                CandleFreshnessAssessment(
                    status=base.status,
                    reason=base.reason,
                    symbol=base.symbol,
                    timeframe=base.timeframe,
                    role=role,
                    decision_cutoff=base.decision_cutoff,
                    latest_observed_close=base.latest_observed_close,
                    required_close=base.required_close,
                    lag_intervals=base.lag_intervals,
                    policy_version=base.policy_version,
                    allowance=base.allowance,
                    anchor=base.anchor,
                )
            )
    return EssentialFreshnessBundle(assessments=tuple(assessments))


def assess_candle_freshness(
    candles: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
    role: str,
    decision_timestamp: Any,
    allowance: timedelta = PUBLICATION_ALLOWANCE,
) -> CandleFreshnessAssessment:
    """Coverage of one closed series against decision cutoff D."""

    normalized = _normalize_timeframe(timeframe)
    decision = _optional_decision(decision_timestamp)
    if decision is None:
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason="decision_cutoff_invalid",
            decision_cutoff=None,
        )
    if allowance < timedelta(0):
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason="negative_allowance",
            decision_cutoff=decision,
        )
    try:
        delta = timeframe_duration(normalized)
    except CandleIntegrityError:
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason="unsupported_timeframe",
            decision_cutoff=decision,
        )
    if not _supported_anchor(normalized):
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason="unsupported_anchor",
            decision_cutoff=decision,
            anchor=NA,
        )
    try:
        required_ms = required_scheduled_close_ms(normalized, decision, allowance=allowance)
    except ValueError as exc:
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason=str(exc) or "required_close_unavailable",
            decision_cutoff=decision,
        )
    required_close = _datetime_from_epoch_ms(required_ms)
    delta_ms = _timedelta_milliseconds(delta)
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason="candles_not_a_sequence",
            decision_cutoff=decision,
            required_close=required_close,
        )
    try:
        window = closed_candles_as_of(
            candles,
            timeframe=normalized,
            decision_timestamp=decision,
            minimum_closed_history=0,
        )
    except CandleIntegrityError as exc:
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason=f"candle_integrity:{exc.reason.value}",
            decision_cutoff=decision,
            required_close=required_close,
        )
    except (TypeError, ValueError):
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason="candle_timestamps_unverifiable",
            decision_cutoff=decision,
            required_close=required_close,
        )
    if not window.timeline:
        return CandleFreshnessAssessment(
            status=CandleFreshnessStatus.MISSING,
            reason="no_closed_candle",
            symbol=symbol,
            timeframe=normalized,
            role=role,
            decision_cutoff=decision,
            latest_observed_close=None,
            required_close=required_close,
            lag_intervals=None,
            allowance=allowance,
        )
    latest = window.timeline[-1]
    open_ms = _open_milliseconds(latest.source, latest.open_timestamp)
    if open_ms is None or delta_ms <= 0 or open_ms % delta_ms != 0:
        return _unverified(
            symbol=symbol,
            timeframe=normalized,
            role=role,
            reason="off_anchor",
            decision_cutoff=decision,
            required_close=required_close,
            latest_observed_close=None if open_ms is None else _datetime_from_epoch_ms(open_ms + delta_ms),
        )
    scheduled_close_ms = open_ms + delta_ms
    latest_close = _datetime_from_epoch_ms(scheduled_close_ms)
    if scheduled_close_ms < required_ms:
        lag = (required_ms - scheduled_close_ms) // delta_ms
        return CandleFreshnessAssessment(
            status=CandleFreshnessStatus.STALE,
            reason="missing_completed_interval",
            symbol=symbol,
            timeframe=normalized,
            role=role,
            decision_cutoff=decision,
            latest_observed_close=latest_close,
            required_close=required_close,
            lag_intervals=int(lag),
            allowance=allowance,
        )
    return CandleFreshnessAssessment(
        status=CandleFreshnessStatus.PASS,
        reason="covers_required_close",
        symbol=symbol,
        timeframe=normalized,
        role=role,
        decision_cutoff=decision,
        latest_observed_close=latest_close,
        required_close=required_close,
        lag_intervals=0,
        allowance=allowance,
    )


def required_scheduled_close(
    timeframe: str,
    decision_timestamp: Any,
    *,
    allowance: timedelta = PUBLICATION_ALLOWANCE,
) -> datetime:
    return _datetime_from_epoch_ms(
        required_scheduled_close_ms(timeframe, decision_timestamp, allowance=allowance)
    )


def required_scheduled_close_ms(
    timeframe: str,
    decision_timestamp: Any,
    *,
    allowance: timedelta = PUBLICATION_ALLOWANCE,
) -> int:
    """Integer-millisecond E for an epoch-anchored fixed duration."""

    normalized = _normalize_timeframe(timeframe)
    if not _supported_anchor(normalized):
        raise ValueError("unsupported_anchor")
    delta_ms = _timedelta_milliseconds(timeframe_duration(normalized))
    if delta_ms <= 0:
        raise ValueError("invalid_duration")
    decision = normalize_utc_timestamp(decision_timestamp, field_name="decision_timestamp")
    if not isinstance(decision, datetime):
        raise ValueError("decision_cutoff_invalid")
    effective_ms = utc_epoch_milliseconds(decision) - _timedelta_milliseconds(allowance)
    if effective_ms < 0:
        raise ValueError("decision_before_anchor")
    return (effective_ms // delta_ms) * delta_ms


def utc_epoch_milliseconds(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    delta = aware.astimezone(UTC) - _EPOCH_UTC
    return (delta.days * 86_400_000) + (delta.seconds * 1000) + (delta.microseconds // 1000)


def observation_has_essential_freshness_failure(observation: Any) -> bool:
    return any(FRESHNESS_TOKEN in text for text in _observation_texts(observation))


def freshness_failure_preserves_owner(observation: Any) -> bool:
    """Owned-plan hold only when freshness is the sole required-data failure."""

    if not observation_has_essential_freshness_failure(observation):
        return False
    required = (
        tuple(getattr(observation, "required_data_missing", ()) or ()),
        tuple(getattr(observation, "required_data_unverified", ()) or ()),
    )
    flat = [str(item) for group in required for item in group]
    if any(FRESHNESS_TOKEN not in item for item in flat):
        return False
    return True


def _two_day_pair_assessment(
    base: CandleFreshnessAssessment,
    *,
    symbol: str,
    decision_timestamp: Any,
    daily_source_candles: Sequence[Any] | None,
) -> CandleFreshnessAssessment | None:
    decision = _optional_decision(decision_timestamp)
    if decision is None or base.required_close is None:
        return None
    if daily_source_candles is None:
        return CandleFreshnessAssessment(
            status=CandleFreshnessStatus.MISSING,
            reason="synthetic_2d_source_missing",
            symbol=symbol,
            timeframe="2d",
            role="htf",
            decision_cutoff=decision,
            latest_observed_close=base.latest_observed_close,
            required_close=base.required_close,
            lag_intervals=base.lag_intervals,
            allowance=base.allowance,
        )
    try:
        required_ms = utc_epoch_milliseconds(base.required_close)
        day_ms = _timedelta_milliseconds(timeframe_duration("1d"))
        two_day_ms = day_ms * 2
    except (CandleIntegrityError, ValueError):
        return _unverified(
            symbol=symbol,
            timeframe="2d",
            role="htf",
            reason="synthetic_2d_pair_unverifiable",
            decision_cutoff=decision,
            required_close=base.required_close,
        )
    bucket_open = required_ms - two_day_ms
    needed = {bucket_open, bucket_open + day_ms}
    try:
        window = closed_candles_as_of(
            daily_source_candles,
            timeframe="1d",
            decision_timestamp=decision,
            minimum_closed_history=0,
        )
    except (CandleIntegrityError, TypeError, ValueError):
        return _unverified(
            symbol=symbol,
            timeframe="2d",
            role="htf",
            reason="synthetic_2d_source_unverifiable",
            decision_cutoff=decision,
            required_close=base.required_close,
        )
    opens: set[int] = set()
    for item in window.timeline:
        open_ms = _open_milliseconds(item.source, item.open_timestamp)
        if open_ms is not None:
            opens.add(open_ms)
    if not window.timeline:
        return CandleFreshnessAssessment(
            status=CandleFreshnessStatus.MISSING,
            reason="synthetic_2d_source_missing",
            symbol=symbol,
            timeframe="2d",
            role="htf",
            decision_cutoff=decision,
            latest_observed_close=base.latest_observed_close,
            required_close=base.required_close,
            lag_intervals=base.lag_intervals,
            allowance=base.allowance,
        )
    if not needed <= opens:
        return CandleFreshnessAssessment(
            status=CandleFreshnessStatus.STALE,
            reason="incomplete_daily_pair",
            symbol=symbol,
            timeframe="2d",
            role="htf",
            decision_cutoff=decision,
            latest_observed_close=base.latest_observed_close,
            required_close=base.required_close,
            lag_intervals=base.lag_intervals if base.lag_intervals else 1,
            allowance=base.allowance,
        )
    return None


def _observation_texts(observation: Any) -> tuple[str, ...]:
    texts: list[str] = []
    for name in (
        "required_data_missing",
        "required_data_unverified",
        "failed_gate",
        "final_failed_gate",
        "final_block_reason",
    ):
        value = getattr(observation, name, ())
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            texts.extend(str(item) for item in value)
    return tuple(texts)


def _supported_anchor(timeframe: str) -> bool:
    if not timeframe:
        return False
    unit = timeframe[-1:]
    return unit in _SUPPORTED_UNITS and timeframe[:-1].isdigit() and int(timeframe[:-1]) > 0


def _normalize_timeframe(timeframe: str) -> str:
    return str(timeframe or "").strip().lower()


def _optional_decision(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = normalize_utc_timestamp(value, field_name="decision_timestamp")
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, datetime):
        return None
    return parsed


def _open_milliseconds(candle: Any, open_timestamp: datetime) -> int | None:
    raw = candle.get("timestamp") if isinstance(candle, Mapping) else getattr(candle, "timestamp", None)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned.lstrip("-").isdigit():
            try:
                return int(cleaned)
            except ValueError:
                return None
    if isinstance(raw, datetime):
        return utc_epoch_milliseconds(raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC))
    try:
        return utc_epoch_milliseconds(open_timestamp)
    except (TypeError, ValueError, OverflowError):
        return None


def _timedelta_milliseconds(value: timedelta) -> int:
    return (value.days * 86_400_000) + (value.seconds * 1000) + (value.microseconds // 1000)


def _datetime_from_epoch_ms(value: int) -> datetime:
    return _EPOCH_UTC + timedelta(milliseconds=int(value))


def _iso_or_na(value: datetime | None) -> str:
    if value is None:
        return NA
    return value.astimezone(UTC).isoformat()


def _unverified(
    *,
    symbol: str,
    timeframe: str,
    role: str,
    reason: str,
    decision_cutoff: datetime | None,
    required_close: datetime | None = None,
    latest_observed_close: datetime | None = None,
    anchor: str = ANCHOR_UNIX_EPOCH_UTC,
) -> CandleFreshnessAssessment:
    return CandleFreshnessAssessment(
        status=CandleFreshnessStatus.UNVERIFIED,
        reason=reason,
        symbol=symbol,
        timeframe=timeframe,
        role=role,
        decision_cutoff=decision_cutoff,
        latest_observed_close=latest_observed_close,
        required_close=required_close,
        lag_intervals=None,
        anchor=anchor,
    )


__all__ = [
    "ANCHOR_UNIX_EPOCH_UTC",
    "FRESHNESS_TOKEN",
    "POLICY_VERSION",
    "PUBLICATION_ALLOWANCE",
    "CandleFreshnessAssessment",
    "CandleFreshnessStatus",
    "EssentialFreshnessBundle",
    "assess_candle_freshness",
    "assess_essential_discovery",
    "essential_discovery_roles",
    "freshness_failure_preserves_owner",
    "observation_has_essential_freshness_failure",
    "required_scheduled_close",
    "required_scheduled_close_ms",
    "utc_epoch_milliseconds",
]
