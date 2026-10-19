"""Strict CMC REST reader for listings, identity, and global metrics.

Synthetic fixtures must set ``synthetic: true``. HTTP 200 with an API error is
not success. Null market data stays null.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from research.market_data_foundation.timeutil import future_timestamp

KEYLESS_ROOT = "https://pro-api.coinmarketcap.com/public-api"
LISTINGS_PATH = "/v3/cryptocurrency/listings/latest"
GLOBAL_METRICS_PATH = "/v1/global-metrics/quotes/latest"
MAP_PATH = "/v1/cryptocurrency/map"


class CmcContractError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CmcAsset:
    provider_id: str
    symbol: str
    name: str
    rank: int | None
    last_updated: str | None
    quote_last_updated: str | None
    market_cap_usd: str | None
    price_usd: str | None
    circulating_supply: str | None
    rank_status: str


@dataclass(frozen=True)
class CmcListingPage:
    assets: tuple[CmcAsset, ...]
    envelope_timestamp: str | None
    credit_count: str | None
    error_code: str
    error_message: str | None
    synthetic: bool
    raw_sha256: str


@dataclass(frozen=True)
class CmcGlobalMetrics:
    btc_dominance: str | None
    eth_dominance: str | None
    total_market_cap_usd: str | None
    last_updated: str | None
    envelope_timestamp: str | None
    credit_count: str | None
    synthetic: bool
    raw_sha256: str
    missing_fields: tuple[str, ...] = field(default_factory=tuple)
    domain_problems: tuple[str, ...] = field(default_factory=tuple)


def parse_listings(payload: Any, *, raw_sha256: str, synthetic: bool) -> CmcListingPage:
    status, data = _envelope(payload)
    if not isinstance(data, list):
        raise CmcContractError("listings_data_not_a_list")
    assets: list[CmcAsset] = []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise CmcContractError(f"listings_row_not_object:{index}")
        assets.append(_asset(row))
    return CmcListingPage(
        assets=tuple(assets),
        envelope_timestamp=_optional_text(status.get("timestamp")),
        credit_count=_optional_text(status.get("credit_count")),
        error_code="0",
        error_message=None,
        synthetic=synthetic,
        raw_sha256=raw_sha256,
    )


def parse_global_metrics(payload: Any, *, raw_sha256: str, synthetic: bool) -> CmcGlobalMetrics:
    status, data = _envelope(payload)
    if not isinstance(data, dict):
        raise CmcContractError("global_data_not_object")
    quote = data.get("quote")
    usd = quote.get("USD") if isinstance(quote, dict) else None
    missing: list[str] = []
    btc = _optional_decimal_text(data.get("btc_dominance"))
    eth = _optional_decimal_text(data.get("eth_dominance"))
    cap = _optional_decimal_text(usd.get("total_market_cap") if isinstance(usd, dict) else None)
    updated = _optional_text(data.get("last_updated"))
    if btc is None:
        missing.append("btc_dominance")
    if eth is None:
        missing.append("eth_dominance")
    if cap is None:
        missing.append("total_market_cap_usd")
    if updated is None:
        missing.append("last_updated")
    domain: list[str] = []
    for name, text, kind in (
        ("btc_dominance", btc, "dominance"),
        ("eth_dominance", eth, "dominance"),
        ("total_market_cap_usd", cap, "capitalization"),
    ):
        problem = _domain_problem(name, text, kind=kind)
        if problem is not None:
            domain.append(problem)
    if updated is not None and parse_utc_timestamp(updated) is None:
        domain.append("malformed_last_updated")
    if status.get("timestamp") not in (None, "") and parse_utc_timestamp(_optional_text(status.get("timestamp"))) is None:
        domain.append("malformed_envelope_timestamp")
    return CmcGlobalMetrics(
        btc_dominance=btc,
        eth_dominance=eth,
        total_market_cap_usd=cap,
        last_updated=updated,
        envelope_timestamp=_optional_text(status.get("timestamp")),
        credit_count=_optional_text(status.get("credit_count")),
        synthetic=synthetic,
        raw_sha256=raw_sha256,
        missing_fields=tuple(missing),
        domain_problems=tuple(domain),
    )


def membership_problems(page: CmcListingPage, *, expected: int = 100) -> tuple[str, ...]:
    """One hundred rows are not a Top-100 snapshot unless ranks are exactly 1..N."""

    problems: list[str] = []
    if page.envelope_timestamp is None:
        problems.append("missing_envelope_timestamp")
    ids = [asset.provider_id for asset in page.assets]
    if len(ids) != len(set(ids)):
        problems.append("duplicate_id")
    ranks = [asset.rank for asset in page.assets]
    if any(rank is None for rank in ranks):
        problems.append("null_or_invalid_rank")
    present = [rank for rank in ranks if rank is not None]
    if len(present) != len(set(present)):
        problems.append("duplicate_rank")
    if len(page.assets) != expected:
        problems.append("row_count")
    expected_ranks = set(range(1, expected + 1))
    if set(present) != expected_ranks:
        problems.append("ranks_are_not_exactly_1_to_n")
    return tuple(problems)


def parse_utc_timestamp(value: str | None) -> datetime | None:
    """Accept an aware ISO-8601 instant. Naive strings and prose are not times."""

    if value is None:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def temporal_problems(
    page: CmcListingPage,
    *,
    acquired_at: datetime,
    decision_cutoff: datetime,
    max_age: timedelta = timedelta(hours=6),
) -> tuple[str, ...]:
    """Timestamp validity is separate from rank completeness."""

    problems: list[str] = []
    _collect_time_problem(problems, "envelope", page.envelope_timestamp, acquired_at, decision_cutoff, max_age)
    for asset in page.assets:
        _collect_time_problem(problems, "asset", asset.last_updated, acquired_at, decision_cutoff, max_age)
        _collect_time_problem(problems, "quote", asset.quote_last_updated, acquired_at, decision_cutoff, max_age)
    return tuple(dict.fromkeys(problems))


def _collect_time_problem(
    problems: list[str],
    label: str,
    raw: str | None,
    acquired_at: datetime,
    decision_cutoff: datetime,
    max_age: timedelta,
) -> None:
    if raw is None:
        problems.append(f"missing_{label}_timestamp")
        return
    moment = parse_utc_timestamp(raw)
    if moment is None:
        problems.append(f"malformed_{label}_timestamp")
        return
    if future_timestamp(moment, received_at=acquired_at):
        problems.append(f"future_{label}_timestamp")
        return
    oldest_allowed = min(acquired_at, decision_cutoff) - max_age
    if moment < oldest_allowed:
        problems.append(f"stale_{label}_timestamp")


def _domain_problem(name: str, text: str | None, *, kind: str) -> str | None:
    if text is None:
        return None
    number = Decimal(text)
    if kind == "dominance" and (number < 0 or number > 100):
        return f"impossible_{name}"
    if kind == "capitalization" and number < 0:
        return f"impossible_{name}"
    return None


def _envelope(payload: Any) -> tuple[dict[str, Any], Any]:
    if not isinstance(payload, dict):
        raise CmcContractError("malformed_envelope")
    status = payload.get("status")
    if not isinstance(status, dict):
        raise CmcContractError("missing_status")
    error_code = status.get("error_code")
    if error_code in (None, ""):
        raise CmcContractError("missing_error_code")
    if str(error_code) != "0":
        message = status.get("error_message")
        raise CmcContractError(f"api_error:{error_code}:{message}")
    if "data" not in payload:
        raise CmcContractError("missing_data")
    return status, payload.get("data")


def _asset(row: dict[str, Any]) -> CmcAsset:
    provider_id = row.get("id")
    if isinstance(provider_id, bool) or provider_id in (None, ""):
        raise CmcContractError("missing_provider_id")
    symbol = row.get("symbol")
    name = row.get("name")
    if not isinstance(symbol, str) or not symbol.strip():
        raise CmcContractError("missing_symbol")
    if not isinstance(name, str) or not name.strip():
        raise CmcContractError("missing_name")
    rank, rank_status = _rank(row.get("cmc_rank"))
    quote = row.get("quote")
    usd = quote.get("USD") if isinstance(quote, dict) else None
    return CmcAsset(
        provider_id=str(provider_id),
        symbol=symbol.strip().upper(),
        name=name.strip(),
        rank=rank,
        last_updated=_optional_text(row.get("last_updated")),
        quote_last_updated=_optional_text(usd.get("last_updated") if isinstance(usd, dict) else None),
        market_cap_usd=_optional_decimal_text(usd.get("market_cap") if isinstance(usd, dict) else None),
        price_usd=_optional_decimal_text(usd.get("price") if isinstance(usd, dict) else None),
        circulating_supply=_optional_decimal_text(row.get("circulating_supply")),
        rank_status=rank_status,
    )


def _rank(value: Any) -> tuple[int | None, str]:
    if value is None or value == "":
        return None, "null"
    if isinstance(value, bool):
        return None, "invalid"
    try:
        if isinstance(value, float) and not value.is_integer():
            return None, "invalid"
        rank = int(value)
    except (TypeError, ValueError):
        return None, "invalid"
    if rank < 1:
        return None, "invalid"
    return rank, "valid"


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_decimal_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return format(number, "f")


def http_disposition(status_code: int) -> str:
    if status_code == 429:
        return "access_blocked"
    if status_code in {401, 403}:
        return "access_blocked"
    if status_code >= 500 or status_code == 408:
        return "unverified"
    if status_code != 200:
        return "malformed"
    return "verified"


def received_clock(moment: datetime) -> str:
    return moment.isoformat()
