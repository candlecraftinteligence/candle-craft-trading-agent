from __future__ import annotations

import pytest

from app.microstructure.service import MicrostructureFlowService
from app.pipeline.scanner_runner import ScannerRunConfig
from scripts import run_scan


def _config(**overrides: object) -> ScannerRunConfig:
    values: dict[str, object] = {
        "symbols": ["ETHUSDT"],
        "exchange": "binance",
        "account_equity": "10000",
        "risk_per_trade_pct": "1",
    }
    values.update(overrides)
    return ScannerRunConfig.model_validate(values)


def test_watch_service_factory_is_disabled_by_default_and_binance_only() -> None:
    assert run_scan._microstructure_flow_service_for_config(_config()) is None
    assert (
        run_scan._microstructure_flow_service_for_config(
            _config(exchange="bybit", microstructure_flow_enabled=True)
        )
        is None
    )


def test_watch_service_factory_applies_runtime_bounds_without_connecting() -> None:
    service = run_scan._microstructure_flow_service_for_config(
        _config(
            microstructure_flow_enabled=True,
            microstructure_flow_stale_sec=7.5,
            microstructure_flow_max_symbols=37,
        )
    )

    assert isinstance(service, MicrostructureFlowService)
    assert service.running is False
    assert service.stale_after_seconds == 7.5
    assert service.max_symbols == 37


def test_each_scanner_runner_receives_the_same_watch_owned_service(monkeypatch) -> None:
    captured: list[object] = []

    class CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs["microstructure_flow_service"])

    shared_service = object()
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingRunner)

    first = run_scan._scanner_runner_with_context(
        None,
        microstructure_flow_service=shared_service,
    )
    second = run_scan._scanner_runner_with_context(
        None,
        microstructure_flow_service=shared_service,
    )

    assert isinstance(first, CapturingRunner)
    assert isinstance(second, CapturingRunner)
    assert captured == [shared_service, shared_service]

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("microstructure_flow_stale_sec", 0),
        ("microstructure_flow_max_symbols", 0),
        ("microstructure_flow_max_symbols", 1025),
    ],
)
def test_scanner_config_rejects_invalid_flow_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})