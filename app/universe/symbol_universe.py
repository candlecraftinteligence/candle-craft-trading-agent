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
BINANCE_USDM_EXCHANGE_INFO_SOURCE = "binance_usdm_exchange_info_public"
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
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE: "Global cryptocurrency market-cap Top N available as Binance USDT perpetuals",
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
class _RankedProviderAsset:
    raw_asset: Mapping[str, Any]
    base_symbol: str
    identity: str
    rank: int


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
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return universe_label(self.mode)

    @property
    def strict_membership(self) -> bool:
        return self.mode == BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE

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
        diagnostic_updates: Mapping[str, Any] | None = None,
    ) -> SymbolUniverse:
        resolved = tuple(symbols)
        diagnostics = dict(self.diagnostics)
        if diagnostics or diagnostic_updates:
            diagnostics["final_universe_count"] = len(resolved)
        if diagnostic_updates:
            diagnostics.update(diagnostic_updates)
        return replace(
            self,
            resolved_symbols=resolved,
            excluded_symbols=_dedupe((*self.excluded_symbols, *extra_excluded_symbols)),
            diagnostics=diagnostics,
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
            "diagnostics": dict(self.diagnostics),
        }


TickerFetcher = Callable[[], Awaitable[Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]]]
MarketCapFetcher = Callable[[], Awaitable[Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]]]
ExchangeInfoFetcher = Callable[[], Awaitable[Mapping[str, Any]] | Mapping[str, Any]]


class UniverseResolutionError(RuntimeError):
    """Raised when a public universe source cannot be resolved cleanly."""


async def resolve_symbol_universe(
    mode: str,
    *,
    universe_size: int,
    min_quote_volume: Decimal | str | int = Decimal("0"),
    ticker_fetcher: TickerFetcher | None = None,
    market_cap_fetcher: MarketCapFetcher | None = None,
    exchange_info_fetcher: ExchangeInfoFetcher | None = None,
    generated_at: str | None = None,
) -> SymbolUniverse:
    if mode == MANUAL_UNIVERSE_MODE:
        raise ValueError("manual universe resolution requires explicit symbols")
    if mode not in UNIVERSE_MODES:
        raise ValueError(f"unsupported universe mode: {mode}")

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

        client: BinanceFuturesClient | None = None
        try:
            if ticker_fetcher is not None:
                raw_tickers = ticker_fetcher()
                tickers = await raw_tickers if inspect.isawaitable(raw_tickers) else raw_tickers
            else:
                client = BinanceFuturesClient()
                tickers = await client.get_24h_tickers()

            if exchange_info_fetcher is not None:
                raw_exchange_info = exchange_info_fetcher()
                exchange_info = (
                    await raw_exchange_info if inspect.isawaitable(raw_exchange_info) else raw_exchange_info
                )
            else:
                client = client or BinanceFuturesClient()
                exchange_info = await client.get_exchange_info()
        except Exception as exc:
            raise UniverseResolutionError(
                f"universe_error: Binance USDT perpetual source failed: {exc}"
            ) from exc
        finally:
            if client is not None:
                await client.aclose()

        return build_symbol_universe_from_market_caps(
            tickers,
            market_caps,
            exchange_info=exchange_info,
            universe_size=universe_size,
            min_quote_volume=min_quote_volume,
            generated_at=generated_at,
        )

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
    exchange_info: Mapping[str, Any] | None = None,
    min_quote_volume: Decimal | str | int = Decimal("0"),
    generated_at: str | None = None,
) -> SymbolUniverse:
    if universe_size < 1:
        raise ValueError("universe_size must be at least 1")
    if not isinstance(market_caps, Sequence) or isinstance(market_caps, (str, bytes)):
        raise UniverseResolutionError("universe_error: CoinPaprika market-cap source returned malformed response")

    provider_assets = tuple(item for item in market_caps if isinstance(item, Mapping))
    if not provider_assets:
        raise UniverseResolutionError("universe_error: CoinPaprika market-cap source returned empty response")

    minimum_volume = _non_negative_decimal(min_quote_volume, "min_quote_volume")
    binance_tickers, excluded = _binance_usdt_symbols_from_tickers(
        tickers,
        min_quote_volume=minimum_volume,
        tradable_only=True,
    )
    quote_volume_by_symbol = {item.symbol: item.quote_volume for item in binance_tickers}
    binance_perpetuals, contract_diagnostics = _binance_crypto_usdt_perpetual_symbols(
        exchange_info,
        fallback_symbols=tuple(quote_volume_by_symbol),
    )
    available_binance_symbols = binance_perpetuals.intersection(quote_volume_by_symbol)

    identities_by_base: dict[str, set[str]] = {}
    ranked_by_identity: dict[str, _RankedProviderAsset] = {}
    missing_rank_count = 0
    invalid_rank_count = 0
    invalid_symbol_count = 0
    for index, raw_asset in enumerate(provider_assets):
        base_symbol = _market_cap_base_symbol(raw_asset)
        if not base_symbol:
            invalid_symbol_count += 1
        identity = _market_cap_asset_identity(raw_asset, index=index)
        if base_symbol:
            identities_by_base.setdefault(base_symbol, set()).add(identity)

        rank, rank_status = _market_cap_rank_with_status(raw_asset)
        if rank_status == "missing":
            missing_rank_count += 1
            continue
        if rank_status == "invalid" or rank is None:
            invalid_rank_count += 1
            continue
        if not base_symbol:
            continue

        candidate = _RankedProviderAsset(
            raw_asset=raw_asset,
            base_symbol=base_symbol,
            identity=identity,
            rank=rank,
        )
        existing = ranked_by_identity.get(identity)
        if existing is None or (candidate.rank, candidate.base_symbol) < (existing.rank, existing.base_symbol):
            ranked_by_identity[identity] = candidate

    if not ranked_by_identity:
        raise UniverseResolutionError(
            "universe_error: CoinPaprika market-cap source contained no valid ranked cryptocurrency assets"
        )
    provider_max_rank = max(asset.rank for asset in ranked_by_identity.values())
    if provider_max_rank < universe_size:
        raise UniverseResolutionError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            f"(highest valid rank {provider_max_rank} < requested {universe_size})"
        )

    ambiguous_bases = {
        base_symbol for base_symbol, identities in identities_by_base.items() if len(identities) > 1
    }
    ranked_candidates: dict[str, UniverseMarketCapSymbol] = {}
    rank_gt_n_excluded_count = 0
    rank_within_boundary_count = 0
    no_binance_perp_count = 0
    ambiguous_within_boundary: set[str] = set()
    for provider_asset in ranked_by_identity.values():
        if provider_asset.rank > universe_size:
            rank_gt_n_excluded_count += 1
            continue
        rank_within_boundary_count += 1
        if provider_asset.base_symbol in ambiguous_bases:
            ambiguous_within_boundary.add(provider_asset.base_symbol)
            continue

        symbol = f"{provider_asset.base_symbol}USDT"
        if symbol not in available_binance_symbols:
            no_binance_perp_count += 1
            continue
        candidate = UniverseMarketCapSymbol(
            symbol=symbol,
            rank=provider_asset.rank,
            market_cap=_market_cap_usd(provider_asset.raw_asset),
        )
        existing = ranked_candidates.get(symbol)
        if existing is None or candidate.rank < existing.rank:
            ranked_candidates[symbol] = candidate

    selected = tuple(sorted(ranked_candidates.values(), key=lambda item: (item.rank, item.symbol)))
    diagnostics = {
        "provider": COINPAPRIKA_MARKET_CAP_SOURCE,
        "exchange_availability_source": BINANCE_USDM_EXCHANGE_INFO_SOURCE,
        "requested_universe_size": universe_size,
        "requested_max_symbols": None,
        "provider_asset_count": len(provider_assets),
        "valid_rank_count": len(ranked_by_identity),
        "provider_max_rank": provider_max_rank,
        "rank_within_boundary_count": rank_within_boundary_count,
        "binance_perp_match_count": len(selected),
        "final_universe_count": len(selected),
        "rank_gt_n_excluded_count": rank_gt_n_excluded_count,
        "missing_rank_count": missing_rank_count,
        "invalid_rank_count": invalid_rank_count,
        "invalid_symbol_count": invalid_symbol_count,
        "no_binance_perp_count": no_binance_perp_count,
        "ambiguous_symbol_count": len(ambiguous_within_boundary),
        "ambiguous_symbols": sorted(ambiguous_within_boundary),
        "provider_failure": False,
        "cache_used": False,
        "cache_age": None,
        **contract_diagnostics,
    }

    return SymbolUniverse(
        mode=BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
        requested_size=universe_size,
        resolved_symbols=tuple(item.symbol for item in selected),
        excluded_symbols=_dedupe(excluded),
        source=COINPAPRIKA_MARKET_CAP_SOURCE,
        generated_at=generated_at or _utc_now_iso(),
        min_quote_volume=minimum_volume,
        quote_volume_by_symbol={item.symbol: quote_volume_by_symbol[item.symbol] for item in selected},
        market_cap_rank_by_symbol={item.symbol: item.rank for item in selected},
        market_cap_by_symbol={
            item.symbol: item.market_cap for item in selected if item.market_cap is not None
        },
        diagnostics=diagnostics,
    )


async def fetch_coinpaprika_market_cap_rankings(
    *,
    timeout: float = 10.0,
    http_client: httpx.AsyncClient | None = None,
) -> Sequence[Mapping[str, Any]]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=COINPAPRIKA_BASE_URL, timeout=timeout)
    try:
        try:
            response = await client.get("/tickers", params={"quotes": "USD"})
        except httpx.TimeoutException as exc:
            raise UniverseResolutionError("universe_error: CoinPaprika market-cap source timed out") from exc
        except httpx.TransportError as exc:
            raise UniverseResolutionError(
                f"universe_error: CoinPaprika market-cap source unavailable: {exc}"
            ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code == 429:
        raise UniverseResolutionError("universe_error: CoinPaprika market-cap source rate limited")
    if not 200 <= response.status_code < 300:
        raise UniverseResolutionError(
            f"universe_error: CoinPaprika market-cap source returned HTTP {response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise UniverseResolutionError(
            "universe_error: CoinPaprika market-cap source returned malformed JSON"
        ) from exc
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise UniverseResolutionError("universe_error: CoinPaprika market-cap source returned malformed response")
    assets = tuple(item for item in payload if isinstance(item, Mapping))
    if not assets:
        detail = "empty response" if not payload else "malformed response"
        raise UniverseResolutionError(f"universe_error: CoinPaprika market-cap source returned {detail}")
    return assets


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


def _binance_crypto_usdt_perpetual_symbols(
    exchange_info: Mapping[str, Any] | None,
    *,
    fallback_symbols: Sequence[str],
) -> tuple[set[str], dict[str, Any]]:
    if exchange_info is None:
        symbols = set(fallback_symbols)
        return symbols, {
            "contract_metadata_used": False,
            "binance_perpetual_contract_count": len(symbols),
            "non_crypto_contract_excluded_count": 0,
            "non_perpetual_contract_excluded_count": 0,
            "non_trading_contract_excluded_count": 0,
        }
    if not isinstance(exchange_info, Mapping):
        raise UniverseResolutionError("universe_error: Binance exchange-info source returned malformed response")
    raw_symbols = exchange_info.get("symbols")
    if not isinstance(raw_symbols, Sequence) or isinstance(raw_symbols, (str, bytes)):
        raise UniverseResolutionError("universe_error: Binance exchange-info source returned malformed response")

    symbols: set[str] = set()
    non_crypto_count = 0
    non_perpetual_count = 0
    non_trading_count = 0
    malformed_count = 0
    policy_excluded_count = 0
    for raw_contract in raw_symbols:
        if not isinstance(raw_contract, Mapping):
            malformed_count += 1
            continue
        contract_type = _upper_field(raw_contract, "contractType")
        status = _upper_field(raw_contract, "status")
        quote_asset = _upper_field(raw_contract, "quoteAsset")
        underlying_type = _upper_field(raw_contract, "underlyingType")
        base_asset = _upper_field(raw_contract, "baseAsset")
        symbol = _upper_field(raw_contract, "symbol")
        if contract_type != "PERPETUAL":
            non_perpetual_count += 1
            continue
        if status != "TRADING":
            non_trading_count += 1
            continue
        if quote_asset != "USDT":
            continue
        if underlying_type != "COIN":
            non_crypto_count += 1
            continue
        if not base_asset or not symbol or symbol != f"{base_asset}{quote_asset}":
            malformed_count += 1
            continue
        if base_asset in STABLECOIN_BASES or _is_leveraged_token_base(base_asset):
            policy_excluded_count += 1
            continue
        symbols.add(symbol)

    return symbols, {
        "contract_metadata_used": True,
        "binance_perpetual_contract_count": len(symbols),
        "non_crypto_contract_excluded_count": non_crypto_count,
        "non_perpetual_contract_excluded_count": non_perpetual_count,
        "non_trading_contract_excluded_count": non_trading_count,
        "malformed_contract_excluded_count": malformed_count,
        "existing_crypto_policy_excluded_count": policy_excluded_count,
    }


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


def _market_cap_asset_identity(raw_asset: Mapping[str, Any], *, index: int) -> str:
    value = raw_asset.get("id") if isinstance(raw_asset, Mapping) else None
    if value not in (None, ""):
        return f"id:{str(value).strip().lower()}"
    return f"row:{index}"


def _market_cap_rank(raw_asset: Mapping[str, Any]) -> int | None:
    rank, _status = _market_cap_rank_with_status(raw_asset)
    return rank


def _market_cap_rank_with_status(raw_asset: Mapping[str, Any]) -> tuple[int | None, str]:
    if not isinstance(raw_asset, Mapping):
        return None, "missing"
    value = raw_asset.get("rank")
    if value in (None, ""):
        return None, "missing"
    try:
        rank = int(str(value))
    except (TypeError, ValueError):
        return None, "invalid"
    if rank < 1:
        return None, "invalid"
    return rank, "valid"


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


def _upper_field(value: Mapping[str, Any], name: str) -> str:
    raw = value.get(name)
    return "" if raw is None else str(raw).strip().upper()


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
