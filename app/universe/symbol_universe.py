from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.data.exchange_clients.binance_futures import BinanceFuturesClient
from app.universe.canonical_qualification import (
    CANONICAL_QUALIFIED_COHORT_LIMIT,
    EXCHANGE_CONTRACT_PRICE_BASIS,
    VERIFIED_CONTRACT_MAPPINGS,
    CanonicalUniverseError,
    QualifiedAsset,
    acquired_timestamp,
    qualify_canonical_cohort,
)

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
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE: (
        "Qualified genuine-cryptocurrency market-cap cohort available as Binance USDT perpetuals"
    ),
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
class _ContractEvidence:
    symbol: str
    base_asset: str
    tick_size: str | None
    step_size: str | None
    stated_multiplier: int | None
    stated_multiplier_invalid: bool


@dataclass(frozen=True)
class _ExchangeIdentityMatch:
    exchange_symbol: str
    exchange_base_asset: str
    contract_multiplier: int
    price_quantity_basis: str
    collision_status: str
    tick_size: str | None
    step_size: str | None


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
        membership = diagnostics.get("qualified_membership")
        if isinstance(membership, list):
            allowed = set(resolved)
            diagnostics["qualified_membership"] = [
                item
                for item in membership
                if isinstance(item, Mapping) and item.get("exchange_symbol") in allowed
            ]
            diagnostics["selected_count"] = len(diagnostics["qualified_membership"])
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


def _attach_source_cleanup_error(primary: BaseException, cleanup: BaseException) -> None:
    """Keep source-client cleanup visible beside the primary failure.

    Control exceptions stay the same object and are not rewritten as provider
    outages. Payload and acquisition errors keep their text and cause.
    """

    setattr(primary, "source_cleanup_error", cleanup)
    if not isinstance(primary, UniverseResolutionError):
        return
    suffix = f"source_cleanup_error:{type(cleanup).__name__}:{cleanup}"
    message = str(primary)
    if suffix not in message:
        primary.args = (f"{message}; {suffix}",)


def _cleanup_failure(source_label: str, cleanup: BaseException) -> UniverseResolutionError:
    failure = UniverseResolutionError(f"universe_error: {source_label} cleanup failed: {cleanup}")
    failure.__cause__ = cleanup
    failure.__suppress_context__ = True
    setattr(failure, "source_cleanup_error", cleanup)
    return failure


async def _close_owned_source_client(client: Any) -> tuple[BaseException | None, BaseException | None]:
    """Close an owned source client without masking the caller's primary error.

    Returns ``(cleanup_error, control_exception)``. A requested stop from close
    is returned to the caller instead of being converted into a provider outage.
    """

    try:
        await client.aclose()
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        return None, exc
    except Exception as exc:
        return exc, None
    return None, None


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
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except UniverseResolutionError:
            raise
        except Exception as exc:
            raise UniverseResolutionError(f"universe_error: market-cap source failed: {exc}") from exc

        client: BinanceFuturesClient | None = None
        control_error: BaseException | None = None
        acquisition_error: BaseException | None = None
        tickers: Any = None
        exchange_info: Any = None
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
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            control_error = exc
        except Exception as exc:
            acquisition_error = exc

        cleanup_error: BaseException | None = None
        close_control: BaseException | None = None
        if client is not None:
            cleanup_error, close_control = await _close_owned_source_client(client)

        if control_error is not None:
            secondary = cleanup_error if cleanup_error is not None else close_control
            if secondary is not None:
                _attach_source_cleanup_error(control_error, secondary)
            raise control_error
        if close_control is not None:
            raise close_control
        if acquisition_error is not None:
            failure = UniverseResolutionError(
                f"universe_error: Binance USDT perpetual source failed: {acquisition_error}"
            )
            failure.__cause__ = acquisition_error
            failure.__suppress_context__ = True
            if cleanup_error is not None:
                _attach_source_cleanup_error(failure, cleanup_error)
            raise failure

        try:
            universe = build_symbol_universe_from_market_caps(
                tickers,
                market_caps,
                exchange_info=exchange_info,
                universe_size=universe_size,
                min_quote_volume=min_quote_volume,
                generated_at=generated_at,
            )
        except UniverseResolutionError as exc:
            if cleanup_error is not None:
                _attach_source_cleanup_error(exc, cleanup_error)
            raise
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            if cleanup_error is not None:
                _attach_source_cleanup_error(exc, cleanup_error)
            raise
        if cleanup_error is not None:
            raise _cleanup_failure("Binance USDT perpetual source", cleanup_error)
        return universe

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

    _require_market_cap_ticker_payload(tickers)
    minimum_volume = _non_negative_decimal(min_quote_volume, "min_quote_volume")
    binance_tickers, excluded = _binance_usdt_symbols_from_tickers(
        tickers,
        min_quote_volume=minimum_volume,
        tradable_only=True,
    )
    if not binance_tickers:
        raise UniverseResolutionError(
            "universe_error: Binance USDT perpetual ticker source contained no usable USDT tickers"
        )
    quote_volume_by_symbol = {item.symbol: item.quote_volume for item in binance_tickers}
    observed_volumes = _observed_usdt_quote_volumes(tickers)
    contracts, contract_diagnostics = _binance_crypto_usdt_perpetual_symbols(exchange_info)
    # A parser defect here is a programming error, not a provider outage.
    for raw_asset in provider_assets:
        _market_cap_usd(raw_asset)
    try:
        acquired_at = acquired_timestamp(generated_at)
        cohort = qualify_canonical_cohort(
            provider_assets,
            cohort_size=min(universe_size, CANONICAL_QUALIFIED_COHORT_LIMIT),
            acquired_at=acquired_at,
        )
    except CanonicalUniverseError as exc:
        raise UniverseResolutionError(str(exc)) from exc

    selected, identity_rejections = _select_qualified_perpetuals(
        cohort.cohort,
        contracts=contracts,
        eligible_symbol_counts=cohort.eligible_symbol_counts,
        observed_volumes=observed_volumes,
        admissible_volumes=quote_volume_by_symbol,
        minimum_volume=minimum_volume,
    )
    if not selected:
        raise UniverseResolutionError(
            "universe_error: strict market-cap discovery intersection is empty"
        )
    ambiguous_symbols = sorted(
        {
            str(item["provider_symbol"])
            for item in identity_rejections
            if item.get("reason") == "unresolved_ambiguity"
        }
    )
    rejection_counts: dict[str, int] = {}
    for item in identity_rejections:
        reason = str(item.get("reason") or "")
        if reason:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    no_binance_perp_count = rejection_counts.get("no_binance_perp", 0) + rejection_counts.get(
        "verified_contract_mismatch", 0
    )
    diagnostics = {
        "provider": COINPAPRIKA_MARKET_CAP_SOURCE,
        "exchange_availability_source": BINANCE_USDM_EXCHANGE_INFO_SOURCE,
        "qualification_policy": "canonical_genuine_crypto_top_n",
        "acquisition_timestamp": acquired_at.isoformat().replace("+00:00", "Z"),
        "canonical_qualified_cohort_limit": CANONICAL_QUALIFIED_COHORT_LIMIT,
        "qualified_cohort_limit": min(universe_size, CANONICAL_QUALIFIED_COHORT_LIMIT),
        "ranking_max_age_seconds": 3600,
        "provider_last_updated_min": cohort.provider_last_updated_min,
        "provider_last_updated_max": cohort.provider_last_updated_max,
        "requested_universe_size": universe_size,
        "requested_max_symbols": None,
        "provider_asset_count": cohort.provider_asset_count,
        "valid_rank_count": cohort.valid_rank_count,
        "provider_max_rank": cohort.provider_max_rank,
        "rank_within_boundary_count": len(cohort.cohort),
        "qualified_cohort_size": len(cohort.cohort),
        "binance_perp_match_count": len(selected),
        "selected_count": len(selected),
        "final_universe_count": len(selected),
        "rank_gt_n_excluded_count": cohort.qualified_rank_above_cohort_count,
        "missing_rank_count": cohort.missing_rank_count,
        "invalid_rank_count": cohort.invalid_rank_count,
        "invalid_symbol_count": cohort.invalid_symbol_count,
        "no_binance_perp_count": no_binance_perp_count,
        "below_min_quote_volume_count": rejection_counts.get("below_min_quote_volume", 0),
        "ambiguous_symbol_count": len(ambiguous_symbols),
        "ambiguous_symbols": ambiguous_symbols,
        "exclusion_counts": dict(cohort.exclusion_counts),
        "rejected_counts": {
            **{f"qualification_{category}": count for category, count in cohort.exclusion_counts.items()},
            **rejection_counts,
            "qualified_rank_above_cohort": cohort.qualified_rank_above_cohort_count,
        },
        "qualification_exclusions": [
            {
                "provider_id": item.provider_id,
                "provider_symbol": item.provider_symbol,
                "raw_rank": item.raw_rank,
                "category": item.category,
            }
            for item in cohort.exclusions[:200]
        ],
        "qualification_exclusions_truncated": len(cohort.exclusions) > 200,
        "identity_rejections": identity_rejections[:100],
        "identity_rejections_truncated": len(identity_rejections) > 100,
        "qualified_membership": [
            _membership_diagnostic(asset, match) for asset, match in selected
        ],
        "qualified_rank_by_symbol": {match.exchange_symbol: asset.qualified_rank for asset, match in selected},
        "provider_failure": False,
        "cache_used": False,
        "cache_age": None,
        **contract_diagnostics,
    }

    return SymbolUniverse(
        mode=BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
        requested_size=universe_size,
        resolved_symbols=tuple(match.exchange_symbol for _asset, match in selected),
        excluded_symbols=_dedupe(excluded),
        source=COINPAPRIKA_MARKET_CAP_SOURCE,
        generated_at=acquired_at.isoformat().replace("+00:00", "Z"),
        min_quote_volume=minimum_volume,
        quote_volume_by_symbol={
            match.exchange_symbol: quote_volume_by_symbol[match.exchange_symbol] for _asset, match in selected
        },
        market_cap_rank_by_symbol={match.exchange_symbol: asset.raw_rank for asset, match in selected},
        market_cap_by_symbol={match.exchange_symbol: asset.market_cap for asset, match in selected},
        diagnostics=diagnostics,
    )


async def fetch_coinpaprika_market_cap_rankings(
    *,
    timeout: float = 10.0,
    http_client: httpx.AsyncClient | None = None,
) -> Sequence[Mapping[str, Any]]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=COINPAPRIKA_BASE_URL, timeout=timeout)
    control_error: BaseException | None = None
    fetch_error: BaseException | None = None
    response: httpx.Response | None = None
    try:
        response = await client.get("/tickers", params={"quotes": "USD"})
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        control_error = exc
    except httpx.TimeoutException as exc:
        fetch_error = UniverseResolutionError("universe_error: CoinPaprika market-cap source timed out")
        fetch_error.__cause__ = exc
        fetch_error.__suppress_context__ = True
    except httpx.TransportError as exc:
        fetch_error = UniverseResolutionError(
            f"universe_error: CoinPaprika market-cap source unavailable: {exc}"
        )
        fetch_error.__cause__ = exc
        fetch_error.__suppress_context__ = True
    except Exception as exc:
        fetch_error = exc

    cleanup_error: BaseException | None = None
    close_control: BaseException | None = None
    if owns_client:
        cleanup_error, close_control = await _close_owned_source_client(client)
    if control_error is not None:
        secondary = cleanup_error if cleanup_error is not None else close_control
        if secondary is not None:
            _attach_source_cleanup_error(control_error, secondary)
        raise control_error
    if close_control is not None:
        raise close_control
    if fetch_error is not None:
        if cleanup_error is not None:
            _attach_source_cleanup_error(fetch_error, cleanup_error)
        raise fetch_error
    assert response is not None
    try:
        assets = _coinpaprika_assets_from_response(response)
    except UniverseResolutionError as exc:
        if cleanup_error is not None:
            _attach_source_cleanup_error(exc, cleanup_error)
        raise
    if cleanup_error is not None:
        raise _cleanup_failure("CoinPaprika market-cap source", cleanup_error)
    return assets


def _coinpaprika_assets_from_response(response: httpx.Response) -> tuple[Mapping[str, Any], ...]:
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


def _require_market_cap_ticker_payload(tickers: object) -> None:
    """Reject a missing or unusable top-level ticker payload before row iteration."""

    if not isinstance(tickers, Sequence) or isinstance(tickers, (str, bytes)):
        raise UniverseResolutionError(
            "universe_error: Binance USDT perpetual ticker source returned malformed response"
        )
    if len(tickers) == 0:
        raise UniverseResolutionError(
            "universe_error: Binance USDT perpetual ticker source returned empty response"
        )
    if not any(isinstance(item, Mapping) for item in tickers):
        raise UniverseResolutionError(
            "universe_error: Binance USDT perpetual ticker source returned malformed response"
        )


def _binance_crypto_usdt_perpetual_symbols(
    exchange_info: Mapping[str, Any] | None,
) -> tuple[dict[str, _ContractEvidence], dict[str, Any]]:
    # Ticker presence is not contract membership. Strict discovery requires the payload.
    if not isinstance(exchange_info, Mapping):
        raise UniverseResolutionError(
            "universe_error: Binance exchange-info source returned malformed response"
        )
    raw_symbols = exchange_info.get("symbols")
    if not isinstance(raw_symbols, Sequence) or isinstance(raw_symbols, (str, bytes)):
        raise UniverseResolutionError(
            "universe_error: Binance exchange-info source returned malformed response"
        )
    if len(raw_symbols) == 0:
        raise UniverseResolutionError(
            "universe_error: Binance exchange-info source returned empty response"
        )
    if not any(isinstance(item, Mapping) for item in raw_symbols):
        raise UniverseResolutionError(
            "universe_error: Binance exchange-info source returned malformed response"
        )

    symbols: dict[str, _ContractEvidence] = {}
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
        stated_multiplier, stated_multiplier_invalid = _stated_contract_multiplier(raw_contract)
        symbols[symbol] = _ContractEvidence(
            symbol=symbol,
            base_asset=base_asset,
            tick_size=_filter_text(raw_contract, "PRICE_FILTER", "tickSize"),
            step_size=_filter_text(raw_contract, "LOT_SIZE", "stepSize"),
            stated_multiplier=stated_multiplier,
            stated_multiplier_invalid=stated_multiplier_invalid,
        )

    if not symbols:
        raise UniverseResolutionError(
            "universe_error: Binance exchange-info source contained no admissible USDT perpetual contracts"
        )

    return symbols, {
        "contract_metadata_used": True,
        "binance_perpetual_contract_count": len(symbols),
        "non_crypto_contract_excluded_count": non_crypto_count,
        "non_perpetual_contract_excluded_count": non_perpetual_count,
        "non_trading_contract_excluded_count": non_trading_count,
        "malformed_contract_excluded_count": malformed_count,
        "existing_crypto_policy_excluded_count": policy_excluded_count,
    }


def _select_qualified_perpetuals(
    cohort: Sequence[QualifiedAsset],
    *,
    contracts: Mapping[str, _ContractEvidence],
    eligible_symbol_counts: Mapping[str, int],
    observed_volumes: Mapping[str, Decimal],
    admissible_volumes: Mapping[str, Decimal],
    minimum_volume: Decimal,
) -> tuple[tuple[tuple[QualifiedAsset, _ExchangeIdentityMatch], ...], list[dict[str, Any]]]:
    """Intersect the qualified cohort with admissible contracts. Never backfill."""

    chosen: dict[str, tuple[QualifiedAsset, _ExchangeIdentityMatch]] = {}
    rejections: list[dict[str, Any]] = []
    for asset in cohort:
        match, reason = _match_qualified_identity(
            asset,
            contracts=contracts,
            eligible_symbol_counts=eligible_symbol_counts,
        )
        if match is None:
            rejections.append(_identity_rejection(asset, reason or "no_binance_perp"))
            continue
        if match.exchange_symbol not in admissible_volumes:
            observed = observed_volumes.get(match.exchange_symbol)
            volume_reason = (
                "below_min_quote_volume"
                if observed is not None and observed < minimum_volume
                else "no_binance_perp"
            )
            rejections.append(_identity_rejection(asset, volume_reason, match=match))
            continue
        incumbent = chosen.get(match.exchange_symbol)
        if incumbent is not None:
            rejections.append(_identity_rejection(asset, "exchange_symbol_collision", match=match))
            continue
        chosen[match.exchange_symbol] = (asset, match)
    selected = tuple(sorted(chosen.values(), key=lambda item: (item[0].qualified_rank, item[1].exchange_symbol)))
    return selected, rejections


def _match_qualified_identity(
    asset: QualifiedAsset,
    *,
    contracts: Mapping[str, _ContractEvidence],
    eligible_symbol_counts: Mapping[str, int],
) -> tuple[_ExchangeIdentityMatch | None, str | None]:
    collision_count = eligible_symbol_counts.get(asset.provider_symbol, 1)
    verified = VERIFIED_CONTRACT_MAPPINGS.get(asset.provider_id)
    if verified is not None:
        if verified.provider_symbol != asset.provider_symbol:
            return None, "identity_mapping_rejected"
        contract = contracts.get(verified.exchange_symbol)
        if contract is None or contract.base_asset != verified.exchange_base_asset:
            return None, "verified_contract_mismatch"
        if not _multiplier_matches_contract(
            verified.contract_multiplier,
            verified.exchange_base_asset,
            verified.provider_symbol,
            contract,
        ):
            return None, "invalid_multiplier"
        if verified.price_quantity_basis != EXCHANGE_CONTRACT_PRICE_BASIS:
            return None, "invalid_multiplier"
        status = "verified_disambiguation" if collision_count > 1 else "verified_unique"
        return _identity_match(contract, verified.contract_multiplier, status), None
    if collision_count > 1:
        return None, "unresolved_ambiguity"
    contract = contracts.get(f"{asset.provider_symbol}USDT")
    if contract is None or contract.base_asset != asset.provider_symbol:
        return None, "no_binance_perp"
    if contract.stated_multiplier_invalid or (
        contract.stated_multiplier is not None and contract.stated_multiplier != 1
    ):
        return None, "invalid_multiplier"
    return _identity_match(contract, 1, "unique"), None


def _multiplier_matches_contract(
    contract_multiplier: int,
    exchange_base_asset: str,
    provider_symbol: str,
    contract: _ContractEvidence,
) -> bool:
    if contract_multiplier < 1:
        return False
    if contract_multiplier == 1 and exchange_base_asset != provider_symbol:
        return False
    if contract_multiplier != 1 and exchange_base_asset != f"{contract_multiplier}{provider_symbol}":
        return False
    if contract.stated_multiplier_invalid:
        return False
    if contract.stated_multiplier is not None and contract.stated_multiplier != contract_multiplier:
        return False
    return True


def _identity_match(
    contract: _ContractEvidence,
    contract_multiplier: int,
    collision_status: str,
) -> _ExchangeIdentityMatch:
    return _ExchangeIdentityMatch(
        exchange_symbol=contract.symbol,
        exchange_base_asset=contract.base_asset,
        contract_multiplier=contract_multiplier,
        price_quantity_basis=EXCHANGE_CONTRACT_PRICE_BASIS,
        collision_status=collision_status,
        tick_size=contract.tick_size,
        step_size=contract.step_size,
    )


def _identity_rejection(
    asset: QualifiedAsset,
    reason: str,
    *,
    match: _ExchangeIdentityMatch | None = None,
) -> dict[str, Any]:
    return {
        "provider_id": asset.provider_id,
        "provider_symbol": asset.provider_symbol,
        "raw_rank": asset.raw_rank,
        "qualified_rank": asset.qualified_rank,
        "reason": reason,
        "collision_status": match.collision_status if match is not None else reason,
        "exchange_symbol": match.exchange_symbol if match is not None else None,
    }


def _membership_diagnostic(asset: QualifiedAsset, match: _ExchangeIdentityMatch) -> dict[str, Any]:
    return {
        "provider_id": asset.provider_id,
        "provider_symbol": asset.provider_symbol,
        "provider_name": asset.provider_name,
        "raw_rank": asset.raw_rank,
        "qualified_rank": asset.qualified_rank,
        "market_cap_usd": _decimal_json(asset.market_cap),
        "last_updated": asset.last_updated,
        "exchange_symbol": match.exchange_symbol,
        "exchange_base_asset": match.exchange_base_asset,
        "contract_multiplier": match.contract_multiplier,
        "price_quantity_basis": match.price_quantity_basis,
        "tick_size": match.tick_size,
        "step_size": match.step_size,
        "collision_status": match.collision_status,
    }


def _observed_usdt_quote_volumes(tickers: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    volumes: dict[str, Decimal] = {}
    for raw_ticker in tickers:
        symbol = _ticker_symbol(raw_ticker)
        if not symbol.endswith("USDT"):
            continue
        quote_volume = _ticker_quote_volume(raw_ticker)
        if quote_volume is None:
            continue
        volumes[symbol] = quote_volume
    return volumes


def _filter_text(raw_contract: Mapping[str, Any], filter_type: str, field: str) -> str | None:
    filters = raw_contract.get("filters")
    if isinstance(filters, (str, bytes)) or not isinstance(filters, Sequence):
        return None
    for item in filters:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("filterType") or "").strip().upper() != filter_type:
            continue
        value = item.get(field)
        if isinstance(value, str):
            return value if value != "" else None
        if value is None or isinstance(value, bool):
            return None
        return str(value)
    return None


def _stated_contract_multiplier(raw_contract: Mapping[str, Any]) -> tuple[int | None, bool]:
    observed: list[int] = []
    for name in ("contractMultiplier", "priceMultiplier", "multiplier"):
        if name not in raw_contract:
            continue
        value = raw_contract.get(name)
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            return None, True
        try:
            if isinstance(value, float) and not value.is_integer():
                return None, True
            parsed = int(str(value))
        except (TypeError, ValueError):
            return None, True
        if parsed < 1:
            return None, True
        observed.append(parsed)
    if not observed:
        return None, False
    if any(item != observed[0] for item in observed):
        return None, True
    return observed[0], False


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
