from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.microstructure.liquidation_service import LiquidationFlowService
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


def test_liquidation_watch_service_is_disabled_by_default_and_binance_only() -> None:
    assert run_scan._liquidation_flow_service_for_config(_config()) is None
    assert (
        run_scan._liquidation_flow_service_for_config(
            _config(exchange="bybit", liquidation_flow_enabled=True)
        )
        is None
    )


def test_liquidation_watch_factory_applies_bounds_without_connecting() -> None:
    service = run_scan._liquidation_flow_service_for_config(
        _config(
            liquidation_flow_enabled=True,
            liquidation_flow_stale_sec=17.5,
            liquidation_flow_max_symbols=37,
        )
    )

    assert isinstance(service, LiquidationFlowService)
    assert service.running is False
    assert service.stale_after_seconds == 17.5
    assert service.max_symbols == 37


def test_each_scanner_runner_receives_same_watch_owned_liquidation_service(
    monkeypatch,
) -> None:
    captured: list[object] = []

    class CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs["liquidation_flow_service"])

    shared_service = object()
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingRunner)

    first = run_scan._scanner_runner_with_context(
        None,
        liquidation_flow_service=shared_service,
    )
    second = run_scan._scanner_runner_with_context(
        None,
        liquidation_flow_service=shared_service,
    )

    assert isinstance(first, CapturingRunner)
    assert isinstance(second, CapturingRunner)
    assert captured == [shared_service, shared_service]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("liquidation_flow_stale_sec", 0),
        ("liquidation_flow_max_symbols", 0),
        ("liquidation_flow_max_symbols", 1025),
    ],
)
def test_scanner_config_rejects_invalid_liquidation_bounds(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_runtime_settings_default_to_disabled_and_validate_bounds() -> None:
    settings = Settings(_env_file=None)

    assert settings.liquidation_flow_enabled is False
    assert settings.liquidation_flow_stale_sec == 30.0
    assert settings.liquidation_flow_max_symbols == 100
    with pytest.raises(ValueError, match="LIQUIDATION_FLOW_STALE_SEC"):
        Settings(_env_file=None, liquidation_flow_stale_sec=0)
    with pytest.raises(ValueError, match="LIQUIDATION_FLOW_MAX_SYMBOLS"):
        Settings(_env_file=None, liquidation_flow_max_symbols=1025)


def test_env_example_keeps_liquidation_research_disabled() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    values: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value

    assert values["LIQUIDATION_FLOW_ENABLED"] == "false"
    assert values["LIQUIDATION_FLOW_STALE_SEC"] == "30.0"
    assert values["LIQUIDATION_FLOW_MAX_SYMBOLS"] == "100"
