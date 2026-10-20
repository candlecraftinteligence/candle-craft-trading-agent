"""Bounded public sampling. Injected transports stay labeled synthetic."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from research.market_data_foundation.budget import BudgetExceeded, StudyBudget, StudyCancelled
from research.market_data_foundation.cmc import (
    GLOBAL_METRICS_PATH,
    KEYLESS_ROOT,
    LISTINGS_PATH,
    CmcContractError,
    http_disposition,
    listing_domain_problems,
    membership_problems,
    parse_global_metrics,
    parse_listings,
    parse_utc_timestamp,
    temporal_problems,
)
from research.market_data_foundation.constants import (
    EVIDENCE_SCHEMA_VERSION,
    MIN_RETRY_BACKOFF_SECONDS,
    PHASE,
)
from research.market_data_foundation.evidence import Observation, canonical_json, digest, sha256_bytes
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


def run_public_sample(
    *,
    root: Path,
    client: httpx.Client,
    budget: StudyBudget,
    transport_label: str,
    clock: Callable[[], datetime] | None = None,
    code_sha: str = "unspecified",
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """One finite round. ``transport_label`` is ``live`` or ``synthetic_transport``.

    Live sampling does not run unless spare shared-IP capacity is authorized.
    Raw bodies are not retained. Normalized observations are.
    """

    now = clock or (lambda: datetime.now(UTC))
    started = now().isoformat()
    run_id = digest({"phase": PHASE, "started_at": started, "transport": transport_label})[:16]
    policy_digest = digest(
        {
            "phase": PHASE,
            "schema": EVIDENCE_SCHEMA_VERSION,
            "live_sampling_authorized": budget.live_sampling_authorized,
        }
    )
    manifest = {
        "frozen_before_metric_fetches": True,
        "selection_rule": "btc_eth_plus_volume_halves_spread_tiebreak_symbol",
        "rounds": budget.online_rounds,
        "run_id": run_id,
        "code_sha": code_sha,
        "policy_digest": policy_digest,
        "retention": "normalized_observations_only",
        "raw_bodies_retained": False,
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
    if transport_label == "live" and not budget.live_sampling_authorized:
        summary = _empty_summary(
            transport_label=transport_label,
            started=started,
            now=now,
            budget=budget,
            run_id=run_id,
            code_sha=code_sha,
            policy_digest=policy_digest,
            partial_reason="live_sampling_skipped_unknown_shared_ip_capacity",
        )
        _persist(root, summary, observations=[])
        return summary

    calls: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    partial_reason = None
    listings = None
    global_metrics = None
    contracts: list[VenueContract] = []
    paprika_overlap: dict[str, Any] = {"status": "not_run"}
    provider_errors: list[dict[str, str]] = []

    def attempt(provider: str, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        nonlocal partial_reason
        if budget.study_stopped_reason:
            if partial_reason is None:
                partial_reason = budget.study_stopped_reason
            return None
        try:
            return _get(
                client,
                budget,
                calls,
                provider=provider,
                url=url,
                params=params,
                clock=now,
                sleeper=sleeper or time.sleep,
                run_id=run_id,
                code_sha=code_sha,
                policy_digest=policy_digest,
            )
        except BudgetExceeded as exc:
            provider_errors.append({"provider": provider, "reason": exc.reason})
            if exc.reason in {
                "deadline",
                "deadline_during_stream",
                "total_bytes",
                "total_bytes_during_stream",
                "http_attempts",
            } or budget.study_stopped_reason:
                if partial_reason is None:
                    partial_reason = budget.study_stopped_reason or exc.reason
            return None
        except StudyCancelled as exc:
            if partial_reason is None:
                partial_reason = exc.reason
            return None

    listings_body = attempt(
        "cmc",
        f"{KEYLESS_ROOT}{LISTINGS_PATH}",
        {"start": 1, "limit": 100, "convert": "USD"},
    )
    if listings_body and listings_body.get("json") is not None and listings_body["status_code"] == 200:
        try:
            listings = parse_listings(
                listings_body["json"],
                raw_sha256=listings_body["sha256"],
                synthetic=transport_label != "live",
            )
        except CmcContractError as exc:
            provider_errors.append({"provider": "cmc", "reason": exc.reason})
            _annotate_call(calls, disposition="malformed", failure=exc.reason)
    elif listings_body is not None and listings_body.get("disposition") == "malformed":
        provider_errors.append({"provider": "cmc", "reason": str(listings_body.get("failure") or "malformed")})
    elif listings_body is not None and listings_body.get("status_code") not in {0, 200}:
        provider_errors.append({"provider": "cmc", "reason": f"http_{listings_body.get('status_code')}"})
    if listings is not None:
        domain = listing_domain_problems(listings)
        if domain:
            _annotate_call(calls, disposition="malformed", failure=domain[0])
    global_body = attempt("cmc", f"{KEYLESS_ROOT}{GLOBAL_METRICS_PATH}", {"convert": "USD"})
    if global_body and global_body.get("json") is not None and global_body["status_code"] == 200:
        try:
            global_metrics = parse_global_metrics(
                global_body["json"],
                raw_sha256=global_body["sha256"],
                synthetic=transport_label != "live",
            )
        except CmcContractError as exc:
            provider_errors.append({"provider": "cmc", "reason": exc.reason})
            _annotate_call(calls, disposition="malformed", failure=exc.reason)
    if global_metrics is not None:
        native = {
            "btc_dominance": global_metrics.btc_dominance,
            "eth_dominance": global_metrics.eth_dominance,
            "total_market_cap_usd": global_metrics.total_market_cap_usd,
        }
        usable = not global_metrics.domain_problems and not global_metrics.missing_fields
        failure = None
        if global_metrics.domain_problems:
            failure = global_metrics.domain_problems[0]
        elif global_metrics.missing_fields:
            failure = f"missing_{global_metrics.missing_fields[0]}"
        _annotate_call(
            calls,
            disposition="verified" if usable else "malformed",
            failure=failure,
            metrics={
                "native": native,
                "normalized": native if usable else None,
                "unit": "cmc_global_metrics",
                "asset_id": "global",
            },
        )
    info = attempt("binance", "https://fapi.binance.com/fapi/v1/exchangeInfo")
    tickers = attempt("binance", "https://fapi.binance.com/fapi/v1/ticker/24hr")
    if info is not None or tickers is not None:
        contracts = _contracts(
            None if info is None else info.get("json"),
            None if tickers is None else tickers.get("json"),
        )
    paprika = attempt("coinpaprika", "https://api.coinpaprika.com/v1/tickers", {"quotes": "USD"})
    paprika_overlap = _paprika_overlap(listings, None if paprika is None else paprika.get("json"))

    stage = None
    sample = None
    if listings is not None:
        acquired = now()
        stage_counts = evaluate_top100(
            listings,
            contracts,
            acquired_at=acquired,
            decision_cutoff=acquired,
            count_kind=transport_label,
        )
        assert_stage_arithmetic(stage_counts)
        stage = stage_counts.as_dict()
        stage["membership_problems"] = list(membership_problems(listings))
        stage["temporal_problems"] = list(
            temporal_problems(listings, acquired_at=acquired, decision_cutoff=acquired)
        )
        liquid_rows = _queued_volumes(stage_counts.queued_symbols, contracts)
        sample = select_shadow_sample(liquid_rows)
        manifest["symbols"] = sample["selected"]
        manifest["symbol_rule_applied_at"] = now().isoformat()
        _write(root, "manifest.json", manifest)
    comparison: dict[str, Any] = {"status": "not_run", "comparable_observations": 0}
    if sample and "BTCUSDT" in sample.get("selected", []) and partial_reason is None:
        binance_k = attempt(
            "binance",
            "https://fapi.binance.com/fapi/v1/klines",
            {"symbol": "BTCUSDT", "interval": "15m", "limit": 5},
        )
        bybit_k = attempt(
            "bybit",
            "https://api.bybit.com/v5/market/kline",
            {"category": "linear", "symbol": "BTCUSDT", "interval": "15", "limit": 5},
        )
        comparison = _btc_cross_venue(binance_k, bybit_k)
    observations = [_observation_for_call(call, run_id, code_sha, policy_digest) for call in calls]
    summary = {
        "transport_label": transport_label,
        "run_id": run_id,
        "code_sha": code_sha,
        "policy_digest": policy_digest,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "retention": "normalized_observations_only",
        "raw_bodies_retained": False,
        "started_at": started,
        "finished_at": now().isoformat(),
        "partial_reason": partial_reason,
        "provider_errors": provider_errors,
        "budget": budget.snapshot(),
        "calls": calls,
        "cmc_listings": _listing_summary(listings),
        "cmc_global": None
        if global_metrics is None
        else {
            "btc_dominance": global_metrics.btc_dominance,
            "eth_dominance": global_metrics.eth_dominance,
            "total_market_cap_usd": global_metrics.total_market_cap_usd,
            "last_updated": global_metrics.last_updated,
            "envelope_timestamp": global_metrics.envelope_timestamp,
            "credit_count": global_metrics.credit_count,
            "missing_fields": list(global_metrics.missing_fields),
            "domain_problems": list(global_metrics.domain_problems),
            "sha256": global_metrics.raw_sha256,
            "synthetic": global_metrics.synthetic,
        },
        "top100": stage,
        "sample": sample,
        "coinpaprika_overlap": paprika_overlap,
        "btc_cross_venue": comparison,
        "coinglass": "not_called_no_authorized_no_overage_entitlement",
        "metered_access": "disabled_no_verified_no_overage_allowance",
        "full_universe_acquisition": "not_executed",
        "websocket": "not_executed",
        "identity_note": "no CMC-id crosswalk was supplied; ticker matches are not identity-validated",
    }
    if partial_reason is None and budget.study_stopped_reason:
        summary["partial_reason"] = budget.study_stopped_reason
    _persist(root, summary, observations)
    return summary


def replay_saved(root: Path, *, decision_cutoff: datetime) -> dict[str, Any]:
    """Replay normalized observations that were valid and available at the cutoff.

    A fitting timestamp does not admit a failed, missing, or schema-invalid row.
    Raw bodies are not part of the artifact.
    """

    from research.market_data_foundation.timeutil import originally_available

    empty = {"admitted": [], "inadmissible": [], "reproduced": [], "raw_bodies_retained": False}
    path = resolve_output(root, "observations.json")
    if not path.exists():
        return {"status": "missing", "reason": "observations_artifact_absent", **empty}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"status": "malformed", "reason": "invalid_schema", **empty}
    if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION or payload.get("raw_bodies_retained") is not False:
        return {"status": "malformed", "reason": "invalid_schema", **empty}
    body = payload.get("observations")
    if not isinstance(body, list) or payload.get("checksum") != digest(body):
        reason = "checksum_mismatch" if isinstance(body, list) else "invalid_schema"
        return {"status": "malformed", "reason": reason, **empty}
    admitted = []
    inadmissible = []
    for item in body:
        if not _observation_schema_ok(item):
            inadmissible.append({"evidence_id": None, "availability": "invalid_schema"})
            continue
        stamps = item.get("timestamps") or {}
        measurement = parse_utc_timestamp(stamps.get("measurement_at"))
        available = parse_utc_timestamp(stamps.get("received_at"))
        availability = originally_available(
            measurement_at=measurement,
            available_to_cci_at=available,
            decision_cutoff=decision_cutoff,
        )
        row = {
            "evidence_id": item.get("evidence_id"),
            "availability": availability,
            "validity": item.get("validity"),
            "asset_id": item.get("asset_id"),
            "normalized_unit": item.get("normalized_unit"),
            "normalized_value": item.get("normalized_value"),
        }
        if item.get("validity") != "verified" or item.get("normalized_value") in (None, ""):
            row["availability"] = str(item.get("validity") or "missing")
            inadmissible.append(row)
            continue
        if availability != "verified":
            inadmissible.append(row)
            continue
        admitted.append(row)
    return {
        "status": "replayed",
        "decision_cutoff": decision_cutoff.isoformat(),
        "admitted": admitted,
        "inadmissible": inadmissible,
        "reproduced": _reproduced_metrics(admitted),
        "raw_bodies_retained": False,
    }


def _empty_summary(**kwargs: Any) -> dict[str, Any]:
    return {
        "transport_label": kwargs["transport_label"],
        "run_id": kwargs["run_id"],
        "code_sha": kwargs["code_sha"],
        "policy_digest": kwargs["policy_digest"],
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "retention": "normalized_observations_only",
        "raw_bodies_retained": False,
        "started_at": kwargs["started"],
        "finished_at": kwargs["now"]().isoformat(),
        "partial_reason": kwargs["partial_reason"],
        "provider_errors": [],
        "budget": kwargs["budget"].snapshot(),
        "calls": [],
        "cmc_listings": None,
        "cmc_global": None,
        "top100": None,
        "sample": None,
        "coinpaprika_overlap": {"status": "not_run"},
        "btc_cross_venue": {"status": "not_run", "comparable_observations": 0},
        "coinglass": "not_called_no_authorized_no_overage_entitlement",
        "metered_access": "disabled_no_verified_no_overage_allowance",
        "full_universe_acquisition": "not_executed",
        "websocket": "not_executed",
    }


def _btc_cross_venue(binance: dict[str, Any] | None, bybit: dict[str, Any] | None) -> dict[str, Any]:
    left_status, left = _binance_rows(binance)
    right_status, right = _bybit_rows(bybit)
    if left_status != "usable" or right_status != "usable":
        return {
            "status": "not_observed",
            "reason": {"binance": left_status, "bybit": right_status},
            "comparison_class": "cross_venue",
            "tolerance_ms": 0,
            "paired_opens": [],
            "comparable_observations": 0,
            "same_venue_vendor_comparison": "not_applicable",
        }
    paired, unmatched_left, unmatched_right = _pair([row[0] for row in left], [row[0] for row in right])
    return {
        "status": "observed",
        "comparison_class": "cross_venue",
        "tolerance_ms": 0,
        "paired_opens": paired,
        "unmatched_binance": unmatched_left,
        "unmatched_bybit": unmatched_right,
        "comparable_observations": len(left) + len(right),
        "same_venue_vendor_comparison": "not_applicable",
        "price_compared": False,
        "label": "open_timestamps_only",
    }


def _get(
    client: httpx.Client,
    budget: StudyBudget,
    calls: list[dict[str, Any]],
    *,
    provider: str,
    url: str,
    params: dict[str, Any] | None = None,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    run_id: str,
    code_sha: str,
    policy_digest: str,
) -> dict[str, Any]:
    attempt = 0
    while True:
        budget.ensure_can_attempt(provider)
        requested_at = clock().isoformat()
        started = time.perf_counter()
        truncated = False
        status_code = 0
        headers: dict[str, str] = {}
        body = b""
        error = None
        stop_reason = None
        received_bytes = 0
        study_stop = None
        try:
            with client.stream("GET", url, params=params) as response:
                status_code = response.status_code
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in _HEADER_ALLOW
                }
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    received_bytes += len(chunk)
                    budget.ensure_during_stream(received_bytes)
                    if received_bytes > budget.max_response_bytes:
                        truncated = True
                        stop_reason = "response_bytes"
                        budget.stop_provider(provider, reason=stop_reason, attempt=attempt)
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
        except BudgetExceeded as exc:
            error = exc.reason
            stop_reason = exc.reason
            if exc.reason in {"total_bytes", "total_bytes_during_stream", "deadline", "deadline_during_stream"}:
                study_stop = exc.reason
            else:
                budget.stop_provider(provider, reason=exc.reason, attempt=attempt)
        except StudyCancelled as exc:
            error = exc.reason
            stop_reason = exc.reason
            study_stop = exc.reason
        except httpx.TimeoutException:
            error = "timeout"
        except httpx.TransportError:
            error = "transport"
        nbytes = received_bytes
        received_at = clock().isoformat()
        latency_ms = int((time.perf_counter() - started) * 1000)
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
            "requested_at": requested_at,
            "received_at": received_at,
            "run_id": run_id,
            "code_sha": code_sha,
            "policy_digest": policy_digest,
            "stopped_reason": stop_reason,
        }
        calls.append(record)
        try:
            budget.record_attempt(provider, nbytes)
        except BudgetExceeded as exc:
            study_stop = study_stop or exc.reason
            record["error"] = record["error"] or exc.reason
            record["stopped_reason"] = record["stopped_reason"] or exc.reason
        if study_stop:
            budget.mark_study_stopped(study_stop, provider=provider, attempt=attempt)
            record["stopped_reason"] = study_stop
        if study_stop or truncated or stop_reason:
            if status_code in {401, 403}:
                budget.stop_provider(provider, reason=f"http_{status_code}", attempt=attempt)
                record["stopped_reason"] = f"http_{status_code}"
            break
        retryable = error in {"timeout", "transport"} or status_code == 429 or status_code >= 500
        if not retryable or attempt >= budget.max_retries:
            if status_code in {401, 403, 429}:
                budget.stop_provider(provider, reason=f"http_{status_code}", attempt=attempt)
                record["stopped_reason"] = f"http_{status_code}"
            break
        delay = _retry_delay(headers.get("Retry-After") or headers.get("retry-after"), now=clock())
        if delay is None or not budget.retry_delay_fits(delay):
            budget.stop_provider(provider, reason="retry_after_exceeds_deadline", attempt=attempt)
            record["stopped_reason"] = "retry_after_exceeds_deadline"
            break
        if delay:
            sleeper(delay)
        attempt += 1
    disposition, failure, decoded = _decode_body(
        body,
        status_code=status_code,
        truncated=truncated,
        error=calls[-1]["error"],
        stop_reason=calls[-1]["stopped_reason"],
    )
    calls[-1]["disposition"] = disposition
    calls[-1]["failure"] = failure
    calls[-1]["source_timestamp"] = _measurement_timestamp(decoded)
    calls[-1]["api_error"] = failure == "provider_api_error"
    return {
        "status_code": status_code,
        "truncated": truncated,
        "sha256": calls[-1]["sha256"],
        "json": decoded,
        "disposition": disposition,
        "failure": failure,
        "requested_at": calls[-1]["requested_at"],
        "received_at": calls[-1]["received_at"],
        "stopped_reason": calls[-1]["stopped_reason"],
    }


def _decode_body(
    body: bytes,
    *,
    status_code: int,
    truncated: bool,
    error: str | None,
    stop_reason: str | None,
) -> tuple[str, str | None, Any]:
    """One disposition for transport, decoding, and the provider envelope.

    HTTP 200 is not verified evidence by itself.
    """

    if status_code in {401, 403, 429}:
        return "access_blocked", f"http_{status_code}", None
    if truncated or error or stop_reason:
        reason = error or stop_reason or "truncated"
        kind = "unverified" if reason in {"timeout", "transport", "deadline", "deadline_during_stream"} else "malformed"
        return kind, reason, None
    if not body:
        if status_code == 200:
            return "malformed", "empty_body", None
        if status_code == 0:
            return "unverified", "no_response", None
        return http_disposition(status_code), f"http_{status_code}", None
    try:
        decoded = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError:
        return "malformed", "invalid_encoding", None
    except json.JSONDecodeError:
        return "malformed", "malformed_json", None
    if _provider_api_error(decoded):
        return "malformed", "provider_api_error", decoded
    if status_code != 200:
        return http_disposition(status_code), f"http_{status_code}", decoded
    return "verified", None, decoded


def _observation_schema_ok(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    required = {
        "evidence_id",
        "validity",
        "timestamps",
        "normalized_value",
        "normalized_unit",
        "code_sha",
        "policy_digest",
        "provider",
    }
    if not required <= set(item):
        return False
    return isinstance(item.get("timestamps"), dict)


def _reproduced_metrics(admitted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reproduced: list[dict[str, Any]] = []
    for item in admitted:
        if item.get("normalized_unit") != "cmc_global_metrics":
            continue
        parsed = json.loads(str(item.get("normalized_value")))
        if isinstance(parsed, dict):
            reproduced.append(parsed)
    return reproduced


def _annotate_call(
    calls: list[dict[str, Any]],
    *,
    disposition: str,
    failure: str | None,
    metrics: dict[str, Any] | None = None,
) -> None:
    if not calls:
        return
    record = calls[-1]
    current = record.get("disposition")
    if current not in {None, "verified", "unverified"} and disposition == "verified":
        return
    record["disposition"] = disposition
    record["failure"] = failure
    if metrics is not None:
        record["normalized_metrics"] = metrics


def _measurement_timestamp(body: Any) -> str | None:
    """Copy a parsed source time. Response receipt is not a measurement time."""

    if not isinstance(body, dict):
        return None
    data = body.get("data")
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.append(data.get("last_updated"))
        quote = data.get("quote")
        usd = quote.get("USD") if isinstance(quote, dict) else None
        if isinstance(usd, dict):
            candidates.append(usd.get("last_updated"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        moment = parse_utc_timestamp(candidate)
        if moment is not None:
            return moment.isoformat()
    return None


def _provider_api_error(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    status = body.get("status")
    if isinstance(status, dict) and "error_code" in status and str(status.get("error_code")) not in {"0"}:
        return True
    if "retCode" in body and body.get("retCode") not in {0, "0"}:
        return True
    return False


def _retry_delay(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse delta-seconds and HTTP-date. A missing header is a bounded wait, not zero."""

    moment = now or datetime.now(UTC)
    if value is None or value.strip() == "":
        return MIN_RETRY_BACKOFF_SECONDS
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = (parsed - moment).total_seconds()
    if seconds < 0:
        seconds = 0.0
    return seconds


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
        multiplier = row.get("multiplier") or row.get("contractSize") or "1"
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
                settlement_asset=str(row.get("marginAsset") or row.get("quoteAsset") or "").upper(),
                multiplier=str(multiplier),
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
        return {"status": "insufficient_evidence", "identity_basis": "symbol_only_unverified", "validated_identity_overlap": 0}
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
        "status": "symbol_only_methodology_comparison",
        "identity_basis": "symbol_only_unverified",
        "validated_identity_overlap": 0,
        "cmc_symbols": len(cmc),
        "coinpaprika_symbols": len(paprika),
        "symbol_overlap": len(common),
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


def _binance_rows(payload: dict[str, Any] | None) -> tuple[str, list[tuple[int, str, str]]]:
    if payload is None:
        return "missing", []
    if payload.get("disposition") != "verified" or payload.get("status_code") != 200:
        return "transport", []
    body = payload.get("json")
    if isinstance(body, dict):
        return "api_error", []
    if not isinstance(body, list) or not body:
        return "empty", []
    rows: list[tuple[int, str, str]] = []
    for row in body:
        if not isinstance(row, list) or len(row) < 6:
            return "malformed", []
        try:
            rows.append((int(row[0]), str(row[4]), str(row[5])))
        except (TypeError, ValueError):
            return "malformed", []
    return "usable", rows


def _bybit_rows(payload: dict[str, Any] | None) -> tuple[str, list[tuple[int, str, str]]]:
    if payload is None:
        return "missing", []
    if payload.get("disposition") != "verified" or payload.get("status_code") != 200:
        return "transport", []
    body = payload.get("json")
    if not isinstance(body, dict):
        return "malformed", []
    if body.get("retCode") != 0:
        return "bybit_retCode", []
    result = body.get("result")
    if not isinstance(result, dict):
        return "malformed", []
    symbol = str(result.get("symbol") or "BTCUSDT").upper()
    if symbol != "BTCUSDT":
        return "instrument_mismatch", []
    listed = result.get("list")
    if not isinstance(listed, list) or not listed:
        return "empty", []
    rows: list[tuple[int, str, str]] = []
    for row in listed:
        if not isinstance(row, list) or len(row) < 6:
            return "malformed", []
        try:
            rows.append((int(row[0]), str(row[4]), str(row[5])))
        except (TypeError, ValueError):
            return "malformed", []
    return "usable", rows


def _pair(left: list[int], right: list[int]) -> tuple[list[int], list[int], list[int]]:
    right_set = set(right)
    paired = [item for item in left if item in right_set]
    return paired, [item for item in left if item not in right_set], [item for item in right if item not in set(left)]


def _observation_for_call(
    call: dict[str, Any],
    run_id: str,
    code_sha: str,
    policy_digest: str,
) -> dict[str, Any]:
    validity = str(call.get("disposition") or "unverified")
    if validity not in {
        "verified",
        "missing",
        "stale",
        "malformed",
        "incompatible",
        "unverified",
        "unsupported",
        "access_blocked",
        "not_applicable",
    }:
        validity = "unverified"
    metrics = call.get("normalized_metrics") if isinstance(call.get("normalized_metrics"), dict) else None
    native_value = None
    normalized_value = None
    normalized_unit = "response"
    asset_id = "unmapped"
    if metrics is not None:
        if metrics.get("native") is not None:
            native_value = canonical_json(metrics.get("native"))
        if metrics.get("normalized") is not None:
            normalized_value = canonical_json(metrics.get("normalized"))
            normalized_unit = str(metrics.get("unit") or "unspecified")
            asset_id = str(metrics.get("asset_id") or "unmapped")
    if validity == "verified" and normalized_value is None:
        validity = "unverified"
    if call.get("status_code") in {401, 403, 429} and validity == "verified":
        validity = "access_blocked"
    observation = Observation(
        evidence_id=digest({"url": call["url"], "attempt": call["attempt"], "sha": call.get("sha256")})[:24],
        run_id=run_id,
        code_sha=code_sha,
        policy_digest=policy_digest,
        provider=str(call["provider"]),
        endpoint=str(call["url"]),
        request_id=str(call["attempt"]),
        parameters=dict(call.get("params") or {}),
        venue=str(call["provider"]),
        instrument_id=str((call.get("params") or {}).get("symbol") or "universe"),
        asset_id=asset_id,
        native_value=native_value,
        native_unit=normalized_unit if native_value is not None else "response",
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
        timestamps={
            "requested_at": call.get("requested_at"),
            "received_at": call.get("received_at"),
            "measurement_at": call.get("source_timestamp"),
        },
        interval_semantics="request",
        cache_status="not_applicable",
        lineage="direct_http",
        response_status=str(call.get("status_code")),
        latency_ms=call.get("latency_ms"),
        retry_count=int(call.get("attempt") or 0),
        request_weight=(call.get("headers") or {}).get("x-mbx-used-weight-1m"),
        raw_sha256=call.get("sha256"),
        validity=validity,
        reason_code=str(call.get("failure") or call.get("stopped_reason") or call.get("error") or "recorded"),
    )
    return observation.as_dict()


def _persist(root: Path, summary: dict[str, Any], observations: list[dict[str, Any]]) -> None:
    checksum = digest(observations)
    artifact = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "retention": "normalized_observations_only",
        "raw_bodies_retained": False,
        "checksum": checksum,
        "observations": observations,
    }
    _write(root, "observations.json", artifact)
    _write(
        root,
        "replay_index.json",
        {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "checksum": checksum,
            "observation_count": len(observations),
            "raw_bodies_retained": False,
            "retention_limit": "normalized records only; raw provider bodies are not stored",
        },
    )
    summary["observation_checksum"] = checksum
    summary["observation_count"] = len(observations)
    _write(root, "sample_summary.json", summary)


def _write(root: Path, name: str, payload: dict[str, Any]) -> None:
    path = resolve_output(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def query_string(params: dict[str, Any]) -> str:
    return urlencode(params)
