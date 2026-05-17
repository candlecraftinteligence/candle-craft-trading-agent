from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from app.analytics.portfolio_selection import (
    BetaGroup,
    PortfolioCandidate,
    PortfolioDecision,
    PortfolioRiskLimits,
    PortfolioSelectionInput,
    select_portfolio,
)
from app.analytics.setup_quality import validate_setup_quality
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult
from scripts import run_scan


def _candidate(
    symbol: str,
    *,
    beta_group: BetaGroup,
    quality_state: str = "HIGH_QUALITY_TRADE",
    quality_score: int = 90,
    edge_score: Decimal = Decimal("80"),
    rr: Decimal = Decimal("3.0"),
    risk_pct: Decimal = Decimal("1"),
) -> PortfolioCandidate:
    return PortfolioCandidate(
        symbol=symbol,
        mode="swing",
        direction="long",
        quality_state=quality_state,
        quality_score=quality_score,
        tradeability_score=quality_score,
        edge_score=edge_score,
        rr=rr,
        risk_pct=risk_pct,
        beta_group=beta_group,
        derivatives_score=85,
        execution_risk_score=20,
        derivatives_clean=True,
    )


def test_highest_quality_valid_setup_selected() -> None:
    result = select_portfolio(
        PortfolioSelectionInput(
            candidates=(
                _candidate("ETHUSDT", beta_group=BetaGroup.ETH_BETA, quality_score=82),
                _candidate("SOLUSDT", beta_group=BetaGroup.SOL_BETA, quality_score=94),
            ),
            risk_limits=PortfolioRiskLimits(max_selected_setups=1),
        )
    )

    assert [candidate.symbol for candidate in result.selected_candidates] == ["SOLUSDT"]
    assert result.selected_candidates[0].decision == PortfolioDecision.SELECTED


def test_near_miss_not_selected() -> None:
    result = select_portfolio(
        PortfolioSelectionInput(
            candidates=(
                _candidate(
                    "NEARUSDT",
                    beta_group=BetaGroup.L1_L2,
                    quality_state="WATCHLIST_NEAR_MISS",
                    quality_score=70,
                ),
            )
        )
    )

    assert result.selected_candidates == ()
    assert result.rejected_candidates[0].decision == PortfolioDecision.WATCHLIST_ONLY


def test_correlated_duplicate_rejected() -> None:
    result = select_portfolio(
        PortfolioSelectionInput(
            candidates=(
                _candidate("SOLUSDT", beta_group=BetaGroup.SOL_BETA, quality_score=92),
                _candidate("JUPUSDT", beta_group=BetaGroup.SOL_BETA, quality_score=80),
            )
        )
    )

    assert [candidate.symbol for candidate in result.selected_candidates] == ["SOLUSDT"]
    assert result.rejected_candidates[0].symbol == "JUPUSDT"
    assert result.rejected_candidates[0].decision == PortfolioDecision.REJECTED_LOWER_QUALITY_DUPLICATE
    assert result.rejected_due_to_correlation == 1


def test_risk_limit_rejection() -> None:
    result = select_portfolio(
        PortfolioSelectionInput(
            candidates=(
                _candidate("BTCUSDT", beta_group=BetaGroup.BTC_MAJOR, quality_score=92, risk_pct=Decimal("2")),
                _candidate("SOLUSDT", beta_group=BetaGroup.SOL_BETA, quality_score=91, risk_pct=Decimal("2")),
            ),
            risk_limits=PortfolioRiskLimits(
                max_portfolio_risk_pct=Decimal("3"),
                max_beta_group_risk_pct=Decimal("3"),
            ),
        )
    )

    assert [candidate.symbol for candidate in result.selected_candidates] == ["BTCUSDT"]
    assert result.rejected_candidates[0].decision == PortfolioDecision.REJECTED_PORTFOLIO_RISK_LIMIT
    assert result.rejected_due_to_risk_limit == 1


def test_allow_correlated_setups_override() -> None:
    result = select_portfolio(
        PortfolioSelectionInput(
            candidates=(
                _candidate("SOLUSDT", beta_group=BetaGroup.SOL_BETA, quality_score=92, risk_pct=Decimal("0.5")),
                _candidate("JUPUSDT", beta_group=BetaGroup.SOL_BETA, quality_score=80, risk_pct=Decimal("0.5")),
            ),
            risk_limits=PortfolioRiskLimits(allow_correlated_setups=True),
        )
    )

    assert [candidate.symbol for candidate in result.selected_candidates] == ["SOLUSDT", "JUPUSDT"]
    assert result.rejected_due_to_correlation == 0


def test_max_selected_setups_respected() -> None:
    result = select_portfolio(
        PortfolioSelectionInput(
            candidates=(
                _candidate("BTCUSDT", beta_group=BetaGroup.BTC_MAJOR, quality_score=94, risk_pct=Decimal("0.5")),
                _candidate("ETHUSDT", beta_group=BetaGroup.ETH_BETA, quality_score=93, risk_pct=Decimal("0.5")),
                _candidate("SOLUSDT", beta_group=BetaGroup.SOL_BETA, quality_score=92, risk_pct=Decimal("0.5")),
            ),
            risk_limits=PortfolioRiskLimits(max_selected_setups=2),
        )
    )

    assert [candidate.symbol for candidate in result.selected_candidates] == ["BTCUSDT", "ETHUSDT"]
    assert result.rejected_candidates[0].symbol == "SOLUSDT"
    assert result.rejected_candidates[0].decision == PortfolioDecision.REJECTED_PORTFOLIO_RISK_LIMIT


def test_portfolio_cli_flags_are_accepted() -> None:
    args = run_scan.parse_args(
        [
            "--symbols",
            "BTCUSDT",
            "--portfolio-select",
            "--max-selected-setups",
            "2",
            "--max-portfolio-risk-pct",
            "2.5",
            "--max-beta-group-risk-pct",
            "1",
            "--allow-correlated-setups",
        ]
    )

    assert args.portfolio_select is True
    assert args.max_selected_setups == 2
    assert args.max_portfolio_risk_pct == Decimal("2.5")
    assert args.max_beta_group_risk_pct == Decimal("1")
    assert args.allow_correlated_setups is True


class PortfolioScannerRunner:
    async def run(self, config, after_symbol=None, progress=None, resume_metadata=None):
        results = (
            _valid_symbol_result("BTCUSDT", quality_score=90, rr=Decimal("3.5")),
            _valid_symbol_result("ETHUSDT", quality_score=82, rr=Decimal("3.0")),
        )
        return ScannerRunResult(
            config=config,
            results=results,
            scanned_symbols=len(results),
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
            resume_metadata=dict(resume_metadata or {}),
        )


def _valid_symbol_result(symbol: str, *, quality_score: int, rr: Decimal) -> ScannerSymbolResult:
    mode = "swing"
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        technical_score=88,
        derivatives_score=85,
        strategy_diagnostics={
            mode: {
                "is_valid": True,
                "mode": mode,
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": rr,
                "trust_percentage": quality_score,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
                "derivatives_supports_trade": True,
                "derivatives_conflict_reason": "N/A",
                "crowding_risk": "low",
            }
        },
        valid_strategy_modes=(mode,),
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": True,
                "mode": mode,
                "bias": "long",
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "ob_or_fvg_valid": True,
                "fib_valid": True,
                "rr_to_tp2": rr,
                "best_rr": rr,
                "htf_2d_trend": "bullish",
                "mtf_12h_trend": "bullish",
                "trust_percentage": quality_score,
                "poc_available": True,
                "value_area_available": True,
                "derivatives_supports_trade": True,
                "derivatives_score": 85,
                "funding_status": "normal",
                "crowding_risk": "low",
                "risk_approved": True,
                "data_quality_score": 90,
            }
        ),
    )


def test_portfolio_select_json_contains_selection(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "scan_output.json"
    monkeypatch.setattr(run_scan, "ScannerRunner", PortfolioScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "ETHUSDT",
                "--portfolio-select",
                "--output-json",
                str(output_path),
            ]
        )
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert "portfolio_selection" in payload
    assert payload["portfolio_selection"]["selected_count"] == 2
    assert [candidate["symbol"] for candidate in payload["selected_candidates"]] == ["BTCUSDT", "ETHUSDT"]
    assert payload["exposure_summary"][0]["beta_group"] == "BTC_MAJOR"
    assert "portfolio_warnings" in payload


def test_scanner_behavior_unchanged_without_portfolio_select(tmp_path, monkeypatch, capsys) -> None:
    output_path = tmp_path / "scan_output.json"
    monkeypatch.setattr(run_scan, "ScannerRunner", PortfolioScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "ETHUSDT", "--output-json", str(output_path)]))

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    captured = capsys.readouterr()
    assert "portfolio_selection" not in payload
    assert "selected_candidates" not in payload
    assert "Portfolio Selection" not in captured.out
