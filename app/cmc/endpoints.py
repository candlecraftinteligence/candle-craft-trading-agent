"""Official REST routes reviewed 2026-10-26. Access remains account-specific."""
from dataclasses import dataclass
from enum import Enum
from math import ceil


class Endpoint(str, Enum):
    LISTINGS = "/v3/cryptocurrency/listings/latest"
    MAP = "/v1/cryptocurrency/map"
    HISTORICAL_LISTINGS = "/v1/cryptocurrency/listings/historical"
    EXCHANGES = "/v5/exchange/derivatives/list"
    EXCHANGE_PAIRS = "/v5/exchange/derivatives/market-pairs/list/latest"
    CRYPTO_PAIRS = "/v5/cryptocurrency/derivatives/market-pairs/list/latest"
    LIQUIDATIONS = "/v5/derivatives/liquidations/quotes/latest"
    EXCHANGE_LIQUIDATIONS = "/v5/derivatives/liquidations/exchange/list/latest"
    CRYPTO_LIQUIDATIONS = "/v5/derivatives/liquidations/cryptocurrency/list/latest"
    GLOBAL = "/v1/global-metrics/quotes/latest"
    FEAR_GREED = "/v3/fear-and-greed/latest"
    ALTCOIN = "/v1/altcoin-season-index/latest"
    CATEGORIES = "/v1/cryptocurrency/categories"
    CATEGORY = "/v1/cryptocurrency/category"
    KEY_INFO = "/v1/key/info"


@dataclass(frozen=True)
class Contract:
    rows_key: str | None = None
    paginated: bool = False
    page_max: int = 100
    credit_rows: int | None = None
    keyless: bool = False
    ttl: int = 300


CONTRACTS = {
    Endpoint.LISTINGS: Contract(paginated=True, page_max=5000, credit_rows=250, keyless=True),
    Endpoint.MAP: Contract(paginated=True, page_max=5000, credit_rows=0, keyless=True, ttl=86400),
    Endpoint.HISTORICAL_LISTINGS: Contract(paginated=True, page_max=5000, credit_rows=100, ttl=86400),
    Endpoint.EXCHANGES: Contract("exchanges", True, 5000, 250, ttl=300),
    Endpoint.EXCHANGE_PAIRS: Contract("market_pairs", True, 5000, 250, ttl=300),
    Endpoint.CRYPTO_PAIRS: Contract("market_pairs", True, 5000, 250, ttl=300),
    Endpoint.LIQUIDATIONS: Contract(ttl=300),
    Endpoint.EXCHANGE_LIQUIDATIONS: Contract("exchanges", True, 250, 250, ttl=300),
    Endpoint.CRYPTO_LIQUIDATIONS: Contract("cryptocurrencies", True, 250, 250, ttl=300),
    Endpoint.GLOBAL: Contract(keyless=True),
    Endpoint.FEAR_GREED: Contract(keyless=True, ttl=900),
    Endpoint.ALTCOIN: Contract(keyless=True, ttl=3600),
    Endpoint.CATEGORIES: Contract(paginated=True, page_max=5000, credit_rows=200, keyless=True, ttl=3600),
    Endpoint.CATEGORY: Contract("coins", True, 1000, 250, keyless=True, ttl=3600),
    Endpoint.KEY_INFO: Contract(ttl=3600),
}


def rows(endpoint: Endpoint, data):
    key = CONTRACTS[endpoint].rows_key
    value = data.get(key) if key and isinstance(data, dict) else data
    if not isinstance(value, list):
        raise ValueError("invalid paginated data contract")
    return value


def reserved_credits(endpoint: Endpoint, params: dict) -> int:
    contract = CONTRACTS[endpoint]
    if contract.credit_rows == 0:
        return 0
    if contract.credit_rows is None:
        return 1
    # Exactly one conversion is permitted by this integration.
    estimate = max(1, ceil(int(params.get("limit", 100 if contract.paginated else 1)) / contract.credit_rows))
    return estimate + (1 if endpoint == Endpoint.CATEGORIES else 0)
