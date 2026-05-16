from __future__ import annotations

import json

import pytest

from app.watchlists.presets import (
    WatchlistPresetError,
    available_preset_names,
    dedupe_symbols,
    load_custom_preset,
    preset_symbols,
    validate_symbols,
)


def test_required_presets_are_available() -> None:
    assert available_preset_names() == (
        "majors",
        "large_caps",
        "l1_l2",
        "sol_ecosystem",
        "ai",
        "rwa",
        "defi",
        "meme_high_liquidity",
    )


def test_preset_resolution_is_case_insensitive() -> None:
    assert preset_symbols("Majors") == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")


def test_dedupe_symbols_preserves_order() -> None:
    assert dedupe_symbols(("BTCUSDT", "ETHUSDT", "BTCUSDT", "SOLUSDT", "ETHUSDT")) == (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    )


def test_validate_symbols_uppercases_and_rejects_empty_values() -> None:
    assert validate_symbols(("btcusdt", " ethusdt "), context="test symbols") == ("BTCUSDT", "ETHUSDT")

    with pytest.raises(WatchlistPresetError, match="non-empty string"):
        validate_symbols(("BTCUSDT", ""), context="test symbols")


def test_custom_preset_json_loading(tmp_path) -> None:
    preset_path = tmp_path / "custom_watchlist.json"
    preset_path.write_text(
        json.dumps({"name": "custom_name", "symbols": ["btcusdt", " ETHUSDT "]}),
        encoding="utf-8",
    )

    preset = load_custom_preset(preset_path)

    assert preset.name == "custom_name"
    assert preset.symbols == ("BTCUSDT", "ETHUSDT")
    assert preset.path == preset_path


def test_custom_preset_json_rejects_invalid_symbols(tmp_path) -> None:
    preset_path = tmp_path / "custom_watchlist.json"
    preset_path.write_text(json.dumps({"name": "bad", "symbols": ["BTCUSDT", 123]}), encoding="utf-8")

    with pytest.raises(WatchlistPresetError, match="non-empty string"):
        load_custom_preset(preset_path)


def test_unknown_preset_error_is_clear() -> None:
    with pytest.raises(WatchlistPresetError, match="Unknown watchlist preset 'unknown'"):
        preset_symbols("unknown")
