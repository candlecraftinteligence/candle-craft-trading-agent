"""P2A read-only activation accounting projections.

Derives research measurements from already-committed `scan_runs` and
`setup_lifecycle_events` rows. This module does not write, backfill, mint
identities, or feed lifecycle, ranking, confirmation, or Telegram decisions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.analytics.evidence_contract import UNAVAILABLE, UNSAFE
from app.lifecycle.models import SetupTransitionReason

ACTIVATION_ACCOUNTING_VERSION: Final[str] = "cci-activation-accounting-v1"

WATCH_ALERT_UNIT: Final[str] = "watch-loop WatchActivation alert per watch iteration"
EVENT_RECORD_UNIT: Final[str] = "setup_lifecycle_events row"
PROCESSING_TIME_WATERMARK: Final[str] = (
    "Committed setup_lifecycle_events.timestamp in half-open [start, cutoff). "
    "That timestamp is evaluation/processing time (evaluated_at), not candle-close "
    "occurrence time."
)
SCAN_PERSIST_WATERMARK: Final[str] = (
    "Committed scan_runs.timestamp persist window [start, cutoff). "
    "Watch-alert values are stored at scan persist, after lifecycle apply, but "
    "the counter itself is the watch-loop alert list, not a lifecycle fill snapshot."
)

_ENTRY_ACTIVATED_REASON: Final[str] = SetupTransitionReason.ENTRY_ACTIVATED.value
_ENTRY_ZONE_TOUCHED_REASON: Final[str] = SetupTransitionReason.ENTRY_ZONE_TOUCHED.value
_ENTRY_FILL_SIMULATED_REASON: Final[str] = SetupTransitionReason.ENTRY_FILL_SIMULATED.value


def project_activation_accounting(
    *,
    scan_rows: Sequence[Mapping[str, Any]],
    scan_columns: set[str],
    event_rows: Sequence[Mapping[str, Any]],
    event_columns: set[str],
    events_table_available: bool,
) -> dict[str, Any]:
    """Project P2A activation measurements from a committed evidence window."""

    return {
        "accounting_version": ACTIVATION_ACCOUNTING_VERSION,
        "feeds_operational_decisions": False,
        "legacy_valid_activations": _legacy_valid_activations(scan_rows, scan_columns),
        "watch_alert_activations": _watch_alert_activations(scan_rows, scan_columns),
        "entry_zone_touched_event_records": _event_record_count(
            event_rows,
            event_columns,
            events_table_available=events_table_available,
            reason=_ENTRY_ZONE_TOUCHED_REASON,
            name="entry_zone_touched_event_records",
            meaning=(
                "Event-record count of SetupTransitionReason.ENTRY_ZONE_TOUCHED. "
                "A zone-touch event is not an entry activation or fill."
            ),
        ),
        "entry_activated_event_records": _event_record_count(
            event_rows,
            event_columns,
            events_table_available=events_table_available,
            reason=_ENTRY_ACTIVATED_REASON,
            name="entry_activated_event_records",
            meaning=(
                "Event-record count of SetupTransitionReason.ENTRY_ACTIVATED "
                "(closed-execution-candle entry activation evidence). "
                "Not a unique fill-occurrence count and not a watch-alert count."
            ),
        ),
        "entry_fill_simulated_event_records": _event_record_count(
            event_rows,
            event_columns,
            events_table_available=events_table_available,
            reason=_ENTRY_FILL_SIMULATED_REASON,
            name="entry_fill_simulated_event_records",
            meaning=(
                "Event-record count of SetupTransitionReason.ENTRY_FILL_SIMULATED. "
                "The stored reason text is 'Entry fill simulated or confirmed.' "
                "Structured evidence cannot split simulated vs verified manual fills."
            ),
            extra={
                "simulated_versus_confirmed": "indistinguishable",
                "do_not_infer_manual_fill": True,
            },
        ),
        "fill_occurrence_count": _unavailable_occurrence(
            "No authoritative fill-occurrence identity exists. "
            "P1 setup_id/plan_version_id are economic content identities, not fill ids. "
            "Do not hash symbol/candle/price/time to invent uniqueness."
        ),
        "manual_fill_count": _unavailable_occurrence(
            "Manual/verified fill is NOT FOUND as a distinct producer, table, or type. "
            "Do not infer it from EXECUTING, ENTRY_FILL_SIMULATED, Telegram delivery, "
            "or natural-language reasons."
        ),
        "unique_activation_occurrence_count": _unavailable_occurrence(
            "Occurrence-level activation uniqueness is unsupported. "
            "Use entry_activated_event_records for event-row accounting only."
        ),
    }


def _legacy_valid_activations(
    scan_rows: Sequence[Mapping[str, Any]],
    scan_columns: set[str],
) -> dict[str, Any]:
    if "valid_activations" not in scan_columns:
        return {
            "status": UNAVAILABLE,
            "value": None,
            "unit": WATCH_ALERT_UNIT,
            "physical_field": "scan_runs.valid_activations",
            "economic_research_metric": False,
            "research_funnel_suitable": False,
            "zero_semantics": "column absent is unavailable, not zero",
        }
    total = sum(int(row.get("valid_activations") or 0) for row in scan_rows)
    return {
        "status": UNSAFE,
        "value": total,
        "unit": (
            "legacy scan_runs.valid_activations sum over the persist window, "
            "including non-watch DEFAULT 0 rows"
        ),
        "physical_field": "scan_runs.valid_activations",
        "economic_research_metric": False,
        "research_funnel_suitable": False,
        "watermark": SCAN_PERSIST_WATERMARK,
        "zero_semantics": (
            "A stored 0 on a non-watch scan_run is the insert default, not a complete "
            "observation of no watch alerts and not proof of no lifecycle fills. "
            "Do not treat this mixed sum as an economic activation total."
        ),
        "see": "watch_alert_activations",
    }


def _watch_alert_activations(
    scan_rows: Sequence[Mapping[str, Any]],
    scan_columns: set[str],
) -> dict[str, Any]:
    base = {
        "unit": WATCH_ALERT_UNIT,
        "producer": "app.watch_mode.build_watch_iteration_summary",
        "authoritative_predicate": (
            "scan_runs.is_watch_iteration = 1; value is the stored "
            "valid_activations for that watch iteration (len(WatchActivation))"
        ),
        "counting_unit": WATCH_ALERT_UNIT,
        "uniqueness_rule": (
            "Per stored watch-iteration scan_runs row. Repeated iterations recount. "
            "Not unique economic plans or fills."
        ),
        "aggregation_grain": "sum of per-watch-iteration counters in the persist window",
        "watermark": SCAN_PERSIST_WATERMARK,
        "economic_research_metric": False,
        "research_funnel_suitable": False,
        "not": (
            "lifecycle ENTRY_ACTIVATED, ENTRY_FILL_SIMULATED, EXECUTING state, "
            "or public signal delivery"
        ),
    }
    if "valid_activations" not in scan_columns or "is_watch_iteration" not in scan_columns:
        return {
            **base,
            "status": UNAVAILABLE,
            "value": None,
            "completeness": "unsupported",
            "zero_semantics": (
                "Missing is_watch_iteration or valid_activations column is unavailable, not zero"
            ),
        }
    watch_rows = [
        row for row in scan_rows if int(row.get("is_watch_iteration") or 0) == 1
    ]
    if not watch_rows:
        return {
            **base,
            "status": UNAVAILABLE,
            "value": None,
            "completeness": "no_watch_iterations_in_window",
            "watch_iterations_in_window": 0,
            "zero_semantics": (
                "No watch-iteration rows in the persist window. That is unavailable "
                "watch-alert evidence, not a complete zero of alerts or fills."
            ),
        }
    total = sum(int(row.get("valid_activations") or 0) for row in watch_rows)
    return {
        **base,
        "status": "available",
        "value": total,
        "completeness": "complete_supported_watch_window",
        "watch_iterations_in_window": len(watch_rows),
        "zero_semantics": (
            "A complete zero means those watch iterations produced no WatchActivation "
            "alerts. It still does not mean no lifecycle fills occurred."
        ),
    }


def _event_record_count(
    event_rows: Sequence[Mapping[str, Any]],
    event_columns: set[str],
    *,
    events_table_available: bool,
    reason: str,
    name: str,
    meaning: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "meaning": meaning,
        "unit": EVENT_RECORD_UNIT,
        "authoritative_predicate": f"setup_lifecycle_events.reason == {reason!r}",
        "event_time": "setup_lifecycle_events.timestamp (processing/evaluation time)",
        "watermark": PROCESSING_TIME_WATERMARK,
        "uniqueness_rule": (
            "Count matching event rows. event_id identifies a persisted event record, "
            "not a unique economic occurrence. Distinct source events stay distinct; "
            "do not collapse by lifecycle_id, setup_id, or plan_version_id."
        ),
        "aggregation_grain": "event records whose timestamp is in [start, cutoff)",
        "run_iteration_attribution": (
            "scan_run_id is copied when the lifecycle iteration received one. "
            "Missing scan_run_id stays missing; those rows remain lifecycle-event "
            "measurements and are not attached to a scan by timestamp join."
        ),
        "economic_research_metric": False,
        "research_funnel_suitable": False,
    }
    if extra:
        payload.update(dict(extra))
    if not events_table_available or "reason" not in event_columns:
        payload.update(
            {
                "status": UNAVAILABLE,
                "value": None,
                "completeness": "unsupported",
                "zero_semantics": "Missing events table or reason column is unavailable, not zero",
            }
        )
        return payload

    matched = [row for row in event_rows if str(row.get("reason") or "") == reason]
    if "event_id" in event_columns:
        identities = tuple(
            row.get("event_id") for row in matched if row.get("event_id") is not None
        )
        value = len(set(identities)) if len(identities) == len(matched) else len(matched)
        uniqueness = "event_id when present; otherwise window row count"
    else:
        value = len(matched)
        uniqueness = "window row count; event_id column absent"
    unattributed = sum(1 for row in matched if not _has_scan_run_id(row, event_columns))
    payload.update(
        {
            "status": "available",
            "value": value,
            "completeness": "complete_supported_event_window",
            "uniqueness_implementation": uniqueness,
            "records_missing_scan_run_id": unattributed,
            "zero_semantics": (
                "A complete zero means no matching event records were committed in the "
                "processing-time window. It is not a unique-fill zero and not a "
                "watch-alert zero."
            ),
        }
    )
    return payload


def _has_scan_run_id(row: Mapping[str, Any], event_columns: set[str]) -> bool:
    if "scan_run_id" not in event_columns:
        return False
    value = row.get("scan_run_id")
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text.upper() not in {"N/A", "NONE"}


def _unavailable_occurrence(reason: str) -> dict[str, Any]:
    return {
        "status": UNAVAILABLE,
        "value": None,
        "unit": "occurrence",
        "completeness": "unsupported",
        "zero_semantics": "Unsupported occurrence identity is unavailable, not zero",
        "reason": reason,
    }


__all__ = [
    "ACTIVATION_ACCOUNTING_VERSION",
    "EVENT_RECORD_UNIT",
    "WATCH_ALERT_UNIT",
    "project_activation_accounting",
]
