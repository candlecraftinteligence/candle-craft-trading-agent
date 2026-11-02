"""Measured host admission evidence; synthetic RSS is never production capacity."""
from datetime import timedelta


def assess_host(*, observed_at, now, total_physical, available_physical,
                commit_total, commit_limit, scanner_peak, listener_peak,
                cmc_incremental_peak, retained_floor, simultaneous_peak_verified=False):
    values = (total_physical, available_physical, commit_total, commit_limit,
        scanner_peak, listener_peak, cmc_incremental_peak, retained_floor)
    if observed_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("timezone-aware host observations required")
    if any(v is None for v in values):
        return {"status":"UNVERIFIED","reason":"missing host/commit or measured CMC incremental peak"}
    if any(isinstance(v,bool) or not isinstance(v,int) or v < 0 for v in values):
        raise ValueError("nonnegative measured memory bytes required")
    if not observed_at <= now <= observed_at+timedelta(minutes=5):
        return {"status":"UNVERIFIED","reason":"host observation stale or future"}
    if available_physical > total_physical or commit_total > commit_limit:
        raise ValueError("inconsistent host memory observation")
    required = scanner_peak+listener_peak+cmc_incremental_peak+retained_floor
    shortfall = max(0,required-available_physical)
    commit_shortfall = max(0,scanner_peak+listener_peak+cmc_incremental_peak-(commit_limit-commit_total))
    status = "BLOCKED" if shortfall or commit_shortfall else "VERIFIED" if simultaneous_peak_verified else "UNVERIFIED"
    return {"status":status,"required_prestart_physical_bytes":required,
        "physical_shortfall_bytes":shortfall,"commit_shortfall_bytes":commit_shortfall,
        "method":"roles stopped; observed process peaks plus measured incremental CMC and owner-reviewed floor",
        "pagefile_is_physical_ram":False,
        "recommendation":"Increase physical host capacity or move the combined services to a larger isolated host; remeasure before approval"
            if shortfall else "Prove simultaneous peaks, paging pressure, scan deadlines and stable headroom in approved staging"}
