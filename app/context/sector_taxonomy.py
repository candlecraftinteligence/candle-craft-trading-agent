from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict


SECTOR_TAXONOMY_VERSION: Final = "cci_sector_taxonomy_v1_2026_08_28"
UNCLASSIFIED_SECTOR: Final = "UNCLASSIFIED"


class SectorAssetType(str, Enum):
    DIRECTIONAL = "DIRECTIONAL"
    BENCHMARK_ONLY = "BENCHMARK_ONLY"
    NON_DIRECTIONAL = "NON_DIRECTIONAL"
    UNCLASSIFIED = "UNCLASSIFIED"


class PrimarySector(str, Enum):
    L1 = "L1"
    L2 = "L2"
    DEFI = "DEFI"
    AI_COMPUTE = "AI_COMPUTE"
    RWA = "RWA"
    MEME = "MEME"
    GAMING = "GAMING"
    EXCHANGE_TRADING = "EXCHANGE_TRADING"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    PRIVACY = "PRIVACY"
    PAYMENTS = "PAYMENTS"
    STORAGE = "STORAGE"
    ORACLE = "ORACLE"
    LIQUID_STAKING_RESTAKING = "LIQUID_STAKING_RESTAKING"


class SectorClassification(BaseModel):
    symbol: str
    base_asset: str
    primary_sector: str = UNCLASSIFIED_SECTOR
    secondary_tags: tuple[str, ...] = ()
    asset_type: SectorAssetType = SectorAssetType.UNCLASSIFIED
    taxonomy_version: str = SECTOR_TAXONOMY_VERSION
    exclusion_reason: str | None = None

    model_config = ConfigDict(frozen=True)


# One reviewed primary sector is canonical for every listed base asset. Secondary
# tags below are descriptive only and never contribute another observation.
_PRIMARY_SECTOR_MEMBERS: Final = MappingProxyType(
    {
        PrimarySector.L1: frozenset(
            {
                "ADA",
                "ALGO",
                "APT",
                "AVAX",
                "BNB",
                "CELO",
                "EGLD",
                "ETH",
                "FTM",
                "HBAR",
                "KAS",
                "NEAR",
                "S",
                "SEI",
                "SOL",
                "SUI",
                "TON",
                "TRX",
                "XTZ",
            }
        ),
        PrimarySector.L2: frozenset(
            {
                "ARB",
                "BLAST",
                "IMX",
                "MATIC",
                "METIS",
                "MNT",
                "OP",
                "POL",
                "SCR",
                "STRK",
                "TAIKO",
                "ZK",
            }
        ),
        PrimarySector.DEFI: frozenset(
            {
                "1INCH",
                "AAVE",
                "BAL",
                "CAKE",
                "COMP",
                "CRV",
                "CVX",
                "DYDX",
                "ENA",
                "GMX",
                "JUP",
                "LQTY",
                "MKR",
                "PENDLE",
                "RAY",
                "RUNE",
                "SKY",
                "SNX",
                "SUSHI",
                "UNI",
            }
        ),
        PrimarySector.AI_COMPUTE: frozenset(
            {
                "AI",
                "AIXBT",
                "AKT",
                "FET",
                "GLM",
                "GRASS",
                "IO",
                "NEARAI",
                "NMR",
                "OLAS",
                "RENDER",
                "RNDR",
                "TAO",
                "VIRTUAL",
                "WLD",
            }
        ),
        PrimarySector.RWA: frozenset(
            {
                "CFG",
                "MPL",
                "OM",
                "ONDO",
                "PLUME",
                "POLYX",
                "RIO",
                "TRU",
            }
        ),
        PrimarySector.MEME: frozenset(
            {
                "BABYDOGE",
                "BOME",
                "BONK",
                "BRETT",
                "DOGE",
                "FLOKI",
                "MEME",
                "MOG",
                "NEIRO",
                "PEPE",
                "PNUT",
                "POPCAT",
                "SHIB",
                "TRUMP",
                "WIF",
            }
        ),
        PrimarySector.GAMING: frozenset(
            {
                "ALICE",
                "APE",
                "AXS",
                "BEAM",
                "GALA",
                "ILV",
                "MANA",
                "PIXEL",
                "PRIME",
                "RON",
                "SAND",
                "YGG",
            }
        ),
        PrimarySector.EXCHANGE_TRADING: frozenset(
            {
                "CRO",
                "GT",
                "KCS",
                "LEO",
                "OKB",
            }
        ),
        PrimarySector.INFRASTRUCTURE: frozenset(
            {
                "ATOM",
                "DOT",
                "GRT",
                "ICP",
                "IOTA",
                "QNT",
                "TIA",
                "ZRO",
            }
        ),
        PrimarySector.PRIVACY: frozenset({"DASH", "ROSE", "SCRT", "XMR", "ZEC"}),
        PrimarySector.PAYMENTS: frozenset(
            {"BCH", "LTC", "NANO", "XLM", "XRP", "XVG"}
        ),
        PrimarySector.STORAGE: frozenset({"AR", "BLZ", "FIL", "SC", "STORJ"}),
        PrimarySector.ORACLE: frozenset({"API3", "BAND", "LINK", "PYTH", "TRB"}),
        PrimarySector.LIQUID_STAKING_RESTAKING: frozenset(
            {"EIGEN", "ETHFI", "LDO", "REZ", "SSV"}
        ),
    }
)

_SECONDARY_TAGS: Final = MappingProxyType(
    {
        "BNB": ("EXCHANGE_TRADING",),
        "IMX": ("GAMING",),
        "JUP": ("SOL_ECOSYSTEM", "EXCHANGE_TRADING"),
        "LDO": ("DEFI",),
        "NEAR": ("AI_COMPUTE",),
        "PENDLE": ("RWA",),
        "PYTH": ("SOL_ECOSYSTEM",),
        "RAY": ("SOL_ECOSYSTEM",),
        "TIA": ("MODULAR_BLOCKCHAIN",),
    }
)

_STABLECOINS: Final = frozenset(
    {
        "BUSD",
        "DAI",
        "FDUSD",
        "FRAX",
        "PYUSD",
        "TUSD",
        "USDC",
        "USDD",
        "USDE",
        "USDS",
        "USDT",
    }
)
_WRAPPED_OR_RECEIPT_ASSETS: Final = frozenset(
    {"BTCB", "CBETH", "RETH", "STETH", "WBETH", "WBTC", "WETH", "WSTETH"}
)
_COMMODITY_BACKED_ASSETS: Final = frozenset({"PAXG", "XAUT"})
_LEVERAGED_SUFFIXES: Final = ("BULL", "BEAR", "DOWN", "2L", "2S", "3L", "3S", "4L", "4S", "5L", "5S")
_MULTIPLIER_PREFIXES: Final = ("1000000", "10000", "1000")
_QUOTE_SUFFIXES: Final = ("USDT", "USDC", "BUSD", "USD", "PERP")


def classify_sector(symbol: str) -> SectorClassification:
    normalized_symbol = str(symbol).strip().upper()
    base_asset = base_asset_from_symbol(normalized_symbol)
    if base_asset == "BTC":
        return SectorClassification(
            symbol=normalized_symbol,
            base_asset=base_asset,
            asset_type=SectorAssetType.BENCHMARK_ONLY,
            exclusion_reason="btc_benchmark_only",
        )
    non_directional_reason = _non_directional_reason(base_asset)
    if non_directional_reason is not None:
        return SectorClassification(
            symbol=normalized_symbol,
            base_asset=base_asset,
            asset_type=SectorAssetType.NON_DIRECTIONAL,
            exclusion_reason=non_directional_reason,
        )
    primary_sector = _PRIMARY_BY_BASE.get(base_asset)
    if primary_sector is None:
        return SectorClassification(
            symbol=normalized_symbol,
            base_asset=base_asset,
            asset_type=SectorAssetType.UNCLASSIFIED,
            exclusion_reason="unclassified_asset",
        )
    return SectorClassification(
        symbol=normalized_symbol,
        base_asset=base_asset,
        primary_sector=primary_sector.value,
        secondary_tags=_SECONDARY_TAGS.get(base_asset, ()),
        asset_type=SectorAssetType.DIRECTIONAL,
    )


def base_asset_from_symbol(symbol: str) -> str:
    base_asset = str(symbol).strip().upper()
    for suffix in _QUOTE_SUFFIXES:
        if base_asset.endswith(suffix) and len(base_asset) > len(suffix):
            base_asset = base_asset[: -len(suffix)]
            break
    for prefix in _MULTIPLIER_PREFIXES:
        if base_asset.startswith(prefix) and len(base_asset) > len(prefix):
            base_asset = base_asset[len(prefix) :]
            break
    return base_asset


def _non_directional_reason(base_asset: str) -> str | None:
    if base_asset in _STABLECOINS:
        return "stablecoin"
    if base_asset in _WRAPPED_OR_RECEIPT_ASSETS:
        return "wrapped_or_receipt_asset"
    if base_asset in _COMMODITY_BACKED_ASSETS:
        return "commodity_backed_asset"
    if any(base_asset.endswith(suffix) for suffix in _LEVERAGED_SUFFIXES):
        return "leveraged_token"
    return None


def _primary_lookup() -> MappingProxyType:
    lookup: dict[str, PrimarySector] = {}
    for sector, members in _PRIMARY_SECTOR_MEMBERS.items():
        for base_asset in members:
            previous = lookup.setdefault(base_asset, sector)
            if previous != sector:
                raise RuntimeError(
                    f"sector taxonomy assigns {base_asset} to both {previous.value} and {sector.value}"
                )
    return MappingProxyType(lookup)


_PRIMARY_BY_BASE: Final = _primary_lookup()


__all__ = [
    "PrimarySector",
    "SECTOR_TAXONOMY_VERSION",
    "SectorAssetType",
    "SectorClassification",
    "UNCLASSIFIED_SECTOR",
    "base_asset_from_symbol",
    "classify_sector",
]
