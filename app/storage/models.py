from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScanRunRecord:
    run_id: str
    timestamp: str
    exchange: str
    universe: str
    symbols_scanned: int
    symbols_json: str
    strategy: str
    timeframes_json: str
    market_regime: str
    regime_confidence: int
    regime_compatibility_json: str
    environment_notes_json: str
    runtime_stats_json: str
    command_preset: str
    command_used: str
    total_valid_setups: int
    near_misses: int
    rejected: int
    data_issues: int
    data_issues_json: str
    raw_payload_json: str


@dataclass(frozen=True)
class SymbolResultRecord:
    run_id: str
    symbol: str
    status: str
    display_bucket: str
    readiness_score: int
    setup_quality_score: str
    edge_score: str
    failed_gate: str
    rejection_reason: str
    next_trigger_needed: str
    action_label: str
    regime_state: str
    regime_confidence: str
    regime_compatibility_score: str
    regime_compatibility_label: str
    regime_penalty: int
    environment_notes_json: str
    derivatives_context_json: str
    volume_profile_context_json: str
    pullback_status: str
    portfolio_decision: str
    raw_result_json: str


@dataclass(frozen=True)
class SetupCandidateRecord:
    run_id: str
    symbol: str
    mode: str
    direction: str
    entry: str
    stop: str
    tp1: str
    tp2: str
    tp3: str
    rr: str
    invalidation: str
    quality_grade: str
    trust_meter: str
    risk_warning: str
    raw_candidate_json: str


@dataclass(frozen=True)
class ReplayResultRecord:
    run_id: str
    setup_fingerprint: str
    outcome: str
    filled: int
    tp_hit: str
    sl_hit: int
    final_r: str
    time_in_trade: str
    regime: str
    symbol: str
    mode: str
    raw_result_json: str


@dataclass(frozen=True)
class ScanHistorySummary:
    run_id: str
    timestamp: str
    universe: str
    symbols_scanned: int
    total_valid_setups: int
    near_misses: int
    rejected: int
    data_issues: int
    market_regime: str
    runtime_seconds: str
    regime_confidence: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "universe": self.universe,
            "symbols_scanned": self.symbols_scanned,
            "valid_setups": self.total_valid_setups,
            "near_misses": self.near_misses,
            "rejected": self.rejected,
            "data_issues": self.data_issues,
            "regime": self.market_regime,
            "regime_confidence": self.regime_confidence,
            "runtime": self.runtime_seconds,
        }
