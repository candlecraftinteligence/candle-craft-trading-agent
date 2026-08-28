from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.microstructure.order_book_service import OrderBookLiquidityService
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


def test_order_book_service_is_disabled_by_default_and_binance_only() -> None:
    assert run_scan._order_book_liquidity_service_for_config(_config()) is None
    assert (
        run_scan._order_book_liquidity_service_for_config(
            _config(exchange="bybit", order_book_liquidity_enabled=True)
        )
        is None
    )


def test_order_book_factory_applies_research_load_bounds_without_connecting() -> None:
    service = run_scan._order_book_liquidity_service_for_config(
        _config(
            order_book_liquidity_enabled=True,
            order_book_liquidity_stale_sec=7.5,
            order_book_liquidity_max_symbols=37,
            order_book_liquidity_update_speed="250ms",
            order_book_liquidity_snapshot_limit=100,
            order_book_liquidity_bootstrap_concurrency=3,
            order_book_liquidity_event_buffer_size=128,
        )
    )
    assert isinstance(service, OrderBookLiquidityService)
    assert service.running is False
    assert service.stale_after_seconds == 7.5
    assert service.max_symbols == 37
    assert service.update_speed == "250ms"
    assert service.snapshot_limit == 100
    assert service.snapshot_request_weight == 5
    assert service.bootstrap_concurrency == 3
    assert service.event_buffer_size == 128


def test_scanner_runner_receives_same_watch_owned_order_book_service(monkeypatch) -> None:
    captured: list[object] = []

    class CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs["order_book_liquidity_service"])

    shared_service = object()
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingRunner)
    first = run_scan._scanner_runner_with_context(
        None,
        order_book_liquidity_service=shared_service,
    )
    second = run_scan._scanner_runner_with_context(
        None,
        order_book_liquidity_service=shared_service,
    )
    assert isinstance(first, CapturingRunner)
    assert isinstance(second, CapturingRunner)
    assert captured == [shared_service, shared_service]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_book_liquidity_stale_sec", 0),
        ("order_book_liquidity_max_symbols", 0),
        ("order_book_liquidity_max_symbols", 101),
        ("order_book_liquidity_bootstrap_concurrency", 0),
        ("order_book_liquidity_bootstrap_concurrency", 9),
        ("order_book_liquidity_event_buffer_size", 0),
        ("order_book_liquidity_event_buffer_size", 4097),
    ],
)
def test_scanner_config_rejects_invalid_order_book_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_runtime_settings_are_disabled_by_default_and_validate_bounds() -> None:
    settings = Settings(_env_file=None)
    assert settings.order_book_liquidity_enabled is False
    assert settings.order_book_liquidity_stale_sec == 5.0
    assert settings.order_book_liquidity_max_symbols == 100
    assert settings.order_book_liquidity_update_speed == "500ms"
    assert settings.order_book_liquidity_snapshot_limit == 500
    assert settings.order_book_liquidity_bootstrap_concurrency == 2
    assert settings.order_book_liquidity_event_buffer_size == 256
    with pytest.raises(ValueError, match="ORDER_BOOK_LIQUIDITY_STALE_SEC"):
        Settings(_env_file=None, order_book_liquidity_stale_sec=0)
    with pytest.raises(ValueError, match="ORDER_BOOK_LIQUIDITY_MAX_SYMBOLS"):
        Settings(_env_file=None, order_book_liquidity_max_symbols=101)


def test_env_example_keeps_order_book_research_disabled() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    values: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    assert values["ORDER_BOOK_LIQUIDITY_ENABLED"] == "false"
    assert values["ORDER_BOOK_LIQUIDITY_UPDATE_SPEED"] == "500ms"
    assert values["ORDER_BOOK_LIQUIDITY_SNAPSHOT_LIMIT"] == "500"
    assert values["ORDER_BOOK_LIQUIDITY_BOOTSTRAP_CONCURRENCY"] == "2"
    assert values["ORDER_BOOK_LIQUIDITY_EVENT_BUFFER_SIZE"] == "256"
