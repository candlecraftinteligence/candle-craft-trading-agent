from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import quote

from app.data.dtos import NA
from app.storage.database import DEFAULT_DATABASE_PATH, StorageError

MISSING_SCAN_DATABASE_MESSAGE = "No scan database found. Run scans with --store-scan first."
SAMPLE_SIZE_WARNING = "Sample size too small for reliable conclusion."
MIN_RELIABLE_SAMPLE_SIZE = 30
DEFAULT_LIFECYCLE_STALE_HOURS = 24.0
LIFECYCLE_FUNNEL_STATES = (
    "DISCOVERED",
    "WATCHLISTED",
    "STALKING",
    "TRIGGERED",
    "CONFIRMED",
    "EXECUTING",
    "TP_HIT",
    "SL_HIT",
    "INVALIDATED",
    "EXPIRED",
    "ARCHIVED",
)
LIFECYCLE_CONVERSION_STEPS = (
    ("WATCHLISTED", "STALKING"),
    ("STALKING", "TRIGGERED"),
    ("TRIGGERED", "CONFIRMED"),
    ("CONFIRMED", "EXECUTING"),
)
LIFECYCLE_EXECUTING_OUTCOMES = ("TP_HIT", "SL_HIT", "INVALIDATED", "EXPIRED")
LIFECYCLE_ACTIVE_STATES = {
    "DISCOVERED",
    "WATCHLISTED",
    "STALKING",
    "TRIGGERED",
    "CONFIRMED",
    "EXECUTING",
    "MANAGING",
}
LIFECYCLE_STALE_STATES = {"WATCHLISTED", "STALKING", "TRIGGERED", "CONFIRMED"}
LIFECYCLE_PROGRESS_RANK = {
    "DISCOVERED": 0,
    "REJECTED": 1,
    "WATCHLISTED": 2,
    "STALKING": 3,
    "TRIGGERED": 4,
    "CONFIRMED": 5,
    "EXECUTING": 6,
    "MANAGING": 7,
    "TP_HIT": 8,
    "SL_HIT": 8,
    "INVALIDATED": 8,
    "EXPIRED": 8,
}
RESEARCH_QUERIES = (
    "summary",
    "best_symbols",
    "worst_symbols",
    "best_regimes",
    "worst_regimes",
    "rejection_reasons",
    "setup_quality",
    "near_misses",
    "replay_expectancy",
    "mode_performance",
    "symbol_detail",
    "regime_expectancy",
    "regime_setup_density",
    "regime_rejection_patterns",
    "regime_quality_distribution",
    "lifecycle_summary",
    "lifecycle_transitions",
    "lifecycle_conversion",
    "lifecycle_funnel",
    "lifecycle_dropoffs",
    "lifecycle_symbol_conversion",
    "lifecycle_state_duration",
    "lifecycle_symbol_detail",
)
MODES = ("challenge", "swing", "scalp")


class ResearchDatabaseMissing(StorageError):
    """Raised when the local scan database has not been created yet."""


@dataclass(frozen=True)
class ResearchFilters:
    symbol: str | None = None
    mode: str | None = None
    regime: str | None = None
    limit: int = 10
    lifecycle_stale_hours: float = DEFAULT_LIFECYCLE_STALE_HOURS

    @property
    def normalized_symbol(self) -> str | None:
        return self.symbol.strip().upper() if self.symbol else None

    @property
    def normalized_mode(self) -> str | None:
        return self.mode.strip().lower() if self.mode else None

    @property
    def normalized_regime(self) -> str | None:
        return self.regime.strip() if self.regime else None

    @property
    def normalized_limit(self) -> int:
        return max(1, int(self.limit or 10))

    @property
    def normalized_lifecycle_stale_hours(self) -> float:
        try:
            hours = float(self.lifecycle_stale_hours)
        except (TypeError, ValueError):
            return DEFAULT_LIFECYCLE_STALE_HOURS
        return max(0.0, hours)

    def to_json(self) -> dict[str, Any]:
        stale_hours = self.normalized_lifecycle_stale_hours
        return {
            "symbol": self.normalized_symbol or NA,
            "mode": self.normalized_mode or NA,
            "regime": self.normalized_regime or NA,
            "limit": self.normalized_limit,
            "lifecycle_stale_hours": int(stale_hours) if stale_hours.is_integer() else stale_hours,
        }


@dataclass(frozen=True)
class ResearchData:
    runs: tuple[dict[str, Any], ...]
    symbols: tuple[dict[str, Any], ...]
    setups: tuple[dict[str, Any], ...]
    replays: tuple[dict[str, Any], ...]
    lifecycle_records: tuple[dict[str, Any], ...] = ()
    lifecycle_events: tuple[dict[str, Any], ...] = ()


def build_research_report(
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    *,
    query: str = "summary",
    filters: ResearchFilters | None = None,
) -> dict[str, Any]:
    normalized_query = query.strip().lower()
    if normalized_query not in RESEARCH_QUERIES:
        raise ValueError(f"Unsupported research query: {query}")

    active_filters = filters or ResearchFilters()
    data = _load_research_data(database_path, active_filters)
    report = _build_query_report(normalized_query, data, active_filters)
    report.setdefault("query", normalized_query)
    report.setdefault("filters", active_filters.to_json())
    report.setdefault("warnings", [])
    return report


def _load_research_data(database_path: Path | str, filters: ResearchFilters) -> ResearchData:
    path = Path(database_path)
    if not path.exists():
        raise ResearchDatabaseMissing(MISSING_SCAN_DATABASE_MESSAGE)

    try:
        with _connect_read_only(path) as connection:
            connection.row_factory = sqlite3.Row
            _require_schema(connection)
            runs = tuple(_row_dict(row) for row in connection.execute("SELECT * FROM scan_runs").fetchall())
            symbol_rows = tuple(
                _normalize_symbol_row(row)
                for row in connection.execute(
                    """
                    SELECT sr.*, r.timestamp, r.market_regime AS run_market_regime,
                           r.command_preset, r.exchange, r.universe
                    FROM symbol_results sr
                    JOIN scan_runs r ON r.run_id = sr.run_id
                    ORDER BY r.timestamp ASC, sr.id ASC
                    """
                ).fetchall()
            )
            setup_rows = tuple(
                _normalize_setup_row(row)
                for row in connection.execute(
                    """
                    SELECT sc.*, r.timestamp, r.market_regime AS run_market_regime,
                           sr.setup_quality_score, sr.readiness_score,
                           sr.display_bucket, sr.raw_result_json AS symbol_raw_result_json
                    FROM setup_candidates sc
                    JOIN scan_runs r ON r.run_id = sc.run_id
                    LEFT JOIN symbol_results sr ON sr.run_id = sc.run_id AND sr.symbol = sc.symbol
                    ORDER BY r.timestamp ASC, sc.id ASC
                    """
                ).fetchall()
            )
            replay_rows = tuple(
                _normalize_replay_row(row)
                for row in connection.execute(
                    """
                    SELECT rr.*, r.timestamp, r.market_regime AS run_market_regime
                    FROM replay_results rr
                    JOIN scan_runs r ON r.run_id = rr.run_id
                    ORDER BY r.timestamp ASC, rr.id ASC
                    """
                ).fetchall()
            )
            lifecycle_record_rows: tuple[dict[str, Any], ...] = ()
            lifecycle_event_rows: tuple[dict[str, Any], ...] = ()
            if _table_exists(connection, "setup_lifecycle_records"):
                lifecycle_record_rows = tuple(
                    _normalize_lifecycle_record(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM setup_lifecycle_records
                        ORDER BY last_seen_at ASC
                        """
                    ).fetchall()
                )
            if _table_exists(connection, "setup_lifecycle_events"):
                lifecycle_event_rows = tuple(
                    _normalize_lifecycle_event(row)
                    for row in connection.execute(
                        """
                        SELECT e.*, r.mode, r.direction, r.regime_state
                        FROM setup_lifecycle_events e
                        LEFT JOIN setup_lifecycle_records r ON r.lifecycle_id = e.lifecycle_id
                        ORDER BY e.timestamp ASC, e.event_id ASC
                        """
                    ).fetchall()
                )
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to read research database: {database_path}") from exc

    filtered_runs = _filter_runs(runs, filters)
    filtered_symbols = tuple(row for row in symbol_rows if _include_symbol_row(row, filters))
    filtered_setups = tuple(row for row in setup_rows if _include_mode_row(row, filters))
    filtered_replays = tuple(row for row in replay_rows if _include_mode_row(row, filters))
    filtered_lifecycle_records = tuple(row for row in lifecycle_record_rows if _include_lifecycle_row(row, filters))
    filtered_lifecycle_events = tuple(row for row in lifecycle_event_rows if _include_lifecycle_row(row, filters))
    run_ids = {row["run_id"] for row in (*filtered_symbols, *filtered_setups, *filtered_replays)}
    if run_ids:
        filtered_runs = tuple(row for row in filtered_runs if row["run_id"] in run_ids)
    return ResearchData(
        runs=filtered_runs,
        symbols=filtered_symbols,
        setups=filtered_setups,
        replays=filtered_replays,
        lifecycle_records=filtered_lifecycle_records,
        lifecycle_events=filtered_lifecycle_events,
    )


def _connect_read_only(path: Path) -> sqlite3.Connection:
    normalized = str(path.resolve()).replace("\\", "/")
    uri = f"file:{quote(normalized, safe='/:')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _require_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    required = {"scan_runs", "symbol_results", "setup_candidates", "replay_results"}
    missing = required - tables
    if missing:
        raise StorageError(f"Research database is missing required tables: {', '.join(sorted(missing))}")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _build_query_report(query: str, data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    if query == "summary":
        return _summary_report(data, filters)
    if query == "best_symbols":
        return _symbol_ranking_report(data, filters, best=True)
    if query == "worst_symbols":
        return _symbol_ranking_report(data, filters, best=False)
    if query == "best_regimes":
        return _regime_report(data, filters, best=True)
    if query == "worst_regimes":
        return _regime_report(data, filters, best=False)
    if query == "rejection_reasons":
        return _rejection_reasons_report(data, filters)
    if query == "setup_quality":
        return _setup_quality_report(data, filters)
    if query == "near_misses":
        return _near_misses_report(data, filters)
    if query == "replay_expectancy":
        return _replay_expectancy_report(data.replays)
    if query == "mode_performance":
        return _mode_performance_report(data)
    if query == "symbol_detail":
        return _symbol_detail_report(data, filters)
    if query == "regime_expectancy":
        return _regime_expectancy_report(data, filters)
    if query == "regime_setup_density":
        return _regime_setup_density_report(data, filters)
    if query == "regime_rejection_patterns":
        return _regime_rejection_patterns_report(data, filters)
    if query == "regime_quality_distribution":
        return _regime_quality_distribution_report(data, filters)
    if query == "lifecycle_summary":
        return _lifecycle_summary_report(data, filters)
    if query == "lifecycle_transitions":
        return _lifecycle_transitions_report(data, filters)
    if query == "lifecycle_conversion":
        return _lifecycle_conversion_report(data, filters)
    if query == "lifecycle_funnel":
        return _lifecycle_funnel_report(data, filters)
    if query == "lifecycle_dropoffs":
        return _lifecycle_dropoffs_report(data, filters)
    if query == "lifecycle_symbol_conversion":
        return _lifecycle_symbol_conversion_report(data, filters)
    if query == "lifecycle_state_duration":
        return _lifecycle_state_duration_report(data, filters)
    if query == "lifecycle_symbol_detail":
        return _lifecycle_symbol_detail_report(data, filters)
    raise ValueError(f"Unsupported research query: {query}")


def _summary_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    quality_scores = _numeric_values(row.get("setup_quality_score") for row in data.symbols)
    readiness_scores = _numeric_values(row.get("readiness_score") for row in data.symbols)
    rejected_rows = tuple(row for row in data.symbols if row["display_bucket"] in {"near_miss", "no_setup", "data_issue"})
    replay_stats = _replay_stats(data.replays)
    warnings = []
    if replay_stats["total_replay_samples"] and replay_stats["total_replay_samples"] < MIN_RELIABLE_SAMPLE_SIZE:
        warnings.append(SAMPLE_SIZE_WARNING)

    return {
        "query": "summary",
        "filters": filters.to_json(),
        "summary": {
            "total_scan_runs": len({row["run_id"] for row in data.runs}),
            "total_symbols_scanned": len(data.symbols),
            "total_valid_setups": _bucket_count(data.symbols, "valid"),
            "total_near_misses": _bucket_count(data.symbols, "near_miss"),
            "total_rejected": _bucket_count(data.symbols, "no_setup"),
            "total_data_issues": _bucket_count(data.symbols, "data_issue"),
            "total_replay_outcomes": len(data.replays),
            "average_readiness_score": _number(_mean(readiness_scores)),
            "average_quality_score": _number(_mean(quality_scores)),
            "most_common_regime": _most_common_text(row.get("regime_state") for row in data.symbols),
            "most_common_rejection_reason": _most_common_text(
                row.get("rejection_reason") for row in rejected_rows if _display(row.get("rejection_reason")) != NA
            ),
        },
        "replay": replay_stats,
        "warnings": warnings,
    }


def _symbol_ranking_report(data: ResearchData, filters: ResearchFilters, *, best: bool) -> dict[str, Any]:
    rows = [_symbol_metrics(symbol, symbol_rows, data.replays) for symbol, symbol_rows in _group_by(data.symbols, "symbol").items()]
    if best:
        rows.sort(
            key=lambda item: (
                item["valid_setups"],
                _sort_number(item["average_quality_score"]),
                _sort_number(item["replay_expectancy_r"]),
                _sort_number(item["near_miss_quality_score"]),
                _sort_number(item["data_completeness_pct"]),
            ),
            reverse=True,
        )
        query = "best_symbols"
        title = "Best Symbols"
    else:
        rows.sort(
            key=lambda item: (
                item["rejected"] + item["data_issues"],
                -_sort_number(item["average_quality_score"]),
                -_sort_number(item["replay_expectancy_r"]),
                item["data_issues"],
            ),
            reverse=True,
        )
        query = "worst_symbols"
        title = "Worst Symbols"

    return {
        "query": query,
        "filters": filters.to_json(),
        "title": title,
        "symbols": rows[: filters.normalized_limit],
        "warnings": _replay_sample_warnings(data.replays),
    }


def _regime_report(data: ResearchData, filters: ResearchFilters, *, best: bool) -> dict[str, Any]:
    rows = []
    replay_by_regime = _group_by(data.replays, "regime")
    for regime, symbol_rows in _group_by(data.symbols, "regime_state").items():
        replay_stats = _replay_stats(tuple(replay_by_regime.get(regime, ())))
        quality_scores = _numeric_values(row.get("setup_quality_score") for row in symbol_rows)
        rows.append(
            {
                "regime": regime,
                "setups": len(symbol_rows),
                "valid_setups": _bucket_count(symbol_rows, "valid"),
                "near_misses": _bucket_count(symbol_rows, "near_miss"),
                "rejected": _bucket_count(symbol_rows, "no_setup"),
                "data_issues": _bucket_count(symbol_rows, "data_issue"),
                "average_quality_score": _number(_mean(quality_scores)),
                "replay_expectancy_r": replay_stats["expectancy_r"],
                "tp1_rate_pct": replay_stats["tp1_rate_pct"],
                "tp2_rate_pct": replay_stats["tp2_rate_pct"],
                "replay_samples": replay_stats["total_replay_samples"],
            }
        )

    if best:
        rows.sort(
            key=lambda item: (
                item["valid_setups"],
                _sort_number(item["average_quality_score"]),
                _sort_number(item["replay_expectancy_r"]),
                _sort_number(item["tp2_rate_pct"]),
            ),
            reverse=True,
        )
        query = "best_regimes"
        title = "Best Regimes"
    else:
        rows.sort(
            key=lambda item: (
                item["rejected"] + item["data_issues"],
                -_sort_number(item["average_quality_score"]),
                -_sort_number(item["replay_expectancy_r"]),
            ),
            reverse=True,
        )
        query = "worst_regimes"
        title = "Worst Regimes"

    return {
        "query": query,
        "filters": filters.to_json(),
        "title": title,
        "regimes": rows[: filters.normalized_limit],
        "warnings": _replay_sample_warnings(data.replays),
    }


def _rejection_reasons_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    rejected_rows = tuple(row for row in data.symbols if row["display_bucket"] in {"near_miss", "no_setup", "data_issue"})
    total = len(rejected_rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rejected_rows:
        grouped[_display(row.get("failed_gate"))].append(row)

    reasons = []
    for gate, rows in grouped.items():
        symbols = sorted({row["symbol"] for row in rows})
        reasons.append(
            {
                "failed_gate": gate,
                "count": len(rows),
                "percentage": _rate(len(rows), total),
                "affected_symbols": symbols,
                "affected_symbol_count": len(symbols),
                "possible_interpretation": _gate_interpretation(gate),
            }
        )
    reasons.sort(key=lambda item: (item["count"], item["affected_symbol_count"], item["failed_gate"]), reverse=True)
    return {
        "query": "rejection_reasons",
        "filters": filters.to_json(),
        "total_rejections": total,
        "reasons": reasons[: filters.normalized_limit],
        "warnings": [],
    }


def _setup_quality_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    return {
        "query": "setup_quality",
        "filters": filters.to_json(),
        "total_symbols": len(data.symbols),
        "average_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in data.symbols))),
        "quality_grades": _conversion_groups(data.symbols, "quality_grade"),
        "readiness_labels": _conversion_groups(data.symbols, "readiness_label"),
        "quality_states": _conversion_groups(data.symbols, "quality_state"),
        "warnings": [],
    }


def _near_misses_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    near_rows = tuple(row for row in data.symbols if row["display_bucket"] == "near_miss")
    valid_timestamps_by_symbol: dict[str, list[str]] = defaultdict(list)
    for row in data.symbols:
        if row["display_bucket"] == "valid":
            valid_timestamps_by_symbol[row["symbol"]].append(row["timestamp"])

    symbols = []
    for symbol, rows in _group_by(near_rows, "symbol").items():
        timestamps = valid_timestamps_by_symbol.get(symbol, [])
        later_became_valid = any(
            valid_timestamp > row["timestamp"]
            for row in rows
            for valid_timestamp in timestamps
            if row.get("timestamp") and valid_timestamp
        )
        symbols.append(
            {
                "symbol": symbol,
                "near_miss_count": len(rows),
                "most_common_failed_gate": _most_common_text(row.get("failed_gate") for row in rows),
                "most_common_next_trigger_needed": _most_common_text(row.get("next_trigger_needed") for row in rows),
                "average_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in rows))),
                "later_became_valid": later_became_valid,
                "latest_near_miss": max((row["timestamp"] for row in rows), default=NA),
            }
        )
    symbols.sort(
        key=lambda item: (
            item["near_miss_count"],
            _sort_number(item["average_quality_score"]),
            item["symbol"],
        ),
        reverse=True,
    )
    return {
        "query": "near_misses",
        "filters": filters.to_json(),
        "total_near_misses": len(near_rows),
        "most_common_failed_gate": _most_common_text(row.get("failed_gate") for row in near_rows),
        "most_common_next_trigger_needed": _most_common_text(row.get("next_trigger_needed") for row in near_rows),
        "symbols": symbols[: filters.normalized_limit],
        "warnings": [],
    }


def _replay_expectancy_report(replays: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stats = _replay_stats(replays)
    warnings = []
    if stats["total_replay_samples"] < MIN_RELIABLE_SAMPLE_SIZE:
        warnings.append(SAMPLE_SIZE_WARNING)
    return {
        "query": "replay_expectancy",
        "replay": stats,
        "warnings": warnings,
    }


def _mode_performance_report(data: ResearchData) -> dict[str, Any]:
    rows = []
    for mode in MODES:
        symbol_rows = tuple(row for row in data.symbols if mode in row.get("modes", ()))
        setup_rows = tuple(row for row in data.setups if row["mode"] == mode)
        replay_rows = tuple(row for row in data.replays if row["mode"] == mode)
        replay_stats = _replay_stats(replay_rows)
        rows.append(
            {
                "mode": mode,
                "symbols_scanned": len(symbol_rows),
                "setup_candidates": len(setup_rows),
                "valid_setups": _bucket_count(symbol_rows, "valid"),
                "near_misses": _bucket_count(symbol_rows, "near_miss"),
                "rejected": _bucket_count(symbol_rows, "no_setup"),
                "average_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in symbol_rows))),
                "replay_samples": replay_stats["total_replay_samples"],
                "expectancy_r": replay_stats["expectancy_r"],
                "tp1_rate_pct": replay_stats["tp1_rate_pct"],
                "tp2_rate_pct": replay_stats["tp2_rate_pct"],
            }
        )
    return {
        "query": "mode_performance",
        "modes": rows,
        "warnings": _replay_sample_warnings(data.replays),
    }


def _symbol_detail_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    symbol = filters.normalized_symbol
    if symbol is None:
        return {
            "query": "symbol_detail",
            "filters": filters.to_json(),
            "error": "Provide --research-symbol for symbol_detail.",
            "warnings": [],
        }
    symbol_rows = tuple(row for row in data.symbols if row["symbol"] == symbol)
    replay_rows = tuple(row for row in data.replays if row["symbol"] == symbol)
    non_valid = tuple(row for row in symbol_rows if row["display_bucket"] != "valid")
    recent = sorted(symbol_rows, key=lambda row: row.get("timestamp", ""), reverse=True)[: filters.normalized_limit]
    replay_stats = _replay_stats(replay_rows)
    warnings = []
    if replay_stats["total_replay_samples"] and replay_stats["total_replay_samples"] < MIN_RELIABLE_SAMPLE_SIZE:
        warnings.append(SAMPLE_SIZE_WARNING)
    return {
        "query": "symbol_detail",
        "filters": filters.to_json(),
        "symbol": symbol,
        "scan_count": len(symbol_rows),
        "valid_setup_count": _bucket_count(symbol_rows, "valid"),
        "near_miss_count": _bucket_count(symbol_rows, "near_miss"),
        "rejected_count": _bucket_count(symbol_rows, "no_setup"),
        "data_issue_count": _bucket_count(symbol_rows, "data_issue"),
        "most_common_failed_gate": _most_common_text(row.get("failed_gate") for row in non_valid),
        "average_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in symbol_rows))),
        "replay": replay_stats,
        "recent_history": [
            {
                "timestamp": row["timestamp"],
                "display_bucket": row["display_bucket"],
                "failed_gate": row["failed_gate"],
                "rejection_reason": row["rejection_reason"],
                "quality_score": row["setup_quality_score"],
                "quality_state": row["quality_state"],
                "readiness_score": row["readiness_score"],
                "readiness_label": row["readiness_label"],
                "next_trigger_needed": row["next_trigger_needed"],
            }
            for row in recent
        ],
        "warnings": warnings,
    }


def _regime_expectancy_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    rows = []
    symbols_by_regime = _group_by(data.symbols, "regime_state")
    replays_by_regime = _group_by(data.replays, "regime")
    for regime, symbol_rows in symbols_by_regime.items():
        replay_stats = _replay_stats(tuple(replays_by_regime.get(regime, ())))
        confidence_values = _numeric_values(row.get("regime_confidence") for row in symbol_rows)
        rows.append(
            {
                "regime": regime,
                "average_confidence": _number(_mean(confidence_values)),
                "symbols_scanned": len(symbol_rows),
                "valid_setups": _bucket_count(symbol_rows, "valid"),
                "near_misses": _bucket_count(symbol_rows, "near_miss"),
                "replay_samples": replay_stats["total_replay_samples"],
                "expectancy_r": replay_stats["expectancy_r"],
                "win_rate_pct": replay_stats["win_rate_pct"],
                "tp1_rate_pct": replay_stats["tp1_rate_pct"],
                "tp2_rate_pct": replay_stats["tp2_rate_pct"],
            }
        )
    rows.sort(
        key=lambda item: (
            _sort_number(item["expectancy_r"]),
            item["valid_setups"],
            _sort_number(item["average_confidence"]),
        ),
        reverse=True,
    )
    return {
        "query": "regime_expectancy",
        "filters": filters.to_json(),
        "regimes": rows[: filters.normalized_limit],
        "warnings": _replay_sample_warnings(data.replays),
    }


def _regime_setup_density_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    rows = []
    setup_counts: dict[str, int] = defaultdict(int)
    for setup in data.setups:
        setup_counts[_display(setup.get("regime"))] += 1
    for regime, symbol_rows in _group_by(data.symbols, "regime_state").items():
        valid = _bucket_count(symbol_rows, "valid")
        near = _bucket_count(symbol_rows, "near_miss")
        rows.append(
            {
                "regime": regime,
                "symbols_scanned": len(symbol_rows),
                "setup_candidates": setup_counts.get(regime, 0),
                "valid_setups": valid,
                "near_misses": near,
                "rejected": _bucket_count(symbol_rows, "no_setup"),
                "data_issues": _bucket_count(symbol_rows, "data_issue"),
                "setup_density_pct": _rate(valid + near, len(symbol_rows)),
                "valid_density_pct": _rate(valid, len(symbol_rows)),
            }
        )
    rows.sort(key=lambda item: (_sort_number(item["setup_density_pct"]), item["setup_candidates"]), reverse=True)
    return {
        "query": "regime_setup_density",
        "filters": filters.to_json(),
        "regimes": rows[: filters.normalized_limit],
        "warnings": [],
    }


def _regime_rejection_patterns_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    rows = []
    for regime, symbol_rows in _group_by(data.symbols, "regime_state").items():
        rejected_rows = tuple(row for row in symbol_rows if row["display_bucket"] in {"near_miss", "no_setup", "data_issue"})
        gates = []
        for gate, gate_rows in _group_by(rejected_rows, "failed_gate").items():
            gates.append(
                {
                    "failed_gate": gate,
                    "count": len(gate_rows),
                    "percentage": _rate(len(gate_rows), len(rejected_rows)),
                    "affected_symbols": sorted({row["symbol"] for row in gate_rows}),
                    "possible_interpretation": _gate_interpretation(gate),
                }
            )
        gates.sort(key=lambda item: (item["count"], item["failed_gate"]), reverse=True)
        rows.append(
            {
                "regime": regime,
                "total_rejections": len(rejected_rows),
                "most_common_failed_gate": gates[0]["failed_gate"] if gates else NA,
                "patterns": gates[: filters.normalized_limit],
            }
        )
    rows.sort(key=lambda item: item["total_rejections"], reverse=True)
    return {
        "query": "regime_rejection_patterns",
        "filters": filters.to_json(),
        "regimes": rows[: filters.normalized_limit],
        "warnings": [],
    }


def _regime_quality_distribution_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    rows = []
    for regime, symbol_rows in _group_by(data.symbols, "regime_state").items():
        rows.append(
            {
                "regime": regime,
                "symbols_scanned": len(symbol_rows),
                "average_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in symbol_rows))),
                "average_readiness_score": _number(_mean(_numeric_values(row.get("readiness_score") for row in symbol_rows))),
                "quality_grades": _conversion_groups(symbol_rows, "quality_grade"),
                "quality_states": _conversion_groups(symbol_rows, "quality_state"),
                "compatibility_labels": _conversion_groups(symbol_rows, "regime_compatibility_label"),
            }
        )
    rows.sort(key=lambda item: (_sort_number(item["average_quality_score"]), item["symbols_scanned"]), reverse=True)
    return {
        "query": "regime_quality_distribution",
        "filters": filters.to_json(),
        "regimes": rows[: filters.normalized_limit],
        "warnings": [],
    }


def _lifecycle_summary_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    records = data.lifecycle_records
    events = data.lifecycle_events
    analytics = _lifecycle_analytics(data, filters)
    return {
        "query": "lifecycle_summary",
        "filters": filters.to_json(),
        "total_lifecycles": analytics["total_lifecycles"],
        "active_lifecycles": analytics["active_lifecycles"],
        "archived_lifecycles": analytics["archived_lifecycles"],
        "states": _state_counts(records),
        "average_time_in_state_seconds": analytics["state_duration_stats"]["states"],
        "state_duration_stats": analytics["state_duration_stats"],
        "stale_lifecycles": analytics["stale_lifecycles"],
        "most_common_invalidation_reason": _most_common_text(
            row.get("invalidation_reason")
            for row in records
            if row.get("current_state") in {"INVALIDATED", "COOLDOWN", "ARCHIVED", "EXPIRED", "SL_HIT"}
        ),
        "warnings": [],
    }


def _lifecycle_transitions_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    recent = sorted(data.lifecycle_events, key=lambda row: row.get("timestamp", ""), reverse=True)[
        : filters.normalized_limit
    ]
    return {
        "query": "lifecycle_transitions",
        "filters": filters.to_json(),
        "total_transitions": len(data.lifecycle_events),
        "transitions": [
            {
                "timestamp": row["timestamp"],
                "symbol": row["symbol"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "reason": row["reason"],
                "scan_run_id": _display(row.get("scan_run_id")),
                "readiness_score": row["readiness_score"],
                "quality_score": row["quality_score"],
                "failed_gate": row["failed_gate"],
                "notes": row["notes"],
            }
            for row in recent
        ],
        "warnings": [],
    }


def _lifecycle_conversion_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    analytics = _lifecycle_analytics(data, filters)
    return {
        "query": "lifecycle_conversion",
        "filters": filters.to_json(),
        **analytics,
        "warnings": [],
    }


def _lifecycle_funnel_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    return {
        "query": "lifecycle_funnel",
        "filters": filters.to_json(),
        **_lifecycle_analytics(data, filters),
        "warnings": [],
    }


def _lifecycle_dropoffs_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    return {
        "query": "lifecycle_dropoffs",
        "filters": filters.to_json(),
        **_lifecycle_analytics(data, filters),
        "warnings": [],
    }


def _lifecycle_symbol_conversion_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    return {
        "query": "lifecycle_symbol_conversion",
        "filters": filters.to_json(),
        **_lifecycle_analytics(data, filters),
        "warnings": [],
    }


def _lifecycle_state_duration_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    return {
        "query": "lifecycle_state_duration",
        "filters": filters.to_json(),
        **_lifecycle_analytics(data, filters),
        "warnings": [],
    }


def _lifecycle_symbol_detail_report(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    symbol = filters.normalized_symbol
    if symbol is None:
        return {
            "query": "lifecycle_symbol_detail",
            "filters": filters.to_json(),
            "error": "Provide --research-symbol for lifecycle_symbol_detail.",
            "warnings": [],
        }
    records = tuple(row for row in data.lifecycle_records if row["symbol"] == symbol)
    events = tuple(row for row in data.lifecycle_events if row["symbol"] == symbol)
    recent = sorted(events, key=lambda row: row.get("timestamp", ""), reverse=True)[: filters.normalized_limit]
    return {
        "query": "lifecycle_symbol_detail",
        "filters": filters.to_json(),
        "symbol": symbol,
        "lifecycles": [
            {
                "lifecycle_id": row["lifecycle_id"],
                "mode": row["mode"],
                "direction": row["direction"],
                "current_state": row["current_state"],
                "previous_state": row["previous_state"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "failed_gate": row["failed_gate"],
                "readiness_score": row["readiness_score"],
                "quality_score": row["quality_score"],
                "invalidation_reason": row["invalidation_reason"],
            }
            for row in records
        ],
        "recent_transitions": [
            {
                "timestamp": row["timestamp"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "reason": row["reason"],
                "failed_gate": row["failed_gate"],
                "notes": row["notes"],
            }
            for row in recent
        ],
        "warnings": [],
    }


def _lifecycle_analytics(data: ResearchData, filters: ResearchFilters) -> dict[str, Any]:
    paths = _lifecycle_paths(data.lifecycle_records, data.lifecycle_events)
    ids_by_state = {
        state: {path["lifecycle_id"] for path in paths if _path_reached_state(path, state)}
        for state in (*LIFECYCLE_FUNNEL_STATES, "MANAGING", "REJECTED")
    }
    funnel_counts = {
        state: len(ids_by_state.get(state, set()))
        for state in LIFECYCLE_FUNNEL_STATES
    }
    conversion_counts = _lifecycle_conversion_counts(ids_by_state)
    conversion_rates = {
        "watchlisted_to_stalking_pct": conversion_counts["WATCHLISTED_to_STALKING"]["conversion_rate_pct"],
        "stalking_to_triggered_pct": conversion_counts["STALKING_to_TRIGGERED"]["conversion_rate_pct"],
        "triggered_to_confirmed_pct": conversion_counts["TRIGGERED_to_CONFIRMED"]["conversion_rate_pct"],
        "confirmed_to_executing_pct": conversion_counts["CONFIRMED_to_EXECUTING"]["conversion_rate_pct"],
        "executing_to_tp_hit_pct": conversion_counts["EXECUTING_to_TP_HIT"]["conversion_rate_pct"],
        "executing_to_sl_hit_pct": conversion_counts["EXECUTING_to_SL_HIT"]["conversion_rate_pct"],
        "executing_to_invalidated_pct": conversion_counts["EXECUTING_to_INVALIDATED"]["conversion_rate_pct"],
        "executing_to_expired_pct": conversion_counts["EXECUTING_to_EXPIRED"]["conversion_rate_pct"],
    }
    watchlisted_ids = ids_by_state.get("WATCHLISTED", set())
    triggered_ids = ids_by_state.get("TRIGGERED", set())
    confirmed_ids = ids_by_state.get("CONFIRMED", set())
    valid_ids = {
        path["lifecycle_id"]
        for path in paths
        if any(_path_reached_state(path, state) for state in ("CONFIRMED", "EXECUTING", "MANAGING", "TP_HIT", "SL_HIT"))
    }
    state_duration_stats = _lifecycle_state_duration_stats(
        data.lifecycle_events,
        data.lifecycle_records,
        filters=filters,
        as_of=_lifecycle_analysis_as_of(data.lifecycle_records, data.lifecycle_events),
    )
    return {
        "total_lifecycles": len(paths),
        "active_lifecycles": sum(1 for path in paths if path["current_state"] in LIFECYCLE_ACTIVE_STATES),
        "archived_lifecycles": sum(
            1
            for path in paths
            if path["current_state"] == "ARCHIVED" or _display(path["record"].get("archived_at")) != NA
        ),
        "funnel_counts": funnel_counts,
        "conversion_counts": conversion_counts,
        "conversion_rates": conversion_rates,
        "dropoff_stats": _lifecycle_dropoff_stats(paths),
        "state_duration_stats": state_duration_stats,
        "stale_lifecycles": state_duration_stats["stale_lifecycles"],
        "per_symbol_conversion": _per_symbol_lifecycle_conversion(paths),
        "watchlisted_to_valid": {
            "watchlisted_count": len(watchlisted_ids),
            "valid_count": len(watchlisted_ids & valid_ids),
            "conversion_rate_pct": _rate(len(watchlisted_ids & valid_ids), len(watchlisted_ids)),
        },
        "triggered_to_confirmed": {
            "triggered_count": len(triggered_ids),
            "confirmed_count": len(triggered_ids & confirmed_ids),
            "conversion_rate_pct": _rate(len(triggered_ids & confirmed_ids), len(triggered_ids)),
        },
        "confirmed_outcomes": {
            "confirmed_count": len(confirmed_ids),
            "tp_hit_count": len(confirmed_ids & ids_by_state.get("TP_HIT", set())),
            "sl_hit_count": len(confirmed_ids & ids_by_state.get("SL_HIT", set())),
            "tp_hit_rate_pct": _rate(len(confirmed_ids & ids_by_state.get("TP_HIT", set())), len(confirmed_ids)),
            "sl_hit_rate_pct": _rate(len(confirmed_ids & ids_by_state.get("SL_HIT", set())), len(confirmed_ids)),
        },
    }


def _lifecycle_paths(
    records: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records_by_lifecycle = {
        _display(row.get("lifecycle_id")): row
        for row in records
        if _display(row.get("lifecycle_id")) != NA
    }
    events_by_lifecycle = _group_by(events, "lifecycle_id")
    lifecycle_ids = sorted(
        lifecycle_id
        for lifecycle_id in set(records_by_lifecycle) | set(events_by_lifecycle)
        if lifecycle_id != NA
    )
    return [
        _lifecycle_path(
            lifecycle_id,
            records_by_lifecycle.get(lifecycle_id, {}),
            events_by_lifecycle.get(lifecycle_id, ()),
        )
        for lifecycle_id in lifecycle_ids
    ]


def _lifecycle_path(
    lifecycle_id: str,
    record: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ordered_events = sorted(events, key=lambda row: (row.get("timestamp", ""), int(row.get("event_id") or 0)))
    states: list[str] = []
    first_seen_by_state: dict[str, datetime] = {}
    for event in ordered_events:
        from_state = _display(event.get("from_state"))
        if from_state != NA and not states:
            states.append(from_state)
        to_state = _display(event.get("to_state"))
        if to_state == NA:
            continue
        states.append(to_state)
        timestamp = _parse_time(event.get("timestamp"))
        if timestamp is not None:
            first_seen_by_state.setdefault(to_state, timestamp)

    current_state = _display(record.get("current_state"))
    if current_state == NA and ordered_events:
        current_state = _display(ordered_events[-1].get("to_state"))
    if current_state != NA and current_state not in states:
        states.append(current_state)
    if current_state != NA:
        current_timestamp = (
            _parse_time(record.get("last_transition_at"))
            or _parse_time(record.get("last_seen_at"))
            or _parse_time(record.get("first_seen_at"))
        )
        if current_timestamp is not None:
            first_seen_by_state.setdefault(current_state, current_timestamp)

    first_seen = _parse_time(record.get("first_seen_at"))
    if first_seen is None and ordered_events:
        first_seen = _parse_time(ordered_events[0].get("timestamp"))
    highest_state = _highest_lifecycle_state(states)
    highest_at = first_seen_by_state.get(highest_state)
    time_to_highest_seconds = (
        Decimal(str((highest_at - first_seen).total_seconds()))
        if first_seen is not None and highest_at is not None and highest_at >= first_seen
        else None
    )
    return {
        "lifecycle_id": lifecycle_id,
        "record": record,
        "events": tuple(ordered_events),
        "states": tuple(states),
        "reached_states": frozenset(states),
        "current_state": current_state,
        "highest_state": highest_state,
        "highest_state_at": highest_at.isoformat() if highest_at is not None else NA,
        "time_to_highest_state_seconds": _number(time_to_highest_seconds),
        "dropoff_stage": _lifecycle_dropoff_stage(tuple(states)),
        "symbol": _display(record.get("symbol") or (ordered_events[-1].get("symbol") if ordered_events else NA)).upper(),
    }


def _lifecycle_conversion_counts(ids_by_state: Mapping[str, set[str]]) -> dict[str, dict[str, Any]]:
    counts = {}
    for from_state, to_state in LIFECYCLE_CONVERSION_STEPS:
        from_ids = ids_by_state.get(from_state, set())
        to_ids = ids_by_state.get(to_state, set())
        converted = from_ids & to_ids
        counts[f"{from_state}_to_{to_state}"] = {
            "from_state": from_state,
            "to_state": to_state,
            "from_count": len(from_ids),
            "to_count": len(converted),
            "conversion_rate_pct": _rate(len(converted), len(from_ids)),
        }
    executing_ids = ids_by_state.get("EXECUTING", set())
    for outcome in LIFECYCLE_EXECUTING_OUTCOMES:
        outcome_ids = executing_ids & ids_by_state.get(outcome, set())
        counts[f"EXECUTING_to_{outcome}"] = {
            "from_state": "EXECUTING",
            "to_state": outcome,
            "from_count": len(executing_ids),
            "to_count": len(outcome_ids),
            "conversion_rate_pct": _rate(len(outcome_ids), len(executing_ids)),
        }
    return counts


def _lifecycle_dropoff_stats(paths: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dropoffs = [path for path in paths if _display(path.get("dropoff_stage")) != NA]
    records = [path["record"] for path in dropoffs if isinstance(path.get("record"), Mapping)]
    stage_counter = Counter(_display(path.get("dropoff_stage")) for path in dropoffs if _display(path.get("dropoff_stage")) != NA)
    failed_gate_counter = Counter(
        _display(record.get("failed_gate"))
        for record in records
        if _display(record.get("failed_gate")) != NA
    )
    invalidation_counter = Counter(
        _display(record.get("invalidation_reason"))
        for record in records
        if _display(record.get("invalidation_reason")) != NA
    )
    regime_counter = Counter(
        _display(record.get("regime_state"))
        for record in records
        if _display(record.get("regime_state")) != NA
    )
    return {
        "dropoff_lifecycle_count": len(dropoffs),
        "biggest_dropoff_stage": stage_counter.most_common(1)[0][0] if stage_counter else NA,
        "dropoff_stages": _counter_rows(stage_counter, "stage"),
        "most_common_failed_gate": failed_gate_counter.most_common(1)[0][0] if failed_gate_counter else NA,
        "failed_gate_counts": _counter_rows(failed_gate_counter, "failed_gate"),
        "most_common_invalidation_reason": invalidation_counter.most_common(1)[0][0] if invalidation_counter else NA,
        "invalidation_reason_counts": _counter_rows(invalidation_counter, "invalidation_reason"),
        "average_readiness_score": _number(_mean(_numeric_values(record.get("readiness_score") for record in records))),
        "average_quality_score": _number(_mean(_numeric_values(record.get("quality_score") for record in records))),
        "most_common_regime_state": regime_counter.most_common(1)[0][0] if regime_counter else NA,
        "regime_state_counts": _counter_rows(regime_counter, "regime_state"),
    }


def _per_symbol_lifecycle_conversion(paths: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for symbol, symbol_paths in _group_by(paths, "symbol").items():
        lifecycle_count = len(symbol_paths)
        confirmed = sum(1 for path in symbol_paths if _path_reached_state(path, "CONFIRMED"))
        executing = sum(1 for path in symbol_paths if _path_reached_state(path, "EXECUTING"))
        dropoff_counter = Counter(
            _display(path.get("dropoff_stage"))
            for path in symbol_paths
            if _display(path.get("dropoff_stage")) != NA
        )
        rows.append(
            {
                "symbol": symbol,
                "lifecycle_count": lifecycle_count,
                "highest_state_reached": _highest_lifecycle_state(
                    tuple(_display(path.get("highest_state")) for path in symbol_paths)
                ),
                "conversion_to_confirmed_pct": _rate(confirmed, lifecycle_count),
                "conversion_to_executing_pct": _rate(executing, lifecycle_count),
                "average_time_to_highest_state_seconds": _number(
                    _mean(_numeric_values(path.get("time_to_highest_state_seconds") for path in symbol_paths))
                ),
                "most_common_failure_point": dropoff_counter.most_common(1)[0][0] if dropoff_counter else NA,
            }
        )
    rows.sort(
        key=lambda item: (
            item["lifecycle_count"],
            _sort_number(item["conversion_to_executing_pct"]),
            _sort_number(item["conversion_to_confirmed_pct"]),
            item["symbol"],
        ),
        reverse=True,
    )
    return rows


def _lifecycle_state_duration_stats(
    events: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    *,
    filters: ResearchFilters,
    as_of: datetime | None,
) -> dict[str, Any]:
    durations_by_state = _lifecycle_durations_by_state(events, records)
    state_rows = []
    for state, durations in durations_by_state.items():
        state_rows.append(
            {
                "state": state,
                "average_seconds": _number(_mean(durations)),
                "median_seconds": _number(Decimal(str(median(durations))) if durations else None),
                "longest_seconds": _number(max(durations) if durations else None),
                "samples": len(durations),
            }
        )
    state_rows.sort(key=lambda item: item["state"])
    stuck_rows = _stuck_lifecycle_rows(records, as_of, filters)
    stale_rows = [
        row
        for row in stuck_rows
        if row["current_state"] in LIFECYCLE_STALE_STATES
        and row["_seconds_in_state"] >= Decimal(str(filters.normalized_lifecycle_stale_hours)) * Decimal("3600")
    ]
    return {
        "analysis_as_of": as_of.isoformat() if as_of is not None else NA,
        "stale_after_hours": _number(Decimal(str(filters.normalized_lifecycle_stale_hours))),
        "states": state_rows,
        "longest_stuck_symbols": [_public_stuck_row(row) for row in stuck_rows[: filters.normalized_limit]],
        "stale_lifecycle_count": len(stale_rows),
        "stale_lifecycles": [_public_stuck_row(row) for row in stale_rows],
    }


def _lifecycle_durations_by_state(
    events: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, list[Decimal]]:
    durations_by_state: dict[str, list[Decimal]] = defaultdict(list)
    if not events and not records:
        return durations_by_state
    last_seen_by_lifecycle = {
        row["lifecycle_id"]: _parse_time(row.get("last_seen_at"))
        for row in records
    }
    events_by_lifecycle = _group_by(events, "lifecycle_id")
    for lifecycle_id, lifecycle_events in events_by_lifecycle.items():
        ordered = sorted(lifecycle_events, key=lambda row: (row.get("timestamp", ""), int(row.get("event_id") or 0)))
        for index, event in enumerate(ordered):
            started = _parse_time(event.get("timestamp"))
            if started is None:
                continue
            if index + 1 < len(ordered):
                ended = _parse_time(ordered[index + 1].get("timestamp"))
            else:
                ended = last_seen_by_lifecycle.get(lifecycle_id)
            if ended is None or ended < started:
                continue
            durations_by_state[_display(event.get("to_state"))].append(Decimal(str((ended - started).total_seconds())))

    event_lifecycle_ids = set(events_by_lifecycle)
    for record in records:
        lifecycle_id = _display(record.get("lifecycle_id"))
        if lifecycle_id in event_lifecycle_ids:
            continue
        started = _parse_time(record.get("first_seen_at"))
        ended = _parse_time(record.get("last_seen_at"))
        if started is None or ended is None or ended < started:
            continue
        durations_by_state[_display(record.get("current_state"))].append(Decimal(str((ended - started).total_seconds())))
    return durations_by_state


def _stuck_lifecycle_rows(
    records: Sequence[Mapping[str, Any]],
    as_of: datetime | None,
    filters: ResearchFilters,
) -> list[dict[str, Any]]:
    if as_of is None:
        return []
    rows = []
    for record in records:
        current_state = _display(record.get("current_state"))
        if current_state not in LIFECYCLE_ACTIVE_STATES:
            continue
        started = (
            _parse_time(record.get("last_transition_at"))
            or _parse_time(record.get("last_seen_at"))
            or _parse_time(record.get("first_seen_at"))
        )
        if started is None or as_of < started:
            continue
        seconds = Decimal(str((as_of - started).total_seconds()))
        rows.append(
            {
                "lifecycle_id": record.get("lifecycle_id"),
                "symbol": record.get("symbol"),
                "mode": record.get("mode"),
                "direction": record.get("direction"),
                "current_state": current_state,
                "last_transition_at": record.get("last_transition_at"),
                "hours_in_state": _number(seconds / Decimal("3600")),
                "stale_after_hours": _number(Decimal(str(filters.normalized_lifecycle_stale_hours))),
                "failed_gate": record.get("failed_gate"),
                "readiness_score": record.get("readiness_score"),
                "quality_score": record.get("quality_score"),
                "_seconds_in_state": seconds,
            }
        )
    rows.sort(key=lambda item: (item["_seconds_in_state"], str(item["symbol"])), reverse=True)
    return rows


def _public_stuck_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _lifecycle_analysis_as_of(
    records: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> datetime | None:
    timestamps: list[datetime] = []
    for record in records:
        for key in ("last_seen_at", "last_transition_at", "first_seen_at"):
            parsed = _parse_time(record.get(key))
            if parsed is not None:
                timestamps.append(parsed)
    for event in events:
        parsed = _parse_time(event.get("timestamp"))
        if parsed is not None:
            timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def _path_reached_state(path: Mapping[str, Any], state: str) -> bool:
    reached = path.get("reached_states")
    return isinstance(reached, frozenset) and state in reached


def _highest_lifecycle_state(states: Sequence[Any]) -> str:
    best_state = NA
    best_rank = -1
    for value in states:
        state = _display(value)
        rank = LIFECYCLE_PROGRESS_RANK.get(state, -1)
        if rank >= best_rank and state != NA:
            best_state = state
            best_rank = rank
    return best_state


def _lifecycle_dropoff_stage(states: Sequence[str]) -> str:
    state_set = set(states)
    for outcome in ("SL_HIT", "INVALIDATED", "EXPIRED"):
        if outcome in state_set:
            previous_stage = _previous_progress_stage(states, outcome)
            return previous_stage if previous_stage != NA else outcome
    if "REJECTED" in state_set and "WATCHLISTED" not in state_set:
        return "DISCOVERED"
    if "DISCOVERED" in state_set and "WATCHLISTED" not in state_set:
        return "DISCOVERED"
    for from_state, to_state in LIFECYCLE_CONVERSION_STEPS:
        if from_state in state_set and to_state not in state_set:
            return from_state
    if "EXECUTING" in state_set and not any(outcome in state_set for outcome in LIFECYCLE_EXECUTING_OUTCOMES):
        return "EXECUTING"
    return NA


def _previous_progress_stage(states: Sequence[str], marker_state: str) -> str:
    try:
        marker_index = next(index for index, state in enumerate(states) if state == marker_state)
    except StopIteration:
        return NA
    for state in reversed(states[:marker_index]):
        if state in LIFECYCLE_ACTIVE_STATES or state == "MANAGING":
            return state
    return NA


def _counter_rows(counter: Counter[str], key: str) -> list[dict[str, Any]]:
    return [
        {key: value, "count": count}
        for value, count in sorted(counter.items(), key=lambda item: (item[1], item[0]), reverse=True)
    ]


def _symbol_metrics(symbol: str, rows: Sequence[Mapping[str, Any]], replays: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    symbol_replays = tuple(row for row in replays if row["symbol"] == symbol)
    replay_stats = _replay_stats(symbol_replays)
    near_rows = tuple(row for row in rows if row["display_bucket"] == "near_miss")
    data_issue_count = sum(1 for row in rows if row["display_bucket"] == "data_issue" or row.get("has_data_issue"))
    return {
        "symbol": symbol,
        "scan_count": len(rows),
        "valid_setups": _bucket_count(rows, "valid"),
        "near_misses": _bucket_count(rows, "near_miss"),
        "rejected": _bucket_count(rows, "no_setup"),
        "data_issues": data_issue_count,
        "average_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in rows))),
        "replay_samples": replay_stats["total_replay_samples"],
        "replay_expectancy_r": replay_stats["expectancy_r"],
        "tp1_rate_pct": replay_stats["tp1_rate_pct"],
        "tp2_rate_pct": replay_stats["tp2_rate_pct"],
        "near_miss_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in near_rows))),
        "data_completeness_pct": _rate(max(0, len(rows) - data_issue_count), len(rows)),
    }


def _replay_stats(replays: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    filled = tuple(row for row in replays if int(row.get("filled") or 0) == 1)
    r_values = _numeric_values(row.get("final_r") for row in filled)
    tp_numbers = tuple(_tp_number(row.get("tp_hit")) for row in filled)
    wins = sum(1 for value in r_values if value > 0)
    total = len(replays)
    filled_count = len(filled)
    return {
        "total_replay_samples": total,
        "filled_samples": filled_count,
        "avg_r": _number(_mean(r_values)),
        "median_r": _number(Decimal(str(median(r_values))) if r_values else None),
        "win_rate_pct": _rate(wins, len(r_values)),
        "tp1_rate_pct": _rate(sum(1 for value in tp_numbers if value >= 1), filled_count),
        "tp2_rate_pct": _rate(sum(1 for value in tp_numbers if value >= 2), filled_count),
        "stop_rate_pct": _rate(sum(1 for row in filled if int(row.get("sl_hit") or 0) == 1), filled_count),
        "expectancy_r": _number(_mean(r_values)),
    }


def _conversion_groups(rows: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped = _group_by(rows, key)
    output = []
    for value, group_rows in grouped.items():
        valid = _bucket_count(group_rows, "valid")
        output.append(
            {
                key: value,
                "count": len(group_rows),
                "valid_setup_count": valid,
                "conversion_to_valid_pct": _rate(valid, len(group_rows)),
                "average_quality_score": _number(_mean(_numeric_values(row.get("setup_quality_score") for row in group_rows))),
            }
        )
    output.sort(key=lambda item: (item["count"], _sort_number(item["average_quality_score"])), reverse=True)
    return output


def _state_counts(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    total = len(rows)
    output = []
    for state, state_rows in _group_by(rows, "current_state").items():
        output.append(
            {
                "state": state,
                "count": len(state_rows),
                "percentage": _rate(len(state_rows), total),
            }
        )
    output.sort(key=lambda item: (item["count"], item["state"]), reverse=True)
    return output


def _parse_time(value: Any) -> datetime | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _filter_runs(rows: Sequence[Mapping[str, Any]], filters: ResearchFilters) -> tuple[dict[str, Any], ...]:
    regime = filters.normalized_regime
    if regime is None:
        return tuple(dict(row) for row in rows)
    return tuple(dict(row) for row in rows if _display(row.get("market_regime")) == regime)


def _include_symbol_row(row: Mapping[str, Any], filters: ResearchFilters) -> bool:
    symbol = filters.normalized_symbol
    mode = filters.normalized_mode
    regime = filters.normalized_regime
    if symbol is not None and row["symbol"] != symbol:
        return False
    if mode is not None and mode not in row.get("modes", ()):
        return False
    if regime is not None and row["regime_state"] != regime:
        return False
    return True


def _include_mode_row(row: Mapping[str, Any], filters: ResearchFilters) -> bool:
    symbol = filters.normalized_symbol
    mode = filters.normalized_mode
    regime = filters.normalized_regime
    if symbol is not None and row["symbol"] != symbol:
        return False
    if mode is not None and row.get("mode") != mode:
        return False
    row_regime = _display(row.get("regime") or row.get("run_market_regime"))
    if regime is not None and row_regime != regime:
        return False
    return True


def _include_lifecycle_row(row: Mapping[str, Any], filters: ResearchFilters) -> bool:
    symbol = filters.normalized_symbol
    mode = filters.normalized_mode
    regime = filters.normalized_regime
    if symbol is not None and row.get("symbol") != symbol:
        return False
    if mode is not None and row.get("mode") != mode:
        return False
    if regime is not None and _display(row.get("regime_state")) != regime:
        return False
    return True


def _normalize_symbol_row(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    raw = _json_loads(data.get("raw_result_json"))
    quality = raw.get("setup_quality") if isinstance(raw.get("setup_quality"), Mapping) else {}
    near_miss = raw.get("near_miss_intelligence") if isinstance(raw.get("near_miss_intelligence"), Mapping) else {}
    failed_gate = _first_non_na(data.get("failed_gate"), near_miss.get("primary_failed_gate"))
    next_trigger = _first_non_na(data.get("next_trigger_needed"), near_miss.get("activation_hint"))
    data.update(
        {
            "symbol": _display(data.get("symbol")).upper(),
            "display_bucket": _display(data.get("display_bucket")),
            "regime_state": _first_non_na(data.get("regime_state"), data.get("run_market_regime")),
            "regime_confidence": _display(data.get("regime_confidence")),
            "regime_compatibility_score": _display(data.get("regime_compatibility_score")),
            "regime_compatibility_label": _display(data.get("regime_compatibility_label")),
            "regime_penalty": _display(data.get("regime_penalty")),
            "failed_gate": failed_gate,
            "rejection_reason": _display(data.get("rejection_reason")),
            "next_trigger_needed": next_trigger,
            "setup_quality_score": _display(data.get("setup_quality_score")),
            "quality_grade": _display(quality.get("quality_grade")),
            "quality_state": _display(quality.get("quality_state")),
            "readiness_label": _display(raw.get("readiness_label")),
            "raw": raw,
            "modes": _row_modes(raw),
            "has_data_issue": _has_data_issue(raw, data),
        }
    )
    return data


def _normalize_setup_row(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    raw = _json_loads(data.get("symbol_raw_result_json"))
    quality = raw.get("setup_quality") if isinstance(raw.get("setup_quality"), Mapping) else {}
    data.update(
        {
            "symbol": _display(data.get("symbol")).upper(),
            "mode": _display(data.get("mode")).lower(),
            "regime": _display(data.get("run_market_regime")),
            "setup_quality_score": _display(data.get("setup_quality_score")),
            "quality_grade": _first_non_na(data.get("quality_grade"), quality.get("quality_grade")),
            "raw": _json_loads(data.get("raw_candidate_json")),
        }
    )
    return data


def _normalize_replay_row(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    data.update(
        {
            "symbol": _display(data.get("symbol")).upper(),
            "mode": _display(data.get("mode")).lower(),
            "regime": _first_non_na(data.get("regime"), data.get("run_market_regime")),
            "raw": _json_loads(data.get("raw_result_json")),
        }
    )
    return data


def _normalize_lifecycle_record(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    mode = _display(data.get("mode"))
    direction = _display(data.get("direction"))
    data.update(
        {
            "symbol": _display(data.get("symbol")).upper(),
            "mode": mode.lower() if mode != NA else NA,
            "direction": direction.lower() if direction != NA else NA,
            "current_state": _display(data.get("current_state")),
            "previous_state": _display(data.get("previous_state")),
            "failed_gate": _display(data.get("failed_gate")),
            "regime_state": _display(data.get("regime_state")),
            "invalidation_reason": _display(data.get("invalidation_reason")),
            "readiness_score": int(data.get("readiness_score") or 0),
            "quality_score": int(data.get("quality_score") or 0),
        }
    )
    return data


def _normalize_lifecycle_event(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    mode = _display(data.get("mode"))
    direction = _display(data.get("direction"))
    data.update(
        {
            "symbol": _display(data.get("symbol")).upper(),
            "from_state": _display(data.get("from_state")),
            "to_state": _display(data.get("to_state")),
            "reason": _display(data.get("reason")),
            "failed_gate": _display(data.get("failed_gate")),
            "notes": _display(data.get("notes")),
            "readiness_score": int(data.get("readiness_score") or 0),
            "quality_score": int(data.get("quality_score") or 0),
            "mode": mode.lower() if mode != NA else NA,
            "direction": direction.lower() if direction != NA else NA,
            "regime_state": _display(data.get("regime_state")),
        }
    )
    return data


def _row_modes(raw: Mapping[str, Any]) -> tuple[str, ...]:
    modes: list[str] = []
    for key in ("valid_strategy_modes", "rejected_strategy_modes"):
        for mode in _sequence_values(raw.get(key)):
            normalized = mode.lower()
            if normalized not in modes:
                modes.append(normalized)
    diagnostics = raw.get("strategy_diagnostics")
    if isinstance(diagnostics, Mapping):
        for mode in diagnostics:
            normalized = str(mode).lower()
            if normalized not in modes:
                modes.append(normalized)
    return tuple(modes)


def _has_data_issue(raw: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    if row.get("display_bucket") == "data_issue":
        return True
    quality = raw.get("setup_quality") if isinstance(raw.get("setup_quality"), Mapping) else {}
    values = (
        *_sequence_values(raw.get("missing_data")),
        *_sequence_values(raw.get("unverified_data")),
        *_sequence_values(raw.get("strategy_missing_data")),
        *_sequence_values(raw.get("strategy_unverified_data")),
        *_sequence_values(raw.get("derivatives_missing_data")),
        *_sequence_values(raw.get("derivatives_unverified_data")),
        *_sequence_values(quality.get("missing_data")),
        *_sequence_values(quality.get("unverified_data")),
    )
    return bool(values)


def _gate_interpretation(gate: str) -> str:
    if gate == NA:
        return "No reliable interpretation available."
    if gate in {"missing_confirmed_sweep"}:
        return "Confirmed liquidity sweeps are not appearing often enough."
    if gate in {"missing_confirmation_structure_shift", "missing_confirmation_candles"}:
        return "Sweep context appears, but confirmation is not completing."
    if gate in {"no_ob_or_fvg_zone", "challenge_limit_entry_missing", "missing_displacement_impulse", "missing_stop"}:
        return "Structure progresses, but no clean execution zone is available."
    if gate in {"pullback_too_deep", "pullback_beyond_786", "entry_window_expired"}:
        return "Pullback quality is weak or stale before activation."
    if gate in {"missing_rr", "missing_target", "rr_below_minimum", "challenge_rr_below_3", "rr_too_low"}:
        return "Reward-to-risk is not compensating for the setup."
    if gate in {"trust_meter_below_minimum", "challenge_trust_below_85", "quality_filter"}:
        return "Final confluence is below the required quality threshold."
    if gate in {"derivatives_conflict", "funding_oi_guard"}:
        return "Public derivatives context is conflicting with the idea."
    if gate == "regime_compatibility":
        return "The setup passed local structure but the broader regime was too weak for the selected mode."
    if gate in {"no_execution_candles", "not_enough_candles", "atr_unavailable", "scanner_error", "current_price"}:
        return "Required public data is incomplete; treat conclusions as unreliable."
    return "Review this gate in raw scanner diagnostics before drawing a conclusion."


def _replay_sample_warnings(replays: Sequence[Mapping[str, Any]]) -> list[str]:
    if replays and len(replays) < MIN_RELIABLE_SAMPLE_SIZE:
        return [SAMPLE_SIZE_WARNING]
    return []


def _bucket_count(rows: Sequence[Mapping[str, Any]], bucket: str) -> int:
    return sum(1 for row in rows if row.get("display_bucket") == bucket)


def _group_by(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_display(row.get(key))].append(row)
    return grouped


def _numeric_values(values: Iterable[Any]) -> tuple[Decimal, ...]:
    output = []
    for value in values:
        number = _decimal_or_none(value)
        if number is not None:
            output.append(number)
    return tuple(output)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _rate(count: int, total: int) -> float | str:
    if total <= 0:
        return NA
    return _number(Decimal(count) / Decimal(total) * Decimal("100"))


def _number(value: Decimal | None) -> float | str:
    if value is None:
        return NA
    quantized = value.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        return int(quantized)
    return float(quantized)


def _sort_number(value: Any) -> float:
    number = _decimal_or_none(value)
    return float(number) if number is not None else float("-inf")


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        decimal = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return decimal if decimal.is_finite() else None


def _tp_number(value: Any) -> int:
    text = _display(value).upper()
    if text.startswith("TP"):
        try:
            return int(text.removeprefix("TP"))
        except ValueError:
            return 0
    return 0


def _most_common_text(values: Iterable[Any]) -> str:
    normalized = [_display(value) for value in values if _display(value) != NA]
    if not normalized:
        return NA
    return Counter(normalized).most_common(1)[0][0]


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _sequence_values(values: Any) -> tuple[str, ...]:
    if values is None or isinstance(values, (str, bytes)):
        return ()
    if not isinstance(values, Sequence):
        return ()
    output = []
    for value in values:
        text = _display(value)
        if text != NA:
            output.append(text)
    return tuple(output)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _json_loads(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)
