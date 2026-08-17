from decimal import Decimal
from app.pipeline.scanner_runner import _apply_market_regime_to_results, ScannerPipelineStatus
from app.analytics.market_regime import (
    MarketRegimeResult,
    RegimeState,
    RegimeRiskLevel,
    RegimeConfidenceBand,
    RegimeAdjustment,
    RegimeCompatibility,
)
from tests.test_regime_intelligence import _valid_symbol, SCANNER_CONTRACT_MODES
from app.regime.models import confidence_band


def _make_compatibility(allowed: bool = True):
    return {
        mode: RegimeCompatibility(
            mode=mode,
            score=80 if allowed else 20,
            label="Strong" if allowed else "Hostile",
            allowed=allowed,
            regime_compatibility=80 if allowed else 20,
            volatility_suitability=80,
            trend_suitability=80,
            execution_quality_suitability=80,
            risk_multiplier=Decimal("1"),
            notes=("ok",),
        )
        for mode in SCANNER_CONTRACT_MODES
    }


def _make_adjustment(allow_modes: bool = True):
    return RegimeAdjustment(
        allow_challenge=allow_modes,
        allow_swings=allow_modes,
        allow_scalps=allow_modes,
        min_quality_score_adjustment=0,
        min_rr_adjustment=Decimal("0"),
        risk_multiplier=Decimal("1"),
        readiness_score_adjustment=0,
        edge_score_adjustment=0,
        trust_score_adjustment=0,
        portfolio_confidence_adjustment=0,
        regime_penalty=0,
        compatibility_scores={m: (80 if allow_modes else 20) for m in SCANNER_CONTRACT_MODES},
        explanation="test",
    )


def test_confidence_invariance_allows_and_blocks_independent_of_confidence():
    symbol = _valid_symbol()

    # Two regimes: one that allows modes, one that blocks modes.
    confidences = [10, 30, 50, 70, 90]

    # Allowed case: change only confidence_score, expect same allowed outcome
    allowed_results = []
    for c in confidences:
        regime = MarketRegimeResult(
            state=RegimeState.TREND_EXPANSION,
            risk_level=RegimeRiskLevel.LOW,
            confidence_score=c,
            confidence_band=confidence_band(c),
            compatibility_scores=_make_compatibility(True),
            adjustment=_make_adjustment(True),
            environment_notes=("env",),
            boosts=(),
            penalties=(),
        )
        adjusted = _apply_market_regime_to_results((symbol,), regime)[0]
        allowed_results.append(
            (
                adjusted.status,
                adjusted.valid_strategy_modes,
                adjusted.rejected_strategy_modes,
                adjusted.setup_quality.min_rr if hasattr(adjusted.setup_quality, 'min_rr') else None,
                adjusted.setup_quality.quality_score,
                adjusted.regime_diagnostics.get("regime_penalty"),
                adjusted.regime_diagnostics.get("regime_readiness_adjustment"),
                adjusted.regime_diagnostics.get("regime_edge_adjustment"),
                adjusted.regime_diagnostics.get("regime_trust_adjustment"),
                adjusted.rejection_reason,
            )
        )

    # All results should be identical across confidences
    assert len(set(allowed_results)) == 1

    # Blocked case: should remain blocked across confidences
    blocked_results = []
    for c in confidences:
        regime = MarketRegimeResult(
            state=RegimeState.CHOP,
            risk_level=RegimeRiskLevel.HIGH,
            confidence_score=c,
            confidence_band=confidence_band(c),
            compatibility_scores=_make_compatibility(False),
            adjustment=_make_adjustment(False),
            environment_notes=("env",),
            boosts=(),
            penalties=(),
        )
        adjusted = _apply_market_regime_to_results((symbol,), regime)[0]
        blocked_results.append(
            (
                adjusted.status,
                adjusted.valid_strategy_modes,
                adjusted.rejected_strategy_modes,
                adjusted.setup_quality.quality_score,
                adjusted.rejection_reason,
            )
        )

    assert len(set(blocked_results)) == 1
