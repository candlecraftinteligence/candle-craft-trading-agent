"""Single-instrument metadata for interpreting existing derivatives observations.

A missing or conflicting lookup stays Unverified. Lot size, minimum quantity, and
quantity precision are never treated as a contract multiplier. Digits in a symbol
are never a multiplier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.derivatives_source_evidence import (
    COUNT_BOTH_SIDES,
    COUNT_OPEN_CONTRACT,
    COUNT_SINGLE_SIDE,
    ENDPOINT_BINANCE_EXCHANGE_INFO,
    ENDPOINT_BYBIT_INSTRUMENT,
    MULTIPLIER_NOT_APPLICABLE,
    MULTIPLIER_UNVERIFIED,
    QUALITY_CONFLICT,
    QUALITY_MISSING,
    QUALITY_UNVERIFIED,
    QUALITY_VERIFIED,
    UNIT_BASE_QUANTITY,
    UNIT_QUOTE_NOTIONAL,
    UNIT_UNKNOWN,
    AcquisitionFact,
    DerivativesEvidenceBatch,
    DerivativesObservation,
    QuantityFact,
)
from app.data.dtos import NA

_BINANCE_DATED = frozenset({"CURRENT_QUARTER", "NEXT_QUARTER", "CURRENT_MONTH", "NEXT_MONTH"})
_BINANCE_PERPETUAL = frozenset({"PERPETUAL", "PERPETUAL_DELIVERING"})
_BYBIT_PERPETUAL = frozenset({"LinearPerpetual"})
_BYBIT_DATED = frozenset({"LinearFutures"})


@dataclass(frozen=True)
class InstrumentMetadata:
    exchange: str
    requested_symbol: str
    symbol: str | None
    market_category: str
    contract_type: str
    expiry: str
    base_asset: str
    quote_asset: str
    settlement_asset: str
    funding_interval_ms: int | None
    multiplier_applicability: str
    inverse: bool | None
    quality: str
    reason_codes: tuple[str, ...]
    source_endpoint: str
    acquisition: AcquisitionFact


def unverified_instrument(
    *,
    exchange: str,
    requested_symbol: str,
    source_endpoint: str,
    reason: str,
) -> InstrumentMetadata:
    return InstrumentMetadata(
        exchange=exchange,
        requested_symbol=requested_symbol.strip().upper(),
        symbol=None,
        market_category=NA,
        contract_type=NA,
        expiry=NA,
        base_asset=NA,
        quote_asset=NA,
        settlement_asset=NA,
        funding_interval_ms=None,
        multiplier_applicability=MULTIPLIER_UNVERIFIED,
        inverse=None,
        quality=QUALITY_UNVERIFIED,
        reason_codes=(reason,),
        source_endpoint=source_endpoint,
        acquisition=AcquisitionFact(
            acquired_at=None,
            meaning="successful_adapter_response_boundary",
            quality=QUALITY_MISSING,
            reason_codes=("missing_acquisition_timestamp", reason),
        ),
    )


def parse_binance_usds_instrument(
    payload: Any,
    *,
    requested_symbol: str,
    acquisition: AcquisitionFact,
) -> InstrumentMetadata:
    requested = requested_symbol.strip().upper()
    symbols = _binance_symbols(payload)
    matches = [item for item in symbols if str(item.get("symbol", "")).upper() == requested]
    if len(matches) != 1:
        reason = "instrument_not_found" if not matches else "instrument_identity_conflict"
        return replace(
            unverified_instrument(
                exchange="binance_futures",
                requested_symbol=requested,
                source_endpoint=ENDPOINT_BINANCE_EXCHANGE_INFO,
                reason=reason,
            ),
            acquisition=acquisition,
            quality=QUALITY_CONFLICT if len(matches) > 1 else QUALITY_UNVERIFIED,
        )
    row = matches[0]
    base = _text(row.get("baseAsset"))
    quote = _text(row.get("quoteAsset"))
    margin = _text(row.get("marginAsset"))
    contract_type_raw = _text(row.get("contractType"))
    reasons: list[str] = []
    inverse = margin not in (NA, quote)
    if inverse:
        reasons.append("unsupported_inverse")
    multiplier = _binance_multiplier(row)
    if multiplier == MULTIPLIER_UNVERIFIED:
        reasons.append("multiplier_unverified")
    if contract_type_raw in _BINANCE_PERPETUAL:
        contract_type = "perpetual"
        expiry = "none"
    elif contract_type_raw in _BINANCE_DATED:
        contract_type = "dated"
        expiry = _expiry_text(row.get("deliveryDate"))
        if expiry == NA:
            reasons.append("missing_expiry")
    else:
        contract_type = NA
        expiry = NA
        reasons.append("unknown_contract_type")
    if base == NA or quote == NA or margin == NA:
        reasons.append("incomplete_instrument_identity")
    funding_ms = _hours_to_ms(row.get("fundingIntervalHours"))
    if funding_ms is None and "fundingIntervalHours" in row:
        reasons.append("malformed_funding_cadence")
    quality = QUALITY_VERIFIED
    if reasons or multiplier != MULTIPLIER_NOT_APPLICABLE:
        quality = QUALITY_UNVERIFIED
    if inverse:
        quality = QUALITY_UNVERIFIED
    return InstrumentMetadata(
        exchange="binance_futures",
        requested_symbol=requested,
        symbol=requested,
        market_category="usds_margined",
        contract_type=contract_type,
        expiry=expiry,
        base_asset=base,
        quote_asset=quote,
        settlement_asset=margin,
        funding_interval_ms=funding_ms,
        multiplier_applicability=multiplier,
        inverse=inverse,
        quality=quality,
        reason_codes=tuple(dict.fromkeys(reasons)),
        source_endpoint=ENDPOINT_BINANCE_EXCHANGE_INFO,
        acquisition=acquisition,
    )


def parse_bybit_linear_instrument(
    payload: Any,
    *,
    requested_symbol: str,
    acquisition: AcquisitionFact,
) -> InstrumentMetadata:
    requested = requested_symbol.strip().upper()
    envelope = payload if isinstance(payload, Mapping) else {}
    result = envelope.get("result") if isinstance(envelope.get("result"), Mapping) else {}
    category = _text(result.get("category")).lower()
    rows = result.get("list") if isinstance(result.get("list"), list) else []
    matches = [item for item in rows if isinstance(item, Mapping) and str(item.get("symbol", "")).upper() == requested]
    if category not in {NA, "linear"}:
        return replace(
            unverified_instrument(
                exchange="bybit_linear",
                requested_symbol=requested,
                source_endpoint=ENDPOINT_BYBIT_INSTRUMENT,
                reason="category_mismatch",
            ),
            acquisition=acquisition,
            market_category=category,
            quality=QUALITY_CONFLICT,
        )
    if len(matches) != 1:
        reason = "instrument_not_found" if not matches else "instrument_identity_conflict"
        return replace(
            unverified_instrument(
                exchange="bybit_linear",
                requested_symbol=requested,
                source_endpoint=ENDPOINT_BYBIT_INSTRUMENT,
                reason=reason,
            ),
            acquisition=acquisition,
            quality=QUALITY_CONFLICT if len(matches) > 1 else QUALITY_UNVERIFIED,
        )
    row = matches[0]
    contract_raw = _text(row.get("contractType"))
    reasons: list[str] = []
    inverse = contract_raw.lower().startswith("inverse")
    if inverse:
        reasons.append("unsupported_inverse")
    if contract_raw in _BYBIT_PERPETUAL:
        contract_type = "perpetual"
        expiry = "none" if str(row.get("deliveryTime", "0")) in {"0", "0.0", ""} else _expiry_text(row.get("deliveryTime"))
    elif contract_raw in _BYBIT_DATED:
        contract_type = "dated"
        expiry = _expiry_text(row.get("deliveryTime"))
        if expiry == NA:
            reasons.append("missing_expiry")
    else:
        contract_type = NA
        expiry = NA
        reasons.append("unknown_contract_type")
    base = _text(row.get("baseCoin"))
    quote = _text(row.get("quoteCoin"))
    settle = _text(row.get("settleCoin"))
    if base == NA or quote == NA or settle == NA:
        reasons.append("incomplete_instrument_identity")
    if settle not in {NA, quote}:
        reasons.append("settlement_asset_mismatch")
    funding_ms = _minutes_to_ms(row.get("fundingInterval"))
    if funding_ms is None and "fundingInterval" in row and row.get("fundingInterval") not in (None, ""):
        reasons.append("malformed_funding_cadence")
    multiplier = MULTIPLIER_NOT_APPLICABLE
    if any(key in row for key in ("contractMultiplier", "contractSize")):
        multiplier = MULTIPLIER_UNVERIFIED
        reasons.append("multiplier_unverified")
    quality = QUALITY_UNVERIFIED if reasons or inverse or multiplier != MULTIPLIER_NOT_APPLICABLE else QUALITY_VERIFIED
    return InstrumentMetadata(
        exchange="bybit_linear",
        requested_symbol=requested,
        symbol=requested,
        market_category="linear",
        contract_type=contract_type,
        expiry=expiry,
        base_asset=base,
        quote_asset=quote,
        settlement_asset=settle,
        funding_interval_ms=funding_ms,
        multiplier_applicability=multiplier,
        inverse=inverse,
        quality=quality,
        reason_codes=tuple(dict.fromkeys(reasons)),
        source_endpoint=ENDPOINT_BYBIT_INSTRUMENT,
        acquisition=acquisition,
    )


def bind_instrument_batch(
    batch: DerivativesEvidenceBatch | None,
    instrument: InstrumentMetadata | None,
) -> DerivativesEvidenceBatch | None:
    if batch is None:
        return None
    observations = tuple(bind_instrument_observation(item, instrument) for item in batch.observations)
    qualities = {item.semantic_quality for item in observations}
    if QUALITY_CONFLICT in qualities:
        quality = QUALITY_CONFLICT
    elif not observations:
        quality = QUALITY_MISSING
    elif qualities == {QUALITY_VERIFIED}:
        quality = QUALITY_VERIFIED
    else:
        quality = QUALITY_UNVERIFIED
    reasons = tuple(dict.fromkeys((*batch.reason_codes, *(code for item in observations for code in item.reason_codes))))
    return replace(batch, observations=observations, semantic_quality=quality, reason_codes=reasons)


def bind_instrument_observation(
    observation: DerivativesObservation,
    instrument: InstrumentMetadata | None,
) -> DerivativesObservation:
    reasons = list(observation.reason_codes)
    if observation.metric != "open_interest":
        if instrument is not None and instrument.quality == QUALITY_VERIFIED:
            if instrument.exchange != observation.exchange:
                reasons.append("venue_mismatch")
                return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_CONFLICT)
            if observation.identity.returned_symbol not in (None, instrument.symbol):
                reasons.append("symbol_conflict")
                return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_CONFLICT)
        return observation
    if instrument is None or instrument.quality != QUALITY_VERIFIED:
        reasons.append("unit_unverified")
        if instrument is not None:
            reasons.extend(instrument.reason_codes)
        return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_UNVERIFIED)
    if instrument.exchange != observation.exchange:
        reasons.append("venue_mismatch")
        return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_CONFLICT)
    if instrument.symbol != observation.identity.requested_symbol:
        reasons.append("symbol_conflict")
        return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_CONFLICT)
    if observation.identity.returned_symbol not in (None, instrument.symbol):
        reasons.append("symbol_conflict")
        return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_CONFLICT)
    if instrument.inverse is True:
        reasons.append("unsupported_inverse")
        return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_UNVERIFIED)
    if instrument.multiplier_applicability != MULTIPLIER_NOT_APPLICABLE:
        reasons.append("multiplier_unverified")
        return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_UNVERIFIED)
    if instrument.market_category != observation.identity.market_category and observation.identity.category_provenance == "response":
        reasons.append("category_mismatch")
        return _retarget(observation, observation.quantities, tuple(dict.fromkeys(reasons)), QUALITY_CONFLICT)
    upgraded: list[QuantityFact] = []
    for quantity in observation.quantities:
        if quantity.counting_convention in {COUNT_OPEN_CONTRACT, COUNT_BOTH_SIDES, COUNT_SINGLE_SIDE} and quantity.unit == UNIT_UNKNOWN:
            next_reasons = tuple(code for code in quantity.reason_codes if code != "unit_unknown")
            quality = QUALITY_VERIFIED if quantity.numeric_value is not None and not next_reasons else quantity.quality
            if quantity.numeric_value is None:
                quality = quantity.quality
            upgraded.append(
                replace(
                    quantity,
                    unit=UNIT_BASE_QUANTITY,
                    unit_asset=instrument.base_asset,
                    multiplier=NA,
                    multiplier_applicability=MULTIPLIER_NOT_APPLICABLE,
                    quality=quality,
                    reason_codes=next_reasons,
                )
            )
        elif quantity.unit == UNIT_QUOTE_NOTIONAL:
            upgraded.append(
                replace(
                    quantity,
                    unit_asset=instrument.quote_asset,
                    multiplier_applicability=MULTIPLIER_NOT_APPLICABLE,
                )
            )
        else:
            upgraded.append(quantity)
    semantic = observation.semantic_quality
    primary = _primary_quantity(tuple(upgraded))
    if primary is None or primary.quality != QUALITY_VERIFIED or primary.unit == UNIT_UNKNOWN:
        semantic = QUALITY_UNVERIFIED
        reasons.append("unit_unverified")
    elif observation.metric_time.quality != QUALITY_VERIFIED:
        semantic = QUALITY_UNVERIFIED
    elif "symbol_conflict" in observation.identity.reason_codes or "category_mismatch" in observation.identity.reason_codes:
        semantic = QUALITY_CONFLICT
    else:
        semantic = QUALITY_VERIFIED
        reasons = [code for code in reasons if code != "unit_unknown"]
    return _retarget(observation, tuple(upgraded), tuple(dict.fromkeys(reasons)), semantic)


def _primary_quantity(quantities: tuple[QuantityFact, ...]) -> QuantityFact | None:
    for quantity in quantities:
        if quantity.unit != UNIT_QUOTE_NOTIONAL and quantity.counting_convention != "not_applicable":
            return quantity
    return None


def _retarget(
    observation: DerivativesObservation,
    quantities: tuple[QuantityFact, ...],
    reasons: tuple[str, ...],
    semantic: str,
) -> DerivativesObservation:
    return replace(observation, quantities=quantities, reason_codes=reasons, semantic_quality=semantic)


def _binance_symbols(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping) and isinstance(payload.get("symbols"), list):
        return [item for item in payload["symbols"] if isinstance(item, Mapping)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


def _binance_multiplier(row: Mapping[str, Any]) -> str:
    for key in ("contractSize", "contractMultiplier"):
        if key not in row or row.get(key) in (None, ""):
            continue
        try:
            value = Decimal(str(row.get(key)))
        except (InvalidOperation, ValueError):
            return MULTIPLIER_UNVERIFIED
        if value != 1:
            return MULTIPLIER_UNVERIFIED
    return MULTIPLIER_NOT_APPLICABLE


def _hours_to_ms(value: Any) -> int | None:
    if value in (None, "", NA):
        return None
    if isinstance(value, bool):
        return None
    try:
        hours = int(str(value))
    except ValueError:
        return None
    if hours <= 0:
        return None
    return hours * 60 * 60 * 1000


def _minutes_to_ms(value: Any) -> int | None:
    if value in (None, "", NA):
        return None
    if isinstance(value, bool):
        return None
    try:
        minutes = int(str(value))
    except ValueError:
        return None
    if minutes <= 0:
        return None
    return minutes * 60 * 1000


def _expiry_text(value: Any) -> str:
    if isinstance(value, bool) or value in (None, "", 0, "0"):
        return NA
    try:
        millis = int(str(value))
    except ValueError:
        return NA
    if millis <= 0:
        return NA
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return NA


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return NA
    cleaned = value.strip()
    return cleaned if cleaned else NA


__all__ = [
    "InstrumentMetadata",
    "bind_instrument_batch",
    "parse_binance_usds_instrument",
    "parse_bybit_linear_instrument",
    "unverified_instrument",
]
