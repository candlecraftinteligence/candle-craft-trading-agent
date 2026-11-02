"""Shared bounded identity acquisition; ticker text is never an identity key."""
from app.cmc.endpoints import Endpoint, rows


def family_complete(fetches):
    """Only a terminal provider page proves that a paginated family is complete."""
    return bool(fetches) and not any(f.error for f in fetches) and fetches[-1].pagination_complete is True


def page_identity(endpoint, item):
    if endpoint == Endpoint.MAP:
        return item.get("id")
    if endpoint in {Endpoint.EXCHANGE_PAIRS, Endpoint.CRYPTO_PAIRS}:
        return item.get("market_id")
    return None


def validate_family(fetches, *, required_ids=None):
    if (not fetches or any(f.error for f in fetches) or len({f.endpoint for f in fetches}) != 1
            or not family_complete(fetches) and not (required_ids and fetches[0].endpoint == Endpoint.MAP)):
        return False
    seen, exchange = set(), None
    for fetch in fetches:
        if fetch.endpoint == Endpoint.EXCHANGE_PAIRS:
            current = (fetch.data.get("exchange_id"), fetch.data.get("exchange_slug"))
            if current != (270, "binance") or exchange is not None and current != exchange:
                return False
            exchange = current
        for item in rows(fetch.endpoint, fetch.data):
            key = page_identity(fetch.endpoint, item) if isinstance(item, dict) else None
            if isinstance(key, bool) or not isinstance(key, int) or key <= 0 or key in seen:
                return False
            seen.add(key)
    return required_ids is None or set(required_ids) <= seen
