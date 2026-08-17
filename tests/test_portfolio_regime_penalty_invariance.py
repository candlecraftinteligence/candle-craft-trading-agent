from types import SimpleNamespace
from decimal import Decimal
from app.analytics.portfolio_selection import build_portfolio_selection_from_scan, selected_symbols
from tests.test_regime_intelligence import _scanner_contract_symbol, _contract_lifecycle_record


def _make_scan_with_symbol(symbol):
    # Simple object with config and results tuple expected by build_portfolio_selection_from_scan
    config = SimpleNamespace(risk_per_trade_pct=Decimal("1"))
    return SimpleNamespace(config=config, results=(symbol,))


def test_historical_regime_penalty_not_influence_selection():
    base = _scanner_contract_symbol("swing")
    # Two symbols identical except historical regime_penalty
    s0 = base.model_copy(update={"regime_penalty": 0})
    s50 = base.model_copy(update={"regime_penalty": 50})

    sel0 = build_portfolio_selection_from_scan(_make_scan_with_symbol(s0))
    sel50 = build_portfolio_selection_from_scan(_make_scan_with_symbol(s50))

    assert selected_symbols(sel0) == selected_symbols(sel50)
    # Confirm selection decisions identical
    assert sel0.selected_candidates == sel50.selected_candidates
