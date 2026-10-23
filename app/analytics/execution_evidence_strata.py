"""Classify supplied execution-integrity claims without reconstructing history."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from app.data.dtos import NA

EXECUTION_EVIDENCE_CONTRACT = "cci-supplied-execution-evidence-v1"
SUPPLIED_FIELDS = (
    "plan_identity", "plan_version_id", "integrity_status", "diagnostic",
    "evaluation_cursor_open_at", "evaluation_cursor_close_at",
    "entry_tracking_from_at", "first_evaluated_at", "last_evaluated_at",
    "metadata_json", "metadata",
)


def summarize_execution_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    supplied = [
        {key: _retained_value(row.get(key)) for key in SUPPLIED_FIELDS}
        for row in rows
    ]
    supplied.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
    counts = Counter(_reported_stratum(row) for row in supplied)
    if not counts:
        stratum = "no_progress_supplied"
    elif len(counts) > 1:
        stratum = "mixed"
    else:
        stratum = next(iter(counts))
    return {
        "contract_version": EXECUTION_EVIDENCE_CONTRACT,
        "stratum": stratum,
        "source_status_counts": dict(sorted(counts.items())),
        "supplied_rows": supplied,
        "historical_completeness": "Unverified",
        "status_basis": "supplied_progress_claims_only",
    }


def _retained_value(value: Any) -> Any:
    if value is None or isinstance(value, str) and (not value.strip() or value.strip().upper() == NA):
        return NA
    # Match the existing source-digest representation for Mapping inputs such as
    # Decimal/datetime metadata. Preserve supplied strings and offsets verbatim.
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _reported_stratum(row: Mapping[str, Any]) -> str:
    status = row.get("integrity_status")
    if status is None or status == NA or status == "":
        return "unavailable"
    if not isinstance(status, str):
        return "unrecognized"
    token = status.strip().casefold()
    if not token or token == NA.casefold():
        return "unavailable"
    if token == "verified":
        diagnostic = row.get("diagnostic")
        if diagnostic not in (None, "", NA):
            return "reported_verified_with_diagnostic"
        return "reported_verified"
    if token == "unverified":
        return "reported_unverified"
    if token == "failed":
        return "reported_failed"
    return "unrecognized"
