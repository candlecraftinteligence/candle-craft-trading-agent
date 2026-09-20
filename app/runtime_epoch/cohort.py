"""Read-only cohort projection from proven ownership relationships."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from app.runtime_epoch.authority import load_active_runtime_epoch
from app.runtime_epoch.models import (
    COHORT_CURRENT_EPOCH_OPERATIONAL,
    COHORT_LEGACY_OR_UNATTRIBUTED,
)
from app.runtime_epoch.ownership import (
    lifecycle_row_epoch_id,
    public_event_epoch_id,
    public_ownership_chain_reason,
)

CohortLabel = Literal["LEGACY_OR_UNATTRIBUTED", "CURRENT_EPOCH_OPERATIONAL"]


def classify_lifecycle_cohort(connection: sqlite3.Connection, row: sqlite3.Row | Any) -> CohortLabel:
    epoch = load_active_runtime_epoch(connection)
    row_epoch = lifecycle_row_epoch_id(row)
    if epoch is not None and row_epoch == epoch.epoch_id:
        return COHORT_CURRENT_EPOCH_OPERATIONAL
    return COHORT_LEGACY_OR_UNATTRIBUTED


def classify_public_cohort(connection: sqlite3.Connection, row: sqlite3.Row | Any) -> CohortLabel:
    epoch = load_active_runtime_epoch(connection)
    event_epoch = public_event_epoch_id(row)
    if epoch is not None and event_epoch == epoch.epoch_id:
        if public_ownership_chain_reason(connection, row, epoch=epoch) is None:
            return COHORT_CURRENT_EPOCH_OPERATIONAL
    return COHORT_LEGACY_OR_UNATTRIBUTED


def classify_run_cohort(connection: sqlite3.Connection, run_id: str) -> CohortLabel:
    epoch = load_active_runtime_epoch(connection)
    if epoch is None:
        return COHORT_LEGACY_OR_UNATTRIBUTED
    row = connection.execute(
        "SELECT runtime_epoch_id FROM runtime_operational_runs WHERE run_id = ?",
        (str(run_id),),
    ).fetchone()
    if row is not None and str(row["runtime_epoch_id"]) == epoch.epoch_id:
        return COHORT_CURRENT_EPOCH_OPERATIONAL
    return COHORT_LEGACY_OR_UNATTRIBUTED


def current_epoch_lifecycle_filter_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}runtime_epoch_id = (SELECT epoch_id FROM runtime_epoch_control WHERE control_key = 'active')"


def forensic_unattributed_lifecycle_filter_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}runtime_epoch_id IS NULL"
