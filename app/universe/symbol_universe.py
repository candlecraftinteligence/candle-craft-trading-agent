from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.exchange_clients.binance_futures import BinanceFuturesClient

MANUAL_UNIVERSE_MODE = "manual"
BINANCE_USDT_PERP_TOP_VOLUME_MODE = "binance_usdt_perp_top_volume"
BINANCE_USDT_PERP_TOP_TRADABLE_MODE = "binance_usdt_perp_top_tradable"
BINANCE_USDM_24H_TICKER_SOURCE = "binance_usdm_24h_ticker_public"
MANUAL_SOURCE = "manual"
UNIVERSE_MODES = (
    MANUAL_UNIVERSE_MODE,
    BINANCE_USDT_PERP_TOP_VOLUME_MODE,
    BINANCE_USDT_PERP_TOP_TRADABLE_MODE,
)

STABLECOIN_BASES = frozenset(("USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USDS"))
LEVERAGED_SUFFIXES = frozenset(("DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "4L", "4S", "5L", "5S"))
NON_LEVERAGED_UP_BASES = frozenset(("JUP",))


@dataclass(frozen=True)
class UniverseSymbol:
    symbol: str
    quote_volume: Decimal


@dataclass(frozen=True)
class SymbolUniverse:
    mode: str
    requested_size: int
    resolved_symbols: tuple[str, ...]
    excluded_symbols: tuple[str, ...]
    source: str
    generated_at: str
    min_quote_volume: Decimal = Decimal("0")
    quote_volume_by_symbol: Mapping[str, Decimal] = field(default_factory=dict)

    def top_by_quote_volume(self, limit: int = 5) -> tuple[UniverseSymbol, ...]:
        symbols = [
            UniverseSymbol(symbol=symbol, quote_volume=quote_volume)
            for symbol in self.resolved_symbols
            if (quote_volume := self.quote_volume_by_symbol.get(symbol)) is not None
        ]
        return tuple(sorted(symbols, key=lambda item: (-item.quote_volume, item.symbol))[:limit])

    def with_resolved_symbols(
        self,
        symbols: Sequence[str],
        *,
        extra_excluded_symbols: Sequence[str] = (),
    ) -> SymbolUniverse:
        return replace(
            self,
            resolved_symbols=tuple(symbols),
            excluded_symbols=_dedupe((*self.excluded_symbols, *extra_excluded_symbols)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "requested_size": self.requested_size,
            "resolved_symbols": list(self.resolved_symbols),
            "excluded_symbols": list(self.excluded_symbols),
            "source": self.source,
            "generated_at": self.generated_at,
            "min_quote_volume": _decimal_json(self.min_quote_volume),
        }


TickerFetcher = Callable[[], Awaitable[Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]]]


async def resolve_symbol_universe(
    mode: str,
    *,
    universe_size: int,
    min_quote_volume: Decimal | str | int = Decimal("0"),
    ticker_fetcher: TickerFetcher | None = None,
    generated_at: str | None = None,
) -> SymbolUniverse:
    if mode == MANUAL_UNIVERSE_MODE:
        raise ValueError("manual universe resolution requires explicit symbols")
    if mode not in UNIVERSE_MODES:
        raise ValueError(f"unsupported universe mode: {mode}")

    if ticker_fetcher is not None:
        raw_tickers = ticker_fetcher()
        tickers = await raw_tickers if inspect.isawaitable(raw_tickers) else raw_tickers
    else:
        client = BinanceFuturesClient()
        try:
            tickers = await client.get_24h_tickers()
        finally:
            await client.aclose()

    return build_symbol_universe_from_tickers(
        mode,
        tickers,
        universe_size=universe_size,
        min_quote_volume=min_quote_volume,
        generated_at=generated_at,
    )


def manual_symbol_universe(
    symbols: Sequence[str],
    *,
    requested_size: int | None = None,
    excluded_symbols: Sequence[str] = (),
    min_quote_volume: Decimal | str | int = Decimal("0"),
    generated_at: str | None = None,
) -> SymbolUniverse:
    resolved = tuple(symbols)
    return SymbolUniverse(
        mode=MANUAL_UNIVERSE_MODE,
        requested_size=requested_size if requested_size is not None else len(resolved),
        resolved_symbols=resolved,
        excluded_symbols=_dedupe(excluded_symbols),
        source=MANUAL_SOURCE,
        generated_at=generated_at or _utc_now_iso(),
        min_quote_volume=_non_negative_decimal(min_quote_volume, "min_quote_volume"),
    )


def build_symbol_universe_from_tickers(
    mode: str,
    tickers: Sequence[Mapping[str, Any]],
    *,
    universe_size: int,
    min_quote_volume: Decimal | str | int = Decimal("0"),
    generated_at: str | None = None,
) -> SymbolUniverse:
    if mode not in (BINANCE_USDT_PERP_TOP_VOLUME_MODE, BINANCE_USDT_PERP_TOP_TRADABLE_MODE):
        raise ValueError(f"unsupported Binance ticker universe mode: {mode}")
    if universe_size < 1:
        raise ValueError("universe_size must be at least 1")

    minimum_volume = _non_negative_decimal(min_quote_volume, "min_quote_volume")
    tradable_only = mode == BINANCE_USDT_PERP_TOP_TRADABLE_MODE
    candidates: list[UniverseSymbol] = []
    excluded: list[str] = []

    for raw_ticker in tickers:
        symbol = _ticker_symbol(raw_ticker)
        if not symbol:
            excluded.append("N/A")
            continue

        if not symbol.endswith("USDT"):
            excluded.append(symbol)
            continue

        quote_volume = _ticker_quote_volume(raw_ticker)
        if quote_volume is None:
            excluded.append(symbol)
            continue
        if quote_volume < minimum_volume:
            excluded.append(symbol)
            continue

        base_symbol = symbol[:-4]
        if tradable_only and (base_symbol in STABLECOIN_BASES or _is_leveraged_token_base(base_symbol)):
            excluded.append(symbol)
            continue

        candidates.append(UniverseSymbol(symbol=symbol, quote_volume=quote_volume))

    sorted_candidates = sorted(candidates, key=lambda item: (-item.quote_volume, item.symbol))
    resolved_symbols: list[str] = []
    quote_volume_by_symbol: dict[str, Decimal] = {}
    for candidate in sorted_candidates:
        if candidate.symbol in quote_volume_by_symbol:
            continue
        resolved_symbols.append(candidate.symbol)
        quote_volume_by_symbol[candidate.symbol] = candidate.quote_volume
        if len(resolved_symbols) >= universe_size:
            break

    return SymbolUniverse(
        mode=mode,
        requested_size=universe_size,
        resolved_symbols=tuple(resolved_symbols),
        excluded_symbols=_dedupe(excluded),
        source=BINANCE_USDM_24H_TICKER_SOURCE,
        generated_at=generated_at or _utc_now_iso(),
        min_quote_volume=minimum_volume,
        quote_volume_by_symbol=quote_volume_by_symbol,
    )


def _ticker_symbol(raw_ticker: Mapping[str, Any]) -> str:
    if not isinstance(raw_ticker, Mapping):
        return ""
    value = raw_ticker.get("symbol")
    if value is None:
        return ""
    return str(value).strip().upper()


def _ticker_quote_volume(raw_ticker: Mapping[str, Any]) -> Decimal | None:
    if not isinstance(raw_ticker, Mapping):
        return None
    for name in ("quoteVolume", "quote_volume", "quote_volume_24h"):
        value = raw_ticker.get(name)
        if value in (None, ""):
            continue
        try:
            quote_volume = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not quote_volume.is_finite() or quote_volume < 0:
            return None
        return quote_volume
    return None


def _is_leveraged_token_base(base_symbol: str) -> bool:
    if any(base_symbol.endswith(suffix) for suffix in LEVERAGED_SUFFIXES):
        return True
    if base_symbol.endswith("UP") and base_symbol not in NON_LEVERAGED_UP_BASES:
        return len(base_symbol[:-2]) >= 2
    return False


def _non_negative_decimal(value: Decimal | str | int, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{path} must be a decimal number") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError(f"{path} must be zero or greater")
    return decimal


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        normalized = str(value).strip().upper()
        if normalized and normalized not in output:
            output.append(normalized)
    return tuple(output)


def _decimal_json(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
