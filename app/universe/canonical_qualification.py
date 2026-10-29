"""Qualified genuine-cryptocurrency cohort for strict market-cap discovery.

CoinPaprika raw rank is not the discovery boundary. Non-genuine assets are
removed, the remaining identities receive ``qualified_rank`` in market-cap
order, and only ``qualified_rank <= cohort_size`` can become new discoveries.
The cohort size is never larger than the canonical Top-100 limit.

Identity mapping uses the provider id plus exchange contract metadata.
A shared ticker is not enough. Verified multiplier mappings record the
exchange price unit and never rescale it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

CANONICAL_QUALIFIED_COHORT_LIMIT = 100
CANONICAL_RANKING_MAX_AGE = timedelta(hours=1)
CANONICAL_RANKING_FUTURE_SKEW = timedelta(minutes=5)
EXCHANGE_CONTRACT_PRICE_BASIS = "exchange_contract_unit"

CATEGORY_GENUINE = "genuine"
CATEGORY_STABLECOIN = "stablecoin"
CATEGORY_COMMODITY_PEGGED = "commodity_pegged"
CATEGORY_WRAPPED_OR_BRIDGED = "wrapped_or_bridged"
CATEGORY_LIQUID_STAKED_OR_RESTAKED = "liquid_staked_or_restaked"
CATEGORY_SYNTHETIC = "synthetic"
CATEGORY_LEVERAGED_PRODUCT = "leveraged_product"
CATEGORY_TOKENIZED_SECURITY = "tokenized_security"

EXCLUSION_CATEGORIES = frozenset(
    {
        CATEGORY_STABLECOIN,
        CATEGORY_COMMODITY_PEGGED,
        CATEGORY_WRAPPED_OR_BRIDGED,
        CATEGORY_LIQUID_STAKED_OR_RESTAKED,
        CATEGORY_SYNTHETIC,
        CATEGORY_LEVERAGED_PRODUCT,
        CATEGORY_TOKENIZED_SECURITY,
    }
)

# Payload tags that identify a non-genuine representation. Sector tags such as
# defi, meme, rwa, and layer-1 are intentionally absent.
EXCLUSION_TAG_PRIORITY = (
    ("stablecoin", CATEGORY_STABLECOIN),
    ("gold-backed", CATEGORY_COMMODITY_PEGGED),
    ("xstocks-ecosystem", CATEGORY_TOKENIZED_SECURITY),
    ("wrapped-token", CATEGORY_WRAPPED_OR_BRIDGED),
    ("liquid-staking-tokens-lst", CATEGORY_LIQUID_STAKED_OR_RESTAKED),
    ("liquid-staking-derivatives-lsd", CATEGORY_LIQUID_STAKED_OR_RESTAKED),
    ("liquid-restaking-tokens", CATEGORY_LIQUID_STAKED_OR_RESTAKED),
    ("restaking", CATEGORY_LIQUID_STAKED_OR_RESTAKED),
)

# Tag-index evidence reviewed on 2026-10-29 against CoinPaprika /v1/tickers.
# eigen-eigenlayer is omitted: the LST tag on that id names the EigenLayer
# protocol token, not a liquid-staking receipt.
_TAG_BACKED_EXCLUSIONS: dict[str, str] = {
    "usdt-tether": CATEGORY_STABLECOIN,
    "steth-lido-staked-ether": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "wsteth-wrapped-liquid-staked-ether-20": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "usds-usds": CATEGORY_STABLECOIN,
    "wbtc-wrapped-bitcoin": CATEGORY_WRAPPED_OR_BRIDGED,
    "cbbtc-coinbase-wrapped-btc": CATEGORY_WRAPPED_OR_BRIDGED,
    "weth-weth": CATEGORY_WRAPPED_OR_BRIDGED,
    "dai-dai": CATEGORY_STABLECOIN,
    "susds-susds": CATEGORY_STABLECOIN,
    "usde-ethena-usde": CATEGORY_STABLECOIN,
    "usd1-usd1": CATEGORY_STABLECOIN,
    "usdg-global-dollar": CATEGORY_STABLECOIN,
    "pyusd-paypal-usd": CATEGORY_STABLECOIN,
    "bfusd-bfusd": CATEGORY_STABLECOIN,
    "syrupusdc-syrupusdc": CATEGORY_STABLECOIN,
    "usdd-usdd": CATEGORY_STABLECOIN,
    "rseth-rseth": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "meth-meth": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "ousd-open-usd": CATEGORY_STABLECOIN,
    "bnsol-binance-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "lseth-liquid-staked-ethereum": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "usdce-usd-coine": CATEGORY_STABLECOIN,
    "gho-gho": CATEGORY_STABLECOIN,
    "usd0-usual-usd": CATEGORY_STABLECOIN,
    "msol-marinade-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "tusd-trueusd": CATEGORY_STABLECOIN,
    "solvbtc-solv-protocol-solvbtc": CATEGORY_WRAPPED_OR_BRIDGED,
    "cbeth-coinbase-wrapped-staked-eth": CATEGORY_WRAPPED_OR_BRIDGED,
    "tbtc-tbtc": CATEGORY_WRAPPED_OR_BRIDGED,
    "usdtb-usdtb": CATEGORY_STABLECOIN,
    "fdusd-first-digital-usd": CATEGORY_STABLECOIN,
    "frxeth-frax-ether": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "ebtc-etherfi-staked-btc": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "alusd-alchemixusd": CATEGORY_STABLECOIN,
    "sfrxeth-staked-frax-ether": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "frax-frax": CATEGORY_STABLECOIN,
    "rlusd-ripple-usd": CATEGORY_STABLECOIN,
    "susdai-staked-usdai": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "asbnb-astherus-staked-bnb": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "oeth-origin-ether": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "cash-cash": CATEGORY_STABLECOIN,
    "onyc-onyc": CATEGORY_STABLECOIN,
    "usdf-astherus-usdf": CATEGORY_STABLECOIN,
    "uusd-unity-usd": CATEGORY_STABLECOIN,
    "musd-metamask-usd": CATEGORY_STABLECOIN,
    "ezeth-renzo-restaked-eth": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "frxusd-frax-usd": CATEGORY_STABLECOIN,
    "hsol-helius-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "busd-binance-usd": CATEGORY_STABLECOIN,
    "gusd-gemini-dollar": CATEGORY_STABLECOIN,
    "buck-bucket-protocol-buck-stablecoin": CATEGORY_STABLECOIN,
    "fidd-fidelity-digital-dollar": CATEGORY_STABLECOIN,
    "fxusd-fx-protocol-fxusd": CATEGORY_STABLECOIN,
}

# Coin-page or ticker identity review on 2026-10-29. These ids were missing or
# incomplete in the tag index. Names alone are not a classification rule;
# each id was checked against provider id, name, and tags.
# Kept on purpose: hype-hyperliquid, ondo-ondo, stable-stable, eigen-eigenlayer.
_REVIEWED_EXCLUSIONS: dict[str, str] = {
    "usdc-usd-coin": CATEGORY_STABLECOIN,
    "usdf-falcon-finance": CATEGORY_STABLECOIN,
    "euroc-euro-coin": CATEGORY_STABLECOIN,
    "paxg-pax-gold": CATEGORY_COMMODITY_PEGGED,
    "xaut-tether-gold": CATEGORY_COMMODITY_PEGGED,
    "wbeth-wrapped-beacon-eth": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "weeth-wrapped-eeth": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "beth-binance-beacon-eth": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "jitosol-jito-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "reth-rocket-pool-eth": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "khype-kinetiq-staked-hype": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "lbtc-lombard-staked-btc": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "susde-ethena-staked-usde": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "jupsol-jupiter-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "stkaave-staked-aave": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "dsol-drift-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "vsol-the-vault-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "scnsol-socean-staked-sol": CATEGORY_LIQUID_STAKED_OR_RESTAKED,
    "wbnb-wrapped-bnb": CATEGORY_WRAPPED_OR_BRIDGED,
    "wtrx-wrapped-trx": CATEGORY_WRAPPED_OR_BRIDGED,
    "eth-ethereum-token": CATEGORY_WRAPPED_OR_BRIDGED,
    "sol-binance-peg-sol": CATEGORY_WRAPPED_OR_BRIDGED,
    "btcb-bitcoin-avalanche-bridged-btcb": CATEGORY_WRAPPED_OR_BRIDGED,
    "kas-wrapped-kaspa": CATEGORY_WRAPPED_OR_BRIDGED,
    "jlp-jupiter-perpetuals-liquidity-provider-token": CATEGORY_SYNTHETIC,
}


def _build_exclusion_registry() -> dict[str, str]:
    merged = dict(_TAG_BACKED_EXCLUSIONS)
    for provider_id, category in _REVIEWED_EXCLUSIONS.items():
        prior = merged.get(provider_id)
        if prior is not None and prior != category:
            raise RuntimeError(f"exclusion registry conflict for {provider_id}")
        if category not in EXCLUSION_CATEGORIES:
            raise RuntimeError(f"unknown exclusion category for {provider_id}")
        merged[provider_id] = category
    return merged


EXCLUSION_BY_PROVIDER_ID = _build_exclusion_registry()


@dataclass(frozen=True)
class VerifiedContractMapping:
    """Exchange contract unit for one provider id.

    ``contract_multiplier`` is the number of provider tokens represented by one
    exchange price unit. Callers must keep the exchange price, quantity, and
    tick size. The multiplier is not a rescale factor.
    """

    provider_id: str
    provider_symbol: str
    exchange_symbol: str
    exchange_base_asset: str
    contract_multiplier: int
    price_quantity_basis: str = EXCHANGE_CONTRACT_PRICE_BASIS


def _verified_mapping(
    provider_id: str,
    provider_symbol: str,
    exchange_base_asset: str,
    contract_multiplier: int,
) -> VerifiedContractMapping:
    if contract_multiplier < 1:
        raise RuntimeError(f"invalid multiplier for {provider_id}")
    if contract_multiplier == 1 and exchange_base_asset != provider_symbol:
        raise RuntimeError(f"unscaled mapping base mismatch for {provider_id}")
    if contract_multiplier != 1 and exchange_base_asset != f"{contract_multiplier}{provider_symbol}":
        raise RuntimeError(f"scaled mapping base mismatch for {provider_id}")
    return VerifiedContractMapping(
        provider_id=provider_id,
        provider_symbol=provider_symbol,
        exchange_symbol=f"{exchange_base_asset}USDT",
        exchange_base_asset=exchange_base_asset,
        contract_multiplier=contract_multiplier,
    )


# Public contract evidence on 2026-10-29: CoinPaprika id/symbol/name and Binance
# USD-M exchangeInfo (TRADING, PERPETUAL, quote USDT, underlyingType COIN,
# baseAsset). 1000-prefix rows are included only where the base asset is the
# provider symbol prefixed by that exact multiplier.
# Ticker collisions mapped here are the higher-ranked provider id whose name
# matches the established asset. CC, FLR, RAIN, and TON stay unmapped.
_VERIFIED_CONTRACT_MAPPINGS = (
    _verified_mapping("hype-hyperliquid", "HYPE", "HYPE", 1),
    _verified_mapping("arb-arbitrum", "ARB", "ARB", 1),
    _verified_mapping("grt-the-graph", "GRT", "GRT", 1),
    _verified_mapping("pump-pumpfun", "PUMP", "PUMP", 1),
    _verified_mapping("zec-zcash", "ZEC", "ZEC", 1),
    _verified_mapping("sol-solana", "SOL", "SOL", 1),
    _verified_mapping("uni-uniswap", "UNI", "UNI", 1),
    _verified_mapping("ltc-litecoin", "LTC", "LTC", 1),
    _verified_mapping("dot-polkadot", "DOT", "DOT", 1),
    _verified_mapping("atom-cosmos", "ATOM", "ATOM", 1),
    _verified_mapping("kas-kaspa", "KAS", "KAS", 1),
    _verified_mapping("doge-dogecoin", "DOGE", "DOGE", 1),
    _verified_mapping("jup-jupiter-exchange-token", "JUP", "JUP", 1),
    _verified_mapping("sky-sky", "SKY", "SKY", 1),
    _verified_mapping("trump-official-trump", "TRUMP", "TRUMP", 1),
    _verified_mapping("shib-shiba-inu", "SHIB", "1000SHIB", 1000),
    _verified_mapping("pepe-pepe", "PEPE", "1000PEPE", 1000),
    _verified_mapping("bonk-bonk", "BONK", "1000BONK", 1000),
    _verified_mapping("floki-floki-inu", "FLOKI", "1000FLOKI", 1000),
    _verified_mapping("luna-terra", "LUNC", "1000LUNC", 1000),
)

VERIFIED_CONTRACT_MAPPINGS: dict[str, VerifiedContractMapping] = {
    item.provider_id: item for item in _VERIFIED_CONTRACT_MAPPINGS
}


class CanonicalUniverseError(RuntimeError):
    """The ranking snapshot cannot support a qualified cohort."""


@dataclass(frozen=True)
class QualifiedAsset:
    provider_id: str
    provider_symbol: str
    provider_name: str
    raw_rank: int
    qualified_rank: int
    market_cap: Decimal
    last_updated: str


@dataclass(frozen=True)
class QualificationExclusion:
    provider_id: str
    provider_symbol: str
    raw_rank: int
    category: str


@dataclass(frozen=True)
class CanonicalCohort:
    cohort: tuple[QualifiedAsset, ...]
    exclusions: tuple[QualificationExclusion, ...]
    eligible_symbol_counts: Mapping[str, int]
    provider_asset_count: int
    valid_rank_count: int
    provider_max_rank: int | None
    missing_rank_count: int
    invalid_rank_count: int
    invalid_symbol_count: int
    qualified_rank_above_cohort_count: int
    exclusion_counts: Mapping[str, int]
    provider_last_updated_min: str | None
    provider_last_updated_max: str | None


def acquired_timestamp(value: str | None) -> datetime:
    """Parse the snapshot clock. A missing value means the caller asked for now."""

    if value is None:
        return datetime.now(UTC)
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            "(acquisition timestamp is malformed)"
        )
    return parsed


def qualify_canonical_cohort(
    market_caps: Sequence[Mapping[str, Any]],
    *,
    cohort_size: int,
    acquired_at: datetime,
) -> CanonicalCohort:
    """Return the genuine cohort or fail closed on a snapshot that cannot prove it."""

    if cohort_size < 1 or cohort_size > CANONICAL_QUALIFIED_COHORT_LIMIT:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            f"(qualified cohort size {cohort_size} is outside 1..{CANONICAL_QUALIFIED_COHORT_LIMIT})"
        )
    if acquired_at.tzinfo is None:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            "(acquisition timestamp is missing a timezone)"
        )
    acquired_at = acquired_at.astimezone(UTC)

    provider_assets = tuple(item for item in market_caps if isinstance(item, Mapping))
    _reject_duplicate_provider_ids(provider_assets)
    ranked, missing_rank_count, invalid_rank_count, invalid_symbol_count = _ranked_assets(provider_assets)
    if not ranked:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            "(no valid ranked cryptocurrency assets)"
        )

    cohort: list[QualifiedAsset] = []
    exclusions: list[QualificationExclusion] = []
    exclusion_counts: dict[str, int] = {category: 0 for category in sorted(EXCLUSION_CATEGORIES)}
    expected_rank = 1
    provider_max_rank = ranked[-1][0]
    above_cohort = 0
    window_updates: list[datetime] = []

    for rank, raw_asset in ranked:
        if len(cohort) >= cohort_size:
            if _category_for_asset(raw_asset) == CATEGORY_GENUINE and _provider_symbol(raw_asset):
                above_cohort += 1
            continue
        if rank < expected_rank:
            raise CanonicalUniverseError(
                "universe_error: CoinPaprika market-cap source returned incomplete ranking "
                f"(duplicate raw rank {rank})"
            )
        if rank > expected_rank:
            raise CanonicalUniverseError(
                "universe_error: CoinPaprika market-cap source returned incomplete ranking "
                f"(missing raw rank {expected_rank}; next raw rank {rank})"
            )
        expected_rank += 1
        provider_id = _require_provider_id(raw_asset, rank=rank)
        provider_symbol = _require_provider_symbol(raw_asset, provider_id=provider_id)
        market_cap = _require_market_cap(raw_asset, provider_id=provider_id)
        updated = _require_fresh_timestamp(raw_asset, provider_id=provider_id, acquired_at=acquired_at)
        window_updates.append(updated)
        category = _category_for_asset(raw_asset)
        if category != CATEGORY_GENUINE:
            exclusion_counts[category] = exclusion_counts.get(category, 0) + 1
            exclusions.append(
                QualificationExclusion(
                    provider_id=provider_id,
                    provider_symbol=provider_symbol,
                    raw_rank=rank,
                    category=category,
                )
            )
            continue
        cohort.append(
            QualifiedAsset(
                provider_id=provider_id,
                provider_symbol=provider_symbol,
                provider_name=_provider_name(raw_asset),
                raw_rank=rank,
                qualified_rank=len(cohort) + 1,
                market_cap=market_cap,
                last_updated=_timestamp_text(updated),
            )
        )

    if len(cohort) < cohort_size:
        last_rank = ranked[-1][0]
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            f"(highest valid rank {last_rank} produced {len(cohort)} qualified assets "
            f"< requested {cohort_size})"
        )
    _require_complete_symbol_identities(provider_assets, cohort)

    return CanonicalCohort(
        cohort=tuple(cohort),
        exclusions=tuple(exclusions),
        eligible_symbol_counts=_genuine_symbol_counts(provider_assets),
        provider_asset_count=len(provider_assets),
        valid_rank_count=len(ranked),
        provider_max_rank=provider_max_rank,
        missing_rank_count=missing_rank_count,
        invalid_rank_count=invalid_rank_count,
        invalid_symbol_count=invalid_symbol_count,
        qualified_rank_above_cohort_count=above_cohort,
        exclusion_counts=exclusion_counts,
        provider_last_updated_min=_timestamp_text(min(window_updates)) if window_updates else None,
        provider_last_updated_max=_timestamp_text(max(window_updates)) if window_updates else None,
    )


def _reject_duplicate_provider_ids(provider_assets: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for raw_asset in provider_assets:
        provider_id = _provider_id(raw_asset)
        if provider_id is None:
            continue
        if provider_id in seen:
            raise CanonicalUniverseError(
                "universe_error: CoinPaprika market-cap source returned duplicate provider identity: "
                f"{provider_id}"
            )
        seen.add(provider_id)


def _ranked_assets(
    provider_assets: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[int, Mapping[str, Any]]], int, int, int]:
    missing_rank_count = 0
    invalid_rank_count = 0
    invalid_symbol_count = 0
    ranked: list[tuple[int, Mapping[str, Any]]] = []
    for raw_asset in provider_assets:
        if not _provider_symbol(raw_asset):
            invalid_symbol_count += 1
        rank, status = _raw_rank(raw_asset)
        if status == "missing":
            missing_rank_count += 1
            continue
        if status != "valid" or rank is None:
            invalid_rank_count += 1
            continue
        ranked.append((rank, raw_asset))
    ranked.sort(key=lambda item: (item[0], _provider_id(item[1]) or ""))
    return ranked, missing_rank_count, invalid_rank_count, invalid_symbol_count


def _require_complete_symbol_identities(
    provider_assets: Sequence[Mapping[str, Any]],
    cohort: Sequence[QualifiedAsset],
) -> None:
    """A cohort ticker cannot be mapped while another row with that ticker has no id."""

    cohort_symbols = {asset.provider_symbol for asset in cohort}
    for raw_asset in provider_assets:
        provider_symbol = _provider_symbol(raw_asset)
        if provider_symbol not in cohort_symbols:
            continue
        if _provider_id(raw_asset) is None:
            raise CanonicalUniverseError(
                "universe_error: CoinPaprika market-cap source returned incomplete ranking "
                f"(ticker {provider_symbol} has a row without a provider id)"
            )


def _genuine_symbol_counts(provider_assets: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, set[str]] = defaultdict(set)
    for raw_asset in provider_assets:
        provider_symbol = _provider_symbol(raw_asset)
        provider_id = _provider_id(raw_asset)
        if not provider_symbol or provider_id is None:
            continue
        if _category_for_asset(raw_asset) != CATEGORY_GENUINE:
            continue
        counts[provider_symbol].add(provider_id)
    return {symbol: len(identities) for symbol, identities in counts.items()}


def _category_for_asset(raw_asset: Mapping[str, Any]) -> str:
    provider_id = _provider_id(raw_asset) or ""
    explicit = _explicit_category(raw_asset)
    tagged = _tagged_category(raw_asset)
    registered = EXCLUSION_BY_PROVIDER_ID.get(provider_id)
    chosen = explicit or tagged or registered or CATEGORY_GENUINE
    for candidate in (explicit, tagged, registered):
        if candidate is not None and candidate != chosen:
            raise CanonicalUniverseError(
                "universe_error: CoinPaprika market-cap source returned ambiguous identity evidence "
                f"for {provider_id or 'missing-id'}"
            )
    return chosen


def _explicit_category(raw_asset: Mapping[str, Any]) -> str | None:
    value = raw_asset.get("qualification_class")
    if value in (None, ""):
        return None
    category = str(value).strip().lower()
    if category == CATEGORY_GENUINE:
        return CATEGORY_GENUINE
    if category in EXCLUSION_CATEGORIES:
        return category
    raise CanonicalUniverseError(
        "universe_error: CoinPaprika market-cap source returned ambiguous identity evidence "
        f"(unknown qualification class {category})"
    )


def _tagged_category(raw_asset: Mapping[str, Any]) -> str | None:
    tags = _tag_ids(raw_asset)
    if not tags:
        return None
    for tag_id, category in EXCLUSION_TAG_PRIORITY:
        if tag_id in tags:
            return category
    return None


def _tag_ids(raw_asset: Mapping[str, Any]) -> set[str]:
    raw_tags = raw_asset.get("tags")
    if raw_tags in (None, ""):
        return set()
    if isinstance(raw_tags, (str, bytes)) or not isinstance(raw_tags, Sequence):
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned ambiguous identity evidence "
            "(asset tags are malformed)"
        )
    tags: set[str] = set()
    for item in raw_tags:
        if isinstance(item, str):
            text = item.strip().lower()
        elif isinstance(item, Mapping) and item.get("id") not in (None, ""):
            text = str(item.get("id")).strip().lower()
        else:
            raise CanonicalUniverseError(
                "universe_error: CoinPaprika market-cap source returned ambiguous identity evidence "
                "(asset tag is malformed)"
            )
        if text:
            tags.add(text)
    return tags


def _require_provider_id(raw_asset: Mapping[str, Any], *, rank: int) -> str:
    provider_id = _provider_id(raw_asset)
    if provider_id is None:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            f"(raw rank {rank} is missing a provider id)"
        )
    return provider_id


def _require_provider_symbol(raw_asset: Mapping[str, Any], *, provider_id: str) -> str:
    provider_symbol = _provider_symbol(raw_asset)
    if not provider_symbol:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            f"(provider id {provider_id} is missing a symbol)"
        )
    return provider_symbol


def _require_market_cap(raw_asset: Mapping[str, Any], *, provider_id: str) -> Decimal:
    market_cap = _market_cap(raw_asset)
    if market_cap is None or market_cap <= 0:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            f"(provider id {provider_id} is missing a positive USD market cap)"
        )
    return market_cap


def _require_fresh_timestamp(
    raw_asset: Mapping[str, Any],
    *,
    provider_id: str,
    acquired_at: datetime,
) -> datetime:
    updated = _parse_timestamp(raw_asset.get("last_updated"))
    if updated is None:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned incomplete ranking "
            f"(provider id {provider_id} is missing last_updated)"
        )
    age = acquired_at - updated
    if age > CANONICAL_RANKING_MAX_AGE or updated - acquired_at > CANONICAL_RANKING_FUTURE_SKEW:
        raise CanonicalUniverseError(
            "universe_error: CoinPaprika market-cap source returned stale ranking "
            f"(provider id {provider_id} last_updated {_timestamp_text(updated)})"
        )
    return updated


def _provider_id(raw_asset: Mapping[str, Any]) -> str | None:
    value = raw_asset.get("id")
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    return text or None


def _provider_symbol(raw_asset: Mapping[str, Any]) -> str:
    value = raw_asset.get("symbol")
    if value is None:
        return ""
    return str(value).strip().upper()


def _provider_name(raw_asset: Mapping[str, Any]) -> str:
    value = raw_asset.get("name")
    if value is None:
        return ""
    return str(value).strip()


def _raw_rank(raw_asset: Mapping[str, Any]) -> tuple[int | None, str]:
    value = raw_asset.get("rank")
    if isinstance(value, bool) or value in (None, ""):
        return None, "missing"
    try:
        rank = int(str(value))
    except (TypeError, ValueError):
        return None, "invalid"
    if rank < 1:
        return None, "invalid"
    return rank, "valid"


def _market_cap(raw_asset: Mapping[str, Any]) -> Decimal | None:
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
    if not market_cap.is_finite() or market_cap <= 0:
        return None
    return market_cap


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
