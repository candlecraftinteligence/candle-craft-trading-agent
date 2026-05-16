from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any


WATCHLIST_PRESETS: dict[str, tuple[str, ...]] = {
    "majors": ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"),
    "large_caps": (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "AVAXUSDT",
        "LINKUSDT",
        "TONUSDT",
        "TRXUSDT",
        "DOTUSDT",
        "MATICUSDT",
    ),
    "l1_l2": (
        "SOLUSDT",
        "ETHUSDT",
        "BNBUSDT",
        "AVAXUSDT",
        "OPUSDT",
        "ARBUSDT",
        "SUIUSDT",
        "APTUSDT",
        "SEIUSDT",
        "NEARUSDT",
    ),
    "sol_ecosystem": ("SOLUSDT", "JUPUSDT", "JTOUSDT", "PYTHUSDT", "WIFUSDT", "BONKUSDT"),
    "ai": ("TAOUSDT", "FETUSDT", "RNDRUSDT", "WLDUSDT", "AIUSDT", "ARKMUSDT"),
    "rwa": ("ONDOUSDT", "PENDLEUSDT", "LINKUSDT", "MKRUSDT"),
    "defi": ("AAVEUSDT", "UNIUSDT", "DYDXUSDT", "GMXUSDT", "LDOUSDT", "CRVUSDT"),
    "meme_high_liquidity": ("DOGEUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT", "SHIBUSDT"),
}


class WatchlistPresetError(ValueError):
    """Raised when a watchlist preset or custom preset file is invalid."""


@dataclass(frozen=True)
class CustomPreset:
    name: str
    symbols: tuple[str, ...]
    path: Path


def available_preset_names() -> tuple[str, ...]:
    return tuple(WATCHLIST_PRESETS)


def presets_with_counts() -> tuple[tuple[str, int], ...]:
    return tuple((name, len(symbols)) for name, symbols in WATCHLIST_PRESETS.items())


def preset_symbols(name: str) -> tuple[str, ...]:
    normalized_name = _normalize_preset_name(name)
    symbols = WATCHLIST_PRESETS.get(normalized_name)
    if symbols is None:
        raise WatchlistPresetError(
            f"Unknown watchlist preset '{name}'. Available presets: {', '.join(available_preset_names())}."
        )
    return symbols


def validate_symbols(values: Iterable[Any], *, context: str = "symbols") -> tuple[str, ...]:
    symbols: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise WatchlistPresetError(f"{context} item {index + 1} must be a non-empty string.")
        normalized = value.strip().upper()
        if not normalized:
            raise WatchlistPresetError(f"{context} item {index + 1} must be a non-empty string.")
        symbols.append(normalized)
    return tuple(symbols)


def dedupe_symbols(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return tuple(output)


def load_custom_preset(path: str | Path) -> CustomPreset:
    preset_path = Path(path)
    try:
        raw_text = preset_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WatchlistPresetError(f"Preset file not found: {preset_path}") from exc
    except OSError as exc:
        raise WatchlistPresetError(f"Could not read preset file '{preset_path}': {exc}") from exc

    try:
        payload = json.loads(raw_text)
    except JSONDecodeError as exc:
        raise WatchlistPresetError(f"Preset file must be valid JSON: {exc.msg}.") from exc

    if not isinstance(payload, dict):
        raise WatchlistPresetError("Preset file must contain a JSON object with 'name' and 'symbols'.")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WatchlistPresetError("Preset file field 'name' must be a non-empty string.")

    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        raise WatchlistPresetError("Preset file field 'symbols' must be a list of non-empty strings.")

    validated_symbols = validate_symbols(symbols, context="preset file symbols")
    if not validated_symbols:
        raise WatchlistPresetError("Preset file field 'symbols' must include at least one symbol.")

    return CustomPreset(name=name.strip(), symbols=validated_symbols, path=preset_path)


def _normalize_preset_name(name: str) -> str:
    normalized = name.strip().lower()
    if not normalized:
        raise WatchlistPresetError("Preset name must be a non-empty string.")
    return normalized


__all__ = [
    "CustomPreset",
    "WATCHLIST_PRESETS",
    "WatchlistPresetError",
    "available_preset_names",
    "dedupe_symbols",
    "load_custom_preset",
    "preset_symbols",
    "presets_with_counts",
    "validate_symbols",
]
