"""Bounded public sampling. Injected transports stay labeled synthetic."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from research.market_data_foundation.budget import BudgetExceeded, StudyBudget, StudyCancelled
from research.market_data_foundation.cmc import (
    GLOBAL_METRICS_PATH,
    KEYLESS_ROOT,
    LISTINGS_PATH,
    http_disposition,
    membership_problems,
    parse_global_metrics,
    parse_listings,
)
from research.market_data_foundation.evidence import sha256_bytes
from research.market_data_foundation.paths import resolve_output
from research.market_data_foundation.top100 import (
    VenueContract,
    assert_stage_arithmetic,
    evaluate_top100,
    select_shadow_sample,
)

_HEADER_ALLOW = {
    "date",
    "retry-after",
    "x-mbx-used-weight",
    "x-mbx-used-weight-1m",
    "x-mbx-used-weight-1s",
}


class SampleFailure(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def run_public_sample(
    *,
    root: Path,
    client: httpx.Client,
    budget: StudyBudget,
    transport_label: str,
) -> dict[str, Any]:
    """One finite round. ``transport_label`` is ``live`` or ``synthetic_transport``."""

    started = datetime.now(UTC).isoformat()
    manifest = {
        "frozen_before_metric_fetches": True,
        "selection_rule": "btc_eth_plus_volume_halves_spread_tiebreak_symbol",
        "rounds": budget.online_rounds,
        "tolerances": {
            "candle_open_match": "exact_timestamp",
            "nearest_neighbor": False,
            "usdt_equals_usd": False,
            "cross_venue_delta_is_error": False,
        },
        "symbols": [],
        "started_at": started,
    }
    _write(root, "manifest.json", manifest)
    calls: list[dict[str, Any]] = []
    partial_reason = None
    listings = None
    global_metrics = None
    contracts: list[VenueContract] = []
    paprika_overlap = {"status": "not_run"}
    try:
        listings_body = _get(
            client,
            budget,
            calls,
            provider="cmc",
            url=f"{KEYLESS_ROOT}{LISTINGS_PATH}",
            params={"start": 1, "limit": 100, "convert": "USD"},
        )
        if listings_body.get("json") is not None and listings_body["status_code"] == 200:
            listings = parse_listings(
                listings_body["json"],
                raw_sha256=listings_body["sha256"],
                synthetic=transport_label != "live",
            )
        global_body = _get(
            client,
            budget,
            calls,
            provider="cmc",
            url=f"{KEYLESS_ROOT}{GLOBAL_METRICS_PATH}",
            params={"convert": "USD"},
        )
        if global_body.get("json") is not None and global_body["status_code"] == 200:
            global_metrics = parse_global_metrics(
                global_body["json"],
                raw_sha256=global_body["sha256"],
                synthetic=transport_label != "live",
            )
        info = _get(
            client,
            budget,
            calls,
            provider="binance",
            url="https://fapi.binance.com/fapi/v1/exchangeInfo",
        )
        tickers = _get(
            client,
            budget,
            calls,
            provider="binance",
            url="https://fapi.binance.com/fapi/v1/ticker/24hr",
        )
        contracts = _contracts(info.get("json"), tickers.get("json"))
        paprika = _get(
            client,
            budget,
            calls,
            provider="coinpaprika",
            url="https://api.coinpaprika.com/v1/tickers",
            params={"quotes": "USD"},
        )
        paprika_overlap = _paprika_overlap(listings, paprika.get("json"))
    except (BudgetExceeded, StudyCancelled) as exc:
        partial_reason = exc.reason
    stage = None
    sample = None
    if listings is not None:
        stage_counts = evaluate_top100(
            listings,
            contracts,
            count_kind=transport_label,
        )
        assert_stage_arithmetic(stage_counts)
        stage = stage_counts.as_dict()
        stage["membership_problems"] = list(membership_problems(listings))
        liquid_rows = _queued_volumes(stage_counts.queued_symbols, contracts)
        sample = select_shadow_sample(liquid_rows)
        manifest["symbols"] = sample["selected"]
        manifest["symbol_rule_applied_at"] = datetime.now(UTC).isoformat()
        _write(root, "manifest.json", manifest)
    comparison = {"status": "not_run"}
    if sample and "BTCUSDT" in sample["selected"] and partial_reason is None:
        try:
            comparison = _btc_cross_venue(client, budget, calls)
        except (BudgetExceeded, StudyCancelled) as exc:
            partial_reason = exc.reason
            comparison = {"status": "partial", "reason": exc.reason}
    summary = {
        "transport_label": transport_label,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "partial_reason": partial_reason,
        "budget": budget.snapshot(),
        "calls": calls,
        "cmc_listings": _listing_summary(listings),
        "cmc_global": None if global_metrics is None else {
            "btc_dominance": global_metrics.btc_dominance,
            "eth_dominance": global_metrics.eth_dominance,
            "total_market_cap_usd": global_metrics.total_market_cap_usd,
            "last_updated": global_metrics.last_updated,
            "envelope_timestamp": global_metrics.envelope_timestamp,
            "credit_count": global_metrics.credit_count,
            "missing_fields": list(global_metrics.missing_fields),
            "sha256": global_metrics.raw_sha256,
            "synthetic": global_metrics.synthetic,
        },
        "top100": stage,
        "sample": sample,
        "coinpaprika_overlap": paprika_overlap,
        "btc_cross_venue": comparison,
        "coinglass": "not_called_no_authorized_no_overage_entitlement",
        "full_universe_acquisition": "not_executed",
        "websocket": "not_executed",
    }
    _write(root, "sample_summary.json", summary)
    return summary


def _btc_cross_venue(client: httpx.Client, budget: StudyBudget, calls: list[dict[str, Any]]) -> dict[str, Any]:
    binance = _get(
        client,
        budget,
        calls,
        provider="binance",
        url="https://fapi.binance.com/fapi/v1/klines",
        params={"symbol": "BTCUSDT", "interval": "15m", "limit": 5},
    )
    bybit = _get(
        client,
        budget,
        calls,
        provider="bybit",
        url="https://api.bybit.com/v5/market/kline",
        params={"category": "linear", "symbol": "BTCUSDT", "interval": "15", "limit": 5},
    )
    left = _binance_opens(binance.get("json"))
    right = _bybit_opens(bybit.get("json"))
    paired, unmatched_left, unmatched_right = _pair(left, right)
    return {
        "status": "observed" if binance.get("json") is not None and bybit.get("json") is not None else "incomplete",
        "comparison_class": "cross_venue",
        "tolerance_ms": 0,
        "paired_opens": paired,
        "unmatched_binance": unmatched_left,
        "unmatched_bybit": unmatched_right,
        "same_venue_vendor_comparison": "not_applicable",
    }


def _get(
    client: httpx.Client,
    budget: StudyBudget,
    calls: list[dict[str, Any]],
    *,
    provider: str,
    url: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempt = 0
    while True:
        budget.ensure_can_attempt(provider)
        started = time.perf_counter()
        truncated = False
        status_code = 0
        headers: dict[str, str] = {}
        body = b""
        error = None
        try:
            with client.stream("GET", url, params=params) as response:
                status_code = response.status_code
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in _HEADER_ALLOW
                }
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > budget.max_response_bytes:
                        truncated = True
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
                nbytes = total
        except httpx.TimeoutException:
            error = "timeout"
            nbytes = 0
        except httpx.TransportError:
            error = "transport"
            nbytes = 0
        latency_ms = int((time.perf_counter() - started) * 1000)
        budget.record_attempt(provider, nbytes)
        record = {
            "provider": provider,
            "url": url,
            "params": params or {},
            "status_code": status_code,
            "latency_ms": latency_ms,
            "bytes": nbytes,
            "truncated": truncated,
            "error": error,
            "headers": headers,
            "attempt": attempt,
            "sha256": sha256_bytes(body) if body else None,
        }
        calls.append(record)
        retryable = error in {"timeout", "transport"} or status_code == 429 or status_code >= 500
        if truncated or not retryable or attempt >= budget.max_retries:
            if status_code in {401, 403}:
                budget.stop_provider(provider)
            break
        delay = _retry_delay(headers.get("Retry-After") or headers.get("retry-after"))
        if not budget.retry_delay_fits(delay):
            budget.stop_provider(provider)
            record["stopped"] = "retry_after_exceeds_deadline"
            break
        if delay:
            time.sleep(delay)
        attempt += 1
    payload: dict[str, Any] = {
        "status_code": status_code,
        "truncated": truncated,
        "sha256": sha256_bytes(body) if body else None,
        "json": None,
        "disposition": "access_blocked" if truncated else http_disposition(status_code) if status_code else "unverified",
    }
    if body and not truncated and error is None:
        try:
            payload["json"] = json.loads(body)
        except json.JSONDecodeError:
            payload["disposition"] = "malformed"
    return payload


def _retry_delay(value: str | None) -> float:
    if value is None or value.strip() == "":
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 0.0


def _contracts(exchange_info: Any, tickers: Any) -> list[VenueContract]:
    volumes: dict[str, str] = {}
    if isinstance(tickers, list):
        for row in tickers:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            quote = row.get("quoteVolume")
            if symbol and quote not in (None, ""):
                volumes[symbol] = str(quote)
    contracts: list[VenueContract] = []
    rows = exchange_info.get("symbols") if isinstance(exchange_info, dict) else None
    if not isinstance(rows, list):
        return contracts
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        base = str(row.get("baseAsset") or "").upper()
        contracts.append(
            VenueContract(
                venue="binance_usdm",
                symbol=symbol,
                base_asset=base,
                quote_asset=str(row.get("quoteAsset") or "").upper(),
                contract_type=str(row.get("contractType") or "").upper(),
                status=str(row.get("status") or "").upper(),
                underlying_type=str(row.get("underlyingType") or "").upper(),
                quote_volume_usdt=volumes.get(symbol),
            )
        )
    return contracts


def _queued_volumes(symbols: tuple[str, ...], contracts: list[VenueContract]) -> list[tuple[str, str]]:
    by_symbol = {
        contract.symbol: contract.quote_volume_usdt
        for contract in contracts
        if contract.quote_volume_usdt is not None
    }
    return [(symbol, by_symbol[symbol]) for symbol in symbols if symbol in by_symbol]


def _paprika_overlap(listings: Any, payload: Any) -> dict[str, Any]:
    if listings is None or not isinstance(payload, list):
        return {"status": "insufficient_evidence"}
    paprika: dict[str, int] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        rank = row.get("rank")
        try:
            rank_int = int(rank)
        except (TypeError, ValueError):
            continue
        if symbol and 1 <= rank_int <= 100 and symbol not in paprika:
            paprika[symbol] = rank_int
    cmc = {
        asset.symbol: asset.rank
        for asset in listings.assets
        if asset.rank is not None and 1 <= asset.rank <= 100
    }
    common = sorted(set(cmc) & set(paprika))
    deltas = [
        {"symbol": symbol, "cmc_rank": cmc[symbol], "coinpaprika_rank": paprika[symbol]}
        for symbol in common
        if cmc[symbol] != paprika[symbol]
    ]
    return {
        "status": "methodology_comparison",
        "cmc_symbols": len(cmc),
        "coinpaprika_symbols": len(paprika),
        "overlap": len(common),
        "rank_disagreements": len(deltas),
        "rank_disagreement_examples": deltas[:5],
        "defect_claimed": False,
    }


def _listing_summary(listings: Any) -> dict[str, Any] | None:
    if listings is None:
        return None
    return {
        "rows": len(listings.assets),
        "envelope_timestamp": listings.envelope_timestamp,
        "credit_count": listings.credit_count,
        "synthetic": listings.synthetic,
        "sha256": listings.raw_sha256,
        "problems": list(membership_problems(listings)),
    }


def _binance_opens(payload: Any) -> list[int]:
    if not isinstance(payload, list):
        return []
    opens = []
    for row in payload:
        if isinstance(row, list) and row:
            opens.append(int(row[0]))
    return opens


def _bybit_opens(payload: Any) -> list[int]:
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    return [int(row[0]) for row in rows if isinstance(row, list) and row]


def _pair(left: list[int], right: list[int]) -> tuple[list[int], list[int], list[int]]:
    right_set = set(right)
    paired = [item for item in left if item in right_set]
    return paired, [item for item in left if item not in right_set], [item for item in right if item not in set(left)]


def _write(root: Path, name: str, payload: dict[str, Any]) -> None:
    path = resolve_output(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def query_string(params: dict[str, Any]) -> str:
    return urlencode(params)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")
