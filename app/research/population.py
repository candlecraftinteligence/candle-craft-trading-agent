"""Prospective research population contract.

Membership is durable Runtime lineage. A timestamp, symbol, mode, direction,
or price geometry does not place a row in a prospective epoch.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.authority import load_active_runtime_epoch
from app.storage.database import StorageError

HISTORICAL_MIXED_NON_PROSPECTIVE = "HISTORICAL_MIXED_NON_PROSPECTIVE"
PROSPECTIVE_EPOCH_SCOPED_RESEARCH = "PROSPECTIVE_EPOCH_SCOPED_RESEARCH"
ACTIVE_EPOCH_SELECTION = "ACTIVE"
_SCOPE_ALIASES = {
    "historical": HISTORICAL_MIXED_NON_PROSPECTIVE,
    "historical_mixed": HISTORICAL_MIXED_NON_PROSPECTIVE,
    HISTORICAL_MIXED_NON_PROSPECTIVE: HISTORICAL_MIXED_NON_PROSPECTIVE,
    "prospective": PROSPECTIVE_EPOCH_SCOPED_RESEARCH,
    PROSPECTIVE_EPOCH_SCOPED_RESEARCH: PROSPECTIVE_EPOCH_SCOPED_RESEARCH,
}


class ResearchPopulationError(StorageError):
    """Prospective scope could not be proven. Callers must not fall back to all rows."""


@dataclass(frozen=True)
class ResolvedResearchPopulation:
    scope_type: str
    prospective: bool
    requested_runtime_epoch_id: str | None
    resolved_runtime_epoch_id: str | None
    epoch_cutoff_at: str | None
    selection: str
    fallback_to_all_history: bool = False

    def requested_label(self) -> str:
        if self.selection == "active_runtime_epoch":
            return ACTIVE_EPOCH_SELECTION
        return self.requested_runtime_epoch_id or ""


def canonical_population_scope(value: str | None) -> str:
    key = str(value or "").strip()
    resolved = _SCOPE_ALIASES.get(key) or _SCOPE_ALIASES.get(key.lower())
    if resolved is None:
        raise ResearchPopulationError(f"Unsupported research population scope: {value}")
    return resolved


def resolve_research_population(
    connection: sqlite3.Connection,
    *,
    population_scope: str,
    runtime_epoch_id: str | None,
    resolve_active_runtime_epoch: bool,
) -> ResolvedResearchPopulation:
    """Resolve the requested scope or fail closed.

    Historical mixed research is explicit and does not accept an epoch selector.
    Prospective research never continues with an unscoped read.
    """

    scope = canonical_population_scope(population_scope)
    epoch_id = str(runtime_epoch_id or "").strip() or None
    resolve_active = bool(resolve_active_runtime_epoch)
    if scope == HISTORICAL_MIXED_NON_PROSPECTIVE:
        if epoch_id is not None or resolve_active:
            raise ResearchPopulationError(
                "Historical mixed research does not accept a Runtime epoch selector."
            )
        return ResolvedResearchPopulation(
            scope_type=scope,
            prospective=False,
            requested_runtime_epoch_id=None,
            resolved_runtime_epoch_id=None,
            epoch_cutoff_at=None,
            selection="explicit_historical_mixed",
        )

    _require_prospective_lineage(connection)
    if epoch_id is not None and resolve_active:
        raise ResearchPopulationError(
            "Prospective research accepts either an exact Runtime epoch or active-epoch "
            "resolution, not both. Refusing to fall back to historical rows."
        )
    if epoch_id is not None:
        row = connection.execute(
            "SELECT epoch_id, cutoff_at FROM runtime_epochs WHERE epoch_id = ?",
            (epoch_id,),
        ).fetchone()
        if row is None:
            raise ResearchPopulationError(
                f"Prospective research epoch {epoch_id!r} does not exist. "
                "Refusing to fall back to historical rows."
            )
        return ResolvedResearchPopulation(
            scope_type=scope,
            prospective=True,
            requested_runtime_epoch_id=str(row["epoch_id"]),
            resolved_runtime_epoch_id=str(row["epoch_id"]),
            epoch_cutoff_at=str(row["cutoff_at"]),
            selection="explicit_runtime_epoch",
        )
    if not resolve_active:
        raise ResearchPopulationError(
            "Prospective research requires an exact Runtime epoch or explicit active-epoch "
            "resolution. Refusing to fall back to historical rows."
        )
    active = load_active_runtime_epoch(connection)
    if active is None:
        raise ResearchPopulationError(
            "Prospective research requires an active Runtime epoch and none is configured. "
            "Refusing to fall back to historical rows."
        )
    return ResolvedResearchPopulation(
        scope_type=scope,
        prospective=True,
        requested_runtime_epoch_id=None,
        resolved_runtime_epoch_id=active.epoch_id,
        epoch_cutoff_at=active.cutoff_at,
        selection="active_runtime_epoch",
    )


def epoch_run_ids(connection: sqlite3.Connection, epoch_id: str) -> tuple[str, ...]:
    """Run ids registered to one Runtime epoch. Unregistered runs are not members."""

    rows = connection.execute(
        """
        SELECT run_id
        FROM runtime_operational_runs
        WHERE runtime_epoch_id = ?
        ORDER BY registered_at ASC, run_id ASC
        """,
        (epoch_id,),
    ).fetchall()
    return tuple(str(row["run_id"]) for row in rows if str(row["run_id"] or "").strip())


def symbol_lifecycle_lineage_census(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    epoch_id: str,
) -> dict[str, int]:
    """Count lifecycle lineage for one symbol. This does not scan by loading rows into the report."""

    row = connection.execute(
        """
        SELECT
            SUM(
                CASE
                    WHEN runtime_epoch_id IS NULL OR TRIM(runtime_epoch_id) = '' THEN 1
                    ELSE 0
                END
            ) AS legacy_or_null_epoch,
            SUM(
                CASE
                    WHEN runtime_epoch_id IS NOT NULL
                     AND TRIM(runtime_epoch_id) != ''
                     AND runtime_epoch_id != ? THEN 1
                    ELSE 0
                END
            ) AS different_epoch
        FROM setup_lifecycle_records
        WHERE symbol = ?
        """,
        (epoch_id, symbol),
    ).fetchone()
    return {
        "legacy_or_null_epoch": int(row["legacy_or_null_epoch"] or 0) if row is not None else 0,
        "different_epoch": int(row["different_epoch"] or 0) if row is not None else 0,
    }


def in_clause(count: int) -> str:
    if count < 1:
        raise ValueError("A prospective run filter requires at least one registered run id.")
    return ",".join("?" for _ in range(count))


def scan_runs_membership_sql(run_count: int) -> str:
    return f"""
        SELECT *
        FROM scan_runs
        WHERE run_id IN ({in_clause(run_count)})
        ORDER BY timestamp ASC
    """


def symbol_results_membership_sql(run_count: int) -> str:
    return f"""
        SELECT sr.*, r.timestamp, r.market_regime AS run_market_regime,
               r.command_preset, r.exchange, r.universe
        FROM symbol_results sr
        JOIN scan_runs r ON r.run_id = sr.run_id
        WHERE sr.run_id IN ({in_clause(run_count)})
        ORDER BY r.timestamp ASC, sr.id ASC
    """


def setup_candidates_membership_sql(run_count: int) -> str:
    return f"""
        SELECT sc.*, r.timestamp, r.market_regime AS run_market_regime,
               sr.setup_quality_score, sr.readiness_score,
               sr.display_bucket, sr.raw_result_json AS symbol_raw_result_json
        FROM setup_candidates sc
        JOIN scan_runs r ON r.run_id = sc.run_id
        LEFT JOIN symbol_results sr ON sr.run_id = sc.run_id AND sr.symbol = sc.symbol
        WHERE sc.run_id IN ({in_clause(run_count)})
        ORDER BY r.timestamp ASC, sc.id ASC
    """


def replay_results_membership_sql(run_count: int) -> str:
    return f"""
        SELECT rr.*, r.timestamp, r.market_regime AS run_market_regime
        FROM replay_results rr
        JOIN scan_runs r ON r.run_id = rr.run_id
        WHERE rr.run_id IN ({in_clause(run_count)})
        ORDER BY r.timestamp ASC, rr.id ASC
    """


LIFECYCLE_MEMBERSHIP_SQL = """
    SELECT *
    FROM setup_lifecycle_records
    WHERE runtime_epoch_id = ?
    ORDER BY last_seen_at ASC
"""

LIFECYCLE_EVENT_MEMBERSHIP_SQL = """
    SELECT e.*, r.mode, r.direction, r.regime_state
    FROM setup_lifecycle_records r
    JOIN setup_lifecycle_events e ON e.lifecycle_id = r.lifecycle_id
    WHERE r.runtime_epoch_id = ?
    ORDER BY e.timestamp ASC, e.event_id ASC
"""


def lifecycle_membership_sql() -> str:
    return LIFECYCLE_MEMBERSHIP_SQL


def lifecycle_event_membership_sql() -> str:
    return LIFECYCLE_EVENT_MEMBERSHIP_SQL


def _require_prospective_lineage(connection: sqlite3.Connection) -> None:
    required_tables = (
        "runtime_epochs",
        "runtime_epoch_control",
        "runtime_operational_runs",
        "runtime_operational_origins",
    )
    missing = [name for name in required_tables if not _table_exists(connection, name)]
    if missing:
        raise ResearchPopulationError(
            "Prospective research requires Runtime epoch lineage tables "
            f"({', '.join(missing)}). Refusing to fall back to historical rows."
        )
    if _table_exists(connection, "setup_lifecycle_records") and not _column_exists(
        connection,
        "setup_lifecycle_records",
        "runtime_epoch_id",
    ):
        raise ResearchPopulationError(
            "Prospective research requires setup_lifecycle_records.runtime_epoch_id. "
            "Refusing to fall back to historical rows."
        )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({_sql_ident(table)})").fetchall()
    return any(str(row[1]) == column for row in rows)


def _sql_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def population_public_dict(
    resolved: ResolvedResearchPopulation,
    *,
    included: dict[str, int],
    exclusions: dict[str, Any],
) -> dict[str, Any]:
    prospective = resolved.prospective
    return {
        "scope_type": resolved.scope_type,
        "requested_runtime_epoch_id": resolved.requested_label() or NA,
        "resolved_runtime_epoch_id": resolved.resolved_runtime_epoch_id or NA,
        "epoch_cutoff_at": resolved.epoch_cutoff_at or NA,
        "selection": resolved.selection,
        "population_semantics": (
            "prospective_runtime_epoch_lineage" if prospective else "historical_mixed_non_prospective"
        ),
        "membership_rule": "durable_lineage_not_timestamp" if prospective else "not_applicable",
        "lineage_status": "epoch_proven" if prospective else "mixed_unscoped",
        "prospective": prospective,
        "fallback_to_all_history": False,
        "expectancy_claim": (
            "prospective_population_only" if prospective else "not_a_prospective_claim"
        ),
        "included": included,
        "exclusions": exclusions,
    }
