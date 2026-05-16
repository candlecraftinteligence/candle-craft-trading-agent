from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.data.exchange_clients.binance_futures import BinanceFuturesClient

MANUAL_UNIVERSE_MODE = "manual"
BINANCE_USDT_PERP_TOP_VOLUME_MODE = "binance_usdt_perp_top_volume"
BINANCE_USDT_PERP_TOP_TRADABLE_MODE = "binance_usdt_perp_top_tradable"
BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE = "binance_usdt_perp_top_market_cap"
BINANCE_USDM_24H_TICKER_SOURCE = "binance_usdm_24h_ticker_public"
COINPAPRIKA_MARKET_CAP_SOURCE = "coinpaprika_market_cap_public"
COINPAPRIKA_BASE_URL = "https://api.coinpaprika.com/v1"
MANUAL_SOURCE = "manual"
UNIVERSE_MODES = (
    MANUAL_UNIVERSE_MODE,
    BINANCE_USDT_PERP_TOP_VOLUME_MODE,
    BINANCE_USDT_PERP_TOP_TRADABLE_MODE,
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
)
UNIVERSE_LABELS = {
    MANUAL_UNIVERSE_MODE: "Manual symbols",
    BINANCE_USDT_PERP_TOP_VOLUME_MODE: "Top Binance USDT perpetuals by quote volume",
    BINANCE_USDT_PERP_TOP_TRADABLE_MODE: "Top Binance USDT perpetuals by quote volume/tradability",
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE: "Top Binance USDT perpetuals by public market-cap rank",
}

STABLECOIN_BASES = frozenset(("USDC", "USDT", "DAI", "FDUSD", "TUSD", "USDE", "USDS"))
LEVERAGED_SUFFIXES = frozenset(("DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "4L", "4S", "5L", "5S"))
NON_LEVERAGED_UP_BASES = frozenset(("JUP",))


@dataclass(frozen=True)
class UniverseSymbol:
    symbol: str
    quote_volume: Decimal


@dataclass(frozen=True)
class UniverseMarketCapSymbol:
    symbol: str
    rank: int
    market_cap: Decimal | None = None


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
    market_cap_rank_by_symbol: Mapping[str, int] = field(default_factory=dict)
    market_cap_by_symbol: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return universe_label(self.mode)

    def top_by_quote_volume(self, limit: int = 5) -> tuple[UniverseSymbol, ...]:
        symbols = [
            UniverseSymbol(symbol=symbol, quote_volume=quote_volume)
            for symbol in self.resolved_symbols
            if (quote_volume := self.quote_volume_by_symbol.get(symbol)) is not None
        ]
        return tuple(sorted(symbols, key=lambda item: (-item.quote_volume, item.symbol))[:limit])

    def top_by_market_cap_rank(self, limit: int = 5) -> tuple[UniverseMarketCapSymbol, ...]:
        symbols = [
            UniverseMarketCapSymbol(
                symbol=symbol,
                rank=rank,
                market_cap=self.market_cap_by_symbol.get(symbol),
            )
            for symbol in self.resolved_symbols
            if (rank := self.market_cap_rank_by_symbol.get(symbol)) is not None
        ]
        return tuple(sorted(symbols, key=lambda item: (item.rank, item.symbol))[:limit])

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
            "label": self.label,
            "requested_size": self.requested_size,
            "resolved_symbols": list(self.resolved_symbols),
            "excluded_symbols": list(self.excluded_symbols),
            "source": self.source,
            "generated_at": self.generated_at,
            "min_quote_volume": _decimal_json(self.min_quote_volume),
            "market_cap_rank_by_symbol": dict(self.market_cap_rank_by_symbol),
            "market_cap_usd_by_symbol": {
                symbol: _decimal_json(value) for symbol, value in self.market_cap_by_symbol.items()
            },
        }


TickerFetcher = Callable[[], Awaitable[Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]]]
MarketCapFetcher = Callable[[], Awaitable[Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]]]


class UniverseResolutionError(RuntimeError):
    """Raised when a public universe source cannot be resolved cleanly."""


async def resolve_symbol_universe(
    mode: str,
    *,
    universe_size: int,
    min_quote_volume: Decimal | str | int = Decimal("0"),
    ticker_fetcher: TickerFetcher | None = None,
    market_cap_fetcher: MarketCapFetcher | None = None,
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

    if mode == BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE:
        try:
            if market_cap_fetcher is not None:
                raw_market_caps = market_cap_fetcher()
                market_caps = await raw_market_caps if inspect.isawaitable(raw_market_caps) else raw_market_caps
            else:
                market_caps = await fetch_coinpaprika_market_cap_rankings()
        except UniverseResolutionError:
            raise
        except Exception as exc:
            raise UniverseResolutionError(f"universe_error: market-cap source failed: {exc}") from exc

        return build_symbol_universe_from_market_caps(
            tickers,
            market_caps,
            universe_size=universe_size,
            min_quote_volume=min_quote_volume,
            generated_at=generated_at,
        )

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
    candidates, excluded = _binance_usdt_symbols_from_tickers(
        tickers,
        min_quote_volume=minimum_volume,
        tradable_only=tradable_only,
    )

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


def build_symbol_universe_from_market_caps(
    tickers: Sequence[Mapping[str, Any]],
    market_caps: Sequence[Mapping[str, Any]],
    *,
    universe_size: int,
    min_quote_volume: Decimal | str | int = Decimal("0"),
    generated_at: str | None = None,
) -> SymbolUniverse:
    if universe_size < 1:
        raise ValueError("universe_size must be at least 1")

    minimum_volume = _non_negative_decimal(min_quote_volume, "min_quote_volume")
    binance_symbols, excluded = _binance_usdt_symbols_from_tickers(
        tickers,
        min_quote_volume=minimum_volume,
        tradable_only=True,
    )
    quote_volume_by_symbol = {item.symbol: item.quote_volume for item in binance_symbols}
    ranked_candidates: dict[str, UniverseMarketCapSymbol] = {}

    for raw_asset in market_caps:
        base_symbol = _market_cap_base_symbol(raw_asset)
        rank = _market_cap_rank(raw_asset)
        if not base_symbol or rank is None:
            continue
        symbol = f"{base_symbol}USDT"
        if symbol not in quote_volume_by_symbol:
            continue
        market_cap = _market_cap_usd(raw_asset)
        candidate = UniverseMarketCapSymbol(symbol=symbol, rank=rank, market_cap=market_cap)
        existing = ranked_candidates.get(symbol)
        if existing is None or candidate.rank < existing.rank:
            ranked_candidates[symbol] = candidate

    sorted_candidates = sorted(ranked_candidates.values(), key=lambda item: (item.rank, item.symbol))
    selected = tuple(sorted_candidates[:universe_size])

    return SymbolUniverse(
        mode=BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
        requested_size=universe_size,
        resolved_symbols=tuple(item.symbol for item in selected),
        excluded_symbols=_dedupe(excluded),
        source=COINPAPRIKA_MARKET_CAP_SOURCE,
        generated_at=generated_at or _utc_now_iso(),
        min_quote_volume=minimum_volume,
        quote_volume_by_symbol={
            item.symbol: quote_volume_by_symbol[item.symbol]
            for item in selected
        },
        market_cap_rank_by_symbol={item.symbol: item.rank for item in selected},
        market_cap_by_symbol={
            item.symbol: item.market_cap for item in selected if item.market_cap is not None
        },
    )


async def fetch_coinpaprika_market_cap_rankings(
    *,
    timeout: float = 10.0,
) -> Sequence[Mapping[str, Any]]:
    async with httpx.AsyncClient(base_url=COINPAPRIKA_BASE_URL, timeout=timeout) as client:
        try:
            response = await client.get("/tickers", params={"quotes": "USD"})
        except httpx.TimeoutException as exc:
            raise UniverseResolutionError("universe_error: CoinPaprika market-cap source timed out") from exc
        except httpx.TransportError as exc:
            raise UniverseResolutionError(f"universe_error: CoinPaprika market-cap source unavailable: {exc}") from exc

    if response.status_code == 429:
        raise UniverseResolutionError("universe_error: CoinPaprika market-cap source rate limited")
    if not 200 <= response.status_code < 300:
        raise UniverseResolutionError(
            f"universe_error: CoinPaprika market-cap source returned HTTP {response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise UniverseResolutionError("universe_error: CoinPaprika market-cap source returned malformed JSON") from exc
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise UniverseResolutionError("universe_error: CoinPaprika market-cap source returned malformed response")
    return tuple(item for item in payload if isinstance(item, Mapping))


def _binance_usdt_symbols_from_tickers(
    tickers: Sequence[Mapping[str, Any]],
    *,
    min_quote_volume: Decimal,
    tradable_only: bool,
) -> tuple[list[UniverseSymbol], list[str]]:
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
        if quote_volume < min_quote_volume:
            excluded.append(symbol)
            continue

        base_symbol = symbol[:-4]
        if tradable_only and (base_symbol in STABLECOIN_BASES or _is_leveraged_token_base(base_symbol)):
            excluded.append(symbol)
            continue

        candidates.append(UniverseSymbol(symbol=symbol, quote_volume=quote_volume))

    return candidates, excluded


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


def _market_cap_base_symbol(raw_asset: Mapping[str, Any]) -> str:
    if not isinstance(raw_asset, Mapping):
        return ""
    value = raw_asset.get("symbol")
    if value is None:
        return ""
    return str(value).strip().upper()


def _market_cap_rank(raw_asset: Mapping[str, Any]) -> int | None:
    if not isinstance(raw_asset, Mapping):
        return None
    value = raw_asset.get("rank")
    try:
        rank = int(str(value))
    except (TypeError, ValueError):
        return None
    if rank < 1:
        return None
    return rank


def _market_cap_usd(raw_asset: Mapping[str, Any]) -> Decimal | None:
    if not isinstance(raw_asset, Mapping):
        return None
    quotes = raw_asset.get("quotes")
    if not isinstance(quotes, Mapping):
        return None
    usd_quote = quotes.get("USD")
    if not isinstance(usd_quote, Mapping):
        return None
    value = usd_quote.get("market_cap")
    if value in (None, ""):
        return None
    try:
        market_cap = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not market_cap.is_finite() or market_cap < 0:
        return None
    return market_cap


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


def universe_label(mode: str) -> str:
    return UNIVERSE_LABELS.get(mode, mode)
