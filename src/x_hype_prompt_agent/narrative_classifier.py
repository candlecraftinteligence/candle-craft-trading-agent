from __future__ import annotations

import re

from .models import NewsItem

BTC_LIQUIDITY = "BTC_LIQUIDITY"
ETH_ECOSYSTEM = "ETH_ECOSYSTEM"
SOLANA_ECOSYSTEM = "SOLANA_ECOSYSTEM"
ETF_FLOWS = "ETF_FLOWS"
MACRO_FED_CPI_RATES = "MACRO_FED_CPI_RATES"
DOLLAR_DXY = "DOLLAR_DXY"
REGULATION_SEC_CFTC_EU_MICA = "REGULATION_SEC_CFTC_EU_MICA"
EXCHANGE_BINANCE_COINBASE_MEXC = "EXCHANGE_BINANCE_COINBASE_MEXC"
LISTING_DELISTING = "LISTING_DELISTING"
HACK_EXPLOIT_SECURITY = "HACK_EXPLOIT_SECURITY"
WHALE_MOVEMENT = "WHALE_MOVEMENT"
LIQUIDATION_SQUEEZE = "LIQUIDATION_SQUEEZE"
FUNDING_OPEN_INTEREST = "FUNDING_OPEN_INTEREST"
STABLECOIN_LIQUIDITY = "STABLECOIN_LIQUIDITY"
AI_CRYPTO = "AI_CRYPTO"
RWA = "RWA"
MEMECOIN_ROTATION = "MEMECOIN_ROTATION"
DEFI_RISK = "DEFI_RISK"
LAYER2 = "LAYER2"
MARKET_STRUCTURE = "MARKET_STRUCTURE"
CONTROVERSY = "CONTROVERSY"
RUMOR = "RUMOR"
SPONSORED_OR_LOW_QUALITY = "SPONSORED_OR_LOW_QUALITY"
GENERIC_ALTCOIN_NOISE = "GENERIC_ALTCOIN_NOISE"

ALL_NARRATIVES = (
    BTC_LIQUIDITY,
    ETH_ECOSYSTEM,
    SOLANA_ECOSYSTEM,
    ETF_FLOWS,
    MACRO_FED_CPI_RATES,
    DOLLAR_DXY,
    REGULATION_SEC_CFTC_EU_MICA,
    EXCHANGE_BINANCE_COINBASE_MEXC,
    LISTING_DELISTING,
    HACK_EXPLOIT_SECURITY,
    WHALE_MOVEMENT,
    LIQUIDATION_SQUEEZE,
    FUNDING_OPEN_INTEREST,
    STABLECOIN_LIQUIDITY,
    AI_CRYPTO,
    RWA,
    MEMECOIN_ROTATION,
    DEFI_RISK,
    LAYER2,
    MARKET_STRUCTURE,
    CONTROVERSY,
    RUMOR,
    SPONSORED_OR_LOW_QUALITY,
    GENERIC_ALTCOIN_NOISE,
)

NARRATIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    BTC_LIQUIDITY: (
        "bitcoin",
        "btc",
        "bitcoin liquidity",
        "btc liquidity",
        "bitcoin dominance",
        "btc dominance",
    ),
    ETH_ECOSYSTEM: ("ethereum", "eth", "ether", "staking", "eigenlayer", "restaking", "ethereum etf"),
    SOLANA_ECOSYSTEM: ("solana", " sol ", "sol ", "jito", "jupiter", "pump.fun", "sol ecosystem"),
    ETF_FLOWS: ("etf", "inflow", "outflow", "blackrock", "ibit", "fidelity", "spot bitcoin etf", "spot ether etf"),
    MACRO_FED_CPI_RATES: ("fed", "federal reserve", "cpi", "inflation", "rates", "rate cut", "rate hike", "fomc"),
    DOLLAR_DXY: ("dxy", "dollar index", "us dollar", "dollar strengthens", "dollar weakens"),
    REGULATION_SEC_CFTC_EU_MICA: (
        "sec",
        "cftc",
        "mica",
        "regulator",
        "regulation",
        "lawsuit",
        "approval",
        "rejection",
        "ban",
        "investigation",
        "enforcement",
    ),
    EXCHANGE_BINANCE_COINBASE_MEXC: ("binance", "coinbase", "mexc", "kraken", "okx", "bybit", "exchange"),
    LISTING_DELISTING: ("listing", "lists", "listed", "delisting", "delists", "delisted"),
    HACK_EXPLOIT_SECURITY: (
        "hack",
        "hacked",
        "exploit",
        "security breach",
        "breach",
        "drained",
        "stolen",
        "phishing",
        "rug pull",
        "compromised",
    ),
    WHALE_MOVEMENT: ("whale", "large transfer", "moved", "wallet", "on-chain", "onchain", "deposit", "withdrawal"),
    LIQUIDATION_SQUEEZE: ("liquidation", "liquidations", "short squeeze", "long squeeze", "cascade", "squeeze"),
    FUNDING_OPEN_INTEREST: ("funding", "open interest", "oi", "perp", "perpetual", "basis"),
    STABLECOIN_LIQUIDITY: ("stablecoin", "usdt", "usdc", "tether", "circle", "stablecoin supply"),
    AI_CRYPTO: ("ai crypto", "artificial intelligence", "depin ai", "ai agent", "ai token"),
    RWA: ("rwa", "tokenized", "tokenization", "real-world asset", "treasury token"),
    MEMECOIN_ROTATION: ("meme", "memecoin", "doge", "shib", "pepe", "bonk", "mania"),
    DEFI_RISK: ("defi", "protocol", "tvl", "governance attack", "oracle", "liquidity pool"),
    LAYER2: ("layer 2", "layer-2", "l2", "arbitrum", "optimism", "base network", "zk rollup"),
    MARKET_STRUCTURE: (
        "breakout",
        "breakdown",
        "support",
        "resistance",
        "market structure",
        "liquidity",
        "order book",
    ),
    CONTROVERSY: (
        "controversy",
        "backlash",
        "criticized",
        "accused",
        "denies",
        "probe",
        "lawsuit",
        "insolvency",
        "freeze",
        "frozen",
    ),
    RUMOR: ("rumor", "rumour", "unconfirmed", "reportedly", "sources say", "alleged", "claims"),
    SPONSORED_OR_LOW_QUALITY: (
        "sponsored",
        "press release",
        "partner content",
        "advertisement",
        "best coins to buy",
        "top 10",
        "price prediction",
        "presale",
        "next 100x",
    ),
}

MINOR_ALTCOIN_PATTERNS = (
    re.compile(r"\bpartnership\b", re.IGNORECASE),
    re.compile(r"\bcommunity\b", re.IGNORECASE),
    re.compile(r"\bairdrop\b", re.IGNORECASE),
    re.compile(r"\broadmap\b", re.IGNORECASE),
    re.compile(r"\bprice prediction\b", re.IGNORECASE),
)


def classify_narratives(item: NewsItem) -> tuple[str, ...]:
    text = _story_text(item)
    narratives: list[str] = []
    for narrative, phrases in NARRATIVE_KEYWORDS.items():
        if any(_contains_phrase(text, phrase) for phrase in phrases):
            narratives.append(narrative)

    high_signal = set(narratives) - {SPONSORED_OR_LOW_QUALITY, RUMOR}
    if GENERIC_ALTCOIN_NOISE not in narratives and _looks_like_generic_altcoin_noise(text, high_signal):
        narratives.append(GENERIC_ALTCOIN_NOISE)
    return tuple(narrative for narrative in ALL_NARRATIVES if narrative in set(narratives))


def has_narrative(item: NewsItem, narrative: str) -> bool:
    return narrative in classify_narratives(item)


def _story_text(item: NewsItem) -> str:
    return f" {item.title} {item.summary} {item.raw_category} ".lower()


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = phrase.lower()
    if phrase.startswith(" ") or phrase.endswith(" "):
        return phrase in text
    if any(char in phrase for char in ".+-"):
        return phrase in text
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _looks_like_generic_altcoin_noise(text: str, high_signal: set[str]) -> bool:
    if high_signal:
        return False
    weak_asset_signal = bool(re.search(r"\b[A-Z]{3,6}\b", text.upper()))
    weak_phrase = any(pattern.search(text) for pattern in MINOR_ALTCOIN_PATTERNS)
    return weak_phrase or ("altcoin" in text and not high_signal) or weak_asset_signal and "major" not in text
