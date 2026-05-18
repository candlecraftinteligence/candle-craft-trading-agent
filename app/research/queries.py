from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.normalized_symbol or NA,
            "mode": self.normalized_mode or NA,
            "regime": self.normalized_regime or NA,
            "limit": self.normalized_limit,
        }


@dataclass(frozen=True)
class ResearchData:
    runs: tuple[dict[str, Any], ...]
    symbols: tuple[dict[str, Any], ...]
    setups: tuple[dict[str, Any], ...]
    replays: tuple[dict[str, Any], ...]


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
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to read research database: {database_path}") from exc

    filtered_runs = _filter_runs(runs, filters)
    filtered_symbols = tuple(row for row in symbol_rows if _include_symbol_row(row, filters))
    filtered_setups = tuple(row for row in setup_rows if _include_mode_row(row, filters))
    filtered_replays = tuple(row for row in replay_rows if _include_mode_row(row, filters))
    run_ids = {row["run_id"] for row in (*filtered_symbols, *filtered_setups, *filtered_replays)}
    if run_ids:
        filtered_runs = tuple(row for row in filtered_runs if row["run_id"] in run_ids)
    return ResearchData(
        runs=filtered_runs,
        symbols=filtered_symbols,
        setups=filtered_setups,
        replays=filtered_replays,
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
