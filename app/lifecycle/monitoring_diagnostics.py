"""Bounded, per-owner monitoring accounting; never evidence of lifetime completeness."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from app.data.dtos import NA


def summarize_monitoring_passes(
    passes: Sequence[Mapping[str, Any]], *, retain_passes: bool = False,
) -> dict[str, Any]:
    owners: dict[str, dict[str, Any]] = {}
    lags: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    evaluated: dict[str, None] = {}
    covered: dict[str, None] = {}
    for report in passes:
        pass_lags = {row["lifecycle_id"]: dict(row) for row in report.get("lags", ())}
        pass_errors = {row["lifecycle_id"]: dict(row) for row in report.get("errors", ())}
        for error in report.get("errors", ()):
            if error not in errors:
                errors.append(dict(error))
        evaluated.update(dict.fromkeys(report.get("evaluated_lifecycle_ids", ())))
        covered.update(dict.fromkeys(report.get("covered_keys", ())))
        verified = set(report.get("verified_lifecycle_ids", ()))
        gaps = set(report.get("gap_lifecycle_ids", ()))
        skipped = set(report.get("skipped_lifecycle_ids", ()))
        pass_evaluated = set(report.get("evaluated_lifecycle_ids", ()))
        ids = dict.fromkeys(report.get("obligation_lifecycle_ids", ()))
        ids.update(dict.fromkeys((*pass_lags, *pass_errors)))
        for lifecycle_id in ids:
            if lifecycle_id in skipped and lifecycle_id not in pass_errors and lifecycle_id in owners:
                continue
            row = pass_lags.get(lifecycle_id, {})
            error = pass_errors.get(lifecycle_id)
            if error is not None:
                disposition = "ERROR"
            elif lifecycle_id in gaps:
                disposition = "EVIDENCE_GAP"
            elif lifecycle_id in verified:
                disposition = "EVALUATED_VERIFIED"
            elif lifecycle_id in pass_evaluated:
                disposition = "EVALUATED_UNVERIFIED"
            elif lifecycle_id in skipped:
                disposition = "SKIPPED_PREVIOUS_PASS"
            else:
                disposition = "UNCOVERED"
            owners[lifecycle_id] = {
                "lifecycle_id": lifecycle_id,
                "symbol": row.get("symbol", (error or {}).get("symbol", NA)),
                "plan_version_id": row.get("plan_version_id") or NA,
                "disposition": disposition,
                "evidence_quality": "Verified" if disposition == "EVALUATED_VERIFIED" else "Unverified",
                "diagnostic": (error or {}).get("detail") or row.get("gap_reason") or NA,
            }
            if row:
                lags[lifecycle_id] = row
    counts = dict(Counter(row["disposition"] for row in owners.values()))
    verified_ids = [key for key, row in owners.items() if row["disposition"] == "EVALUATED_VERIFIED"]
    result = {
        "obligation_lifecycle_ids": list(owners),
        "obligation_count": len(owners),
        "owner_dispositions": list(owners.values()),
        "disposition_counts": counts,
        "verified_lifecycle_ids": verified_ids,
        "verified_evaluation_count": len(verified_ids),
        "all_obligations_verified": (len(verified_ids) == len(owners) and not errors) if owners else None,
        "historical_completeness": "Unverified",
        "evidence_scope": "this_monitoring_cycle_only",
        "evaluated_lifecycle_ids": list(evaluated),
        "gap_lifecycle_ids": [key for key, row in owners.items() if row["disposition"] == "EVIDENCE_GAP"],
        "skipped_lifecycle_ids": [key for key, row in owners.items() if row["disposition"] == "SKIPPED_PREVIOUS_PASS"],
        "uncovered_symbols": list(dict.fromkeys(
            row["symbol"] for row in owners.values()
            if row["disposition"] in {"UNCOVERED", "SKIPPED_PREVIOUS_PASS"}
        )),
        "errors": errors,
        "lags": list(lags.values()),
        "covered_keys": list(covered),
    }
    if retain_passes:
        result["passes"] = [dict(report) for report in passes]
    return result
