"""Separate observed diagnostic incompleteness from retryable operational failure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackoffEligibility:
    eligible: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"retry_backoff_eligible": self.eligible, "retry_backoff_reason": self.reason,
                "consecutive_failure_scope": "backoff_eligible_operational_iterations"}


def watch_backoff_eligibility(
    phases: Mapping[str, str], counts: Mapping[str, Any], delivery_summary: Any | None,
) -> BackoffEligibility:
    """Only a positively established provenance-only PARTIAL avoids retry backoff.

    Actual delivery failure, unavailable evidence and every other adverse phase
    retain backoff. This decision neither changes PARTIAL nor authorizes a send.
    """
    normalized = {key: str(value).upper() for key, value in phases.items()}
    healthy = {"SUCCESS", "SKIPPED"}
    if all(value in healthy for value in normalized.values()):
        return BackoffEligibility(False, "NO_OPERATIONAL_FAILURE")
    if normalized.get("telegram_outbox") != "PARTIAL" or any(
        value not in healthy for key, value in normalized.items() if key != "telegram_outbox"
    ):
        return BackoffEligibility(True, "OPERATIONAL_PHASE_FAILURE")

    keys = ("failed", "retryable", "uncertain", "failed_final")
    if delivery_summary is None or any(type(counts.get(key)) is not int or counts[key] < 0 for key in keys):
        return BackoffEligibility(True, "DELIVERY_FAILURE_COUNTS_UNVERIFIED")
    if any(counts[key] for key in keys):
        return BackoffEligibility(True, "DELIVERY_OPERATIONAL_FAILURE")
    # Prove the original sources exist; default-to-zero summary conversions are
    # not evidence that an unavailable delivery summary was healthy.
    failed = getattr(delivery_summary, "failed", None)
    deliveries = getattr(delivery_summary, "deliveries", None)
    if type(failed) is not int or failed != 0 or not isinstance(deliveries, (tuple, list)):
        return BackoffEligibility(True, "DELIVERY_SOURCE_UNVERIFIED")
    for delivery in deliveries:
        status, detail = getattr(delivery, "status", None), getattr(delivery, "detail", None)
        if not isinstance(status, str) or not isinstance(detail, str):
            return BackoffEligibility(True, "DELIVERY_SOURCE_UNVERIFIED")
        if status.lower() not in {"sent", "skipped", "blocked", "blocked_repeat", "duplicate", "dry_run"} or any(
            marker in detail.upper() for marker in ("RETRYABLE", "UNCERTAIN", "FAILED_FINAL")
        ):
            return BackoffEligibility(True, "DELIVERY_OPERATIONAL_FAILURE")

    provenance = getattr(delivery_summary, "provenance_diagnostics", None)
    if not isinstance(provenance, Mapping) or provenance.get("status") != "OBSERVED" or provenance.get("contract") != "cci-public-provenance-diagnostics-v1" or provenance.get("error") not in (None, "", "N/A"):
        return BackoffEligibility(True, "PROVENANCE_OBSERVATION_UNVERIFIED")
    unresolved, inspected, limit = (provenance.get(key) for key in ("unresolved", "inspected", "inspection_limit"))
    complete = provenance.get("coverage_complete")
    if (type(complete) is not bool or any(type(value) is not int for value in (unresolved, inspected, limit))
            or not 0 <= unresolved <= inspected <= limit or limit < 1
            or (not complete and inspected != limit) or (complete and unresolved == 0)):
        return BackoffEligibility(True, "PROVENANCE_OBSERVATION_UNVERIFIED")
    if type(counts.get("provenance_unresolved")) is not int or counts["provenance_unresolved"] != unresolved or type(counts.get("provenance_incomplete")) is not int or counts["provenance_incomplete"] != int(not complete):
        return BackoffEligibility(True, "PROVENANCE_COUNTS_UNVERIFIED")
    return BackoffEligibility(False, "PROVENANCE_ONLY_DIAGNOSTIC")
