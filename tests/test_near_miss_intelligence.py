from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

from app.analytics.near_miss_intelligence import build_near_miss_intelligence
from app.formatters.scanner_display import build_symbol_display, display_fields, format_symbol_card
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from scripts import run_scan


def _base_config() -> ScannerRunConfig:
    return ScannerRunConfig(
        symbols=("BTCUSDT",),
        exchange="binance",
        account_equity=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
    )


def _rr_near_miss_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="RRUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("1.8"),
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
                "rr_diagnostics": "failed: RR to TP2 1.8 is below 2.5.",
            }
        },
        rejected_strategy_modes=("swing",),
    )


def _valid_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="VALIDUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "swing": {
                "is_valid": True,
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("3.1"),
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
            }
        },
        valid_strategy_modes=("swing",),
    )


def _missing_sweep_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="REJECTUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "challenge": {
                "execution_sweep_status": "failed",
                "confirmation_structure_shift_status": "not_evaluated",
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        },
        rejected_strategy_modes=("challenge",),
    )


def test_rr_below_minimum_near_miss_plan() -> None:
    intelligence = build_near_miss_intelligence(
        failed_gate="rr_below_minimum",
        short_reason="RR to TP2 1.8 is below 2.5.",
        diagnostics={
            "execution_sweep_status": "passed",
            "confirmation_structure_shift_status": "passed",
            "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
        },
    )

    assert intelligence is not None
    assert intelligence.primary_failed_gate == "rr_below_minimum"
    assert intelligence.watchlist_status == "Watchlist only"
    assert intelligence.action_label == "Watchlist only"
    assert "Better pullback entry" in intelligence.next_required_conditions[0]
    assert "RR must improve to the required minimum" in intelligence.activation_hint


def test_pullback_too_deep_rejection_plan() -> None:
    intelligence = build_near_miss_intelligence(
        failed_gate="pullback_too_deep",
        short_reason="Pullback tagged beyond 0.786 before entry.",
        diagnostics={
            "execution_sweep_status": "passed",
            "confirmation_structure_shift_status": "passed",
        },
    )

    assert intelligence is not None
    assert intelligence.watchlist_status == "Rejected"
    assert intelligence.action_label == "Rejected"
    assert "completely new liquidity sweep" in " ".join(intelligence.next_required_conditions)
    assert "intent is weak" in intelligence.quality_note


def test_missing_5m_confirmation_plan_waits_for_confirmation() -> None:
    intelligence = build_near_miss_intelligence(
        failed_gate="missing_confirmation_structure_shift",
        short_reason="No 5m BOS/CHoCH close beyond the required LTF swing.",
        diagnostics={
            "execution_sweep_status": "passed",
            "confirmation_structure_shift_status": "failed",
            "confirmation_bos_choch_reason": "No 5m BOS/CHoCH close beyond the required LTF swing.",
        },
    )

    assert intelligence is not None
    assert intelligence.watchlist_status == "Wait for confirmation"
    assert intelligence.action_label == "Wait for confirmation"
    assert "5m BOS/CHoCH close" in intelligence.next_required_conditions[0]
    assert "before this can become interesting" in intelligence.activation_hint


def test_no_ob_or_fvg_plan_depends_on_core_structure() -> None:
    watchlist = build_near_miss_intelligence(
        failed_gate="no_ob_or_fvg_zone",
        short_reason="No valid OB or FVG was found inside the 5m displacement impulse.",
        diagnostics={
            "execution_sweep_status": "passed",
            "confirmation_structure_shift_status": "passed",
        },
    )
    rejected = build_near_miss_intelligence(
        failed_gate="no_ob_or_fvg_zone",
        short_reason="No valid OB or FVG was found inside the 5m displacement impulse.",
        diagnostics={
            "execution_sweep_status": "failed",
            "confirmation_structure_shift_status": "not_evaluated",
        },
    )

    assert watchlist is not None
    assert rejected is not None
    assert watchlist.action_label == "Watchlist only"
    assert rejected.action_label == "Rejected"
    assert "inside the displacement impulse" in watchlist.next_required_conditions[0]


def test_json_contains_near_miss_intelligence_object() -> None:
    result = ScannerRunResult(
        config=_base_config(),
        results=(_rr_near_miss_symbol(),),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )

    payload = run_scan._json_payload(result)
    intelligence = payload["results"][0]["near_miss_intelligence"]

    assert isinstance(intelligence, dict)
    assert intelligence["primary_failed_gate"] == "rr_below_minimum"
    assert intelligence["action_label"] == "Watchlist only"


def test_near_miss_display_output_includes_plan_labels() -> None:
    text = format_symbol_card(_rr_near_miss_symbol())

    assert "Needs next:" in text
    assert "Activation hint:" in text
    assert "Action: Watchlist only" in text


def test_valid_setup_behavior_unchanged() -> None:
    display = build_symbol_display(_valid_symbol())
    fields = display_fields(_valid_symbol(), display_rank=1)

    assert display.display_status == "valid_setup"
    assert display.display_bucket == "valid"
    assert display.action_label == "Trade idea created"
    assert display.near_miss_intelligence is None
    assert fields["near_miss_intelligence"] is None


def test_rejected_no_setup_behavior_unchanged() -> None:
    display = build_symbol_display(_missing_sweep_symbol())

    assert display.display_status == "no_setup"
    assert display.display_bucket == "no_setup"
    assert display.action_label == "Rejected"
    assert display.failed_gate == "missing_confirmed_sweep"
    assert display.near_miss_intelligence is None


def test_scan_output_json_remains_untracked_and_untouched() -> None:
    scan_output = Path("scan_output.json")
    before = scan_output.stat().st_mtime_ns if scan_output.exists() else None

    tracked = subprocess.run(
        ["git", "ls-files", "--", "scan_output.json"],
        check=True,
        capture_output=True,
        text=True,
    )
    _ = display_fields(_rr_near_miss_symbol(), display_rank=1)

    after = scan_output.stat().st_mtime_ns if scan_output.exists() else None
    assert tracked.stdout.strip() == ""
    assert after == before
