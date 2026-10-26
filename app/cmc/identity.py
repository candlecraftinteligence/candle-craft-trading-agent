"""Explicit asset bindings plus independent exchange metadata, never ticker-only joins."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.cmc.models import EvidenceState, Frozen, Instrument, stable_id, number


class AssetBinding(Frozen):
    cmc_id: int = Field(gt=0)
    cmc_slug: str
    binance_base: str
    quote_id: int = Field(default=825, gt=0)
    approved: bool = False
    evidence_ref: str
    approved_at: datetime


class MappingResult(Frozen):
    state: EvidenceState
    reason: str
    instrument: Instrument | None = None


def match_instrument(pair: dict, exchange: dict, metadata: dict, binding: AssetBinding,
                     asset: dict, *, as_of: datetime, metadata_at: datetime,
                     acquired_at: datetime, map_acquired_at: datetime, exchange_registry: dict,
                     exchange_registry_acquired_at: datetime) -> MappingResult:
    def reject(reason, state=EvidenceState.AMBIGUOUS):
        return MappingResult(state=state, reason=reason)

    times = (binding.approved_at, metadata_at, acquired_at, as_of, map_acquired_at,exchange_registry_acquired_at)
    if any(value.tzinfo is None for value in times):
        return reject("timezone required", EvidenceState.UNVERIFIED)
    if not binding.approved or not binding.evidence_ref or binding.approved_at > as_of:
        return reject("explicit reviewed asset binding required", EvidenceState.UNVERIFIED)
    if any(t > as_of for t in times) or metadata_at > acquired_at:
        return reject("future identity evidence", EvidenceState.UNVERIFIED)
    if (as_of - metadata_at).total_seconds() > 86400:
        return reject("exchange metadata expired", EvidenceState.STALE)
    base, quote = pair.get("market_pair_base", {}), pair.get("market_pair_quote", {})
    if asset.get("id") != binding.cmc_id or asset.get("slug") != binding.cmc_slug or not asset.get("is_active"):
        return reject("CMC ID map disagrees with asset binding")
    if (exchange.get("exchange_slug") != "binance" or not isinstance(exchange.get("exchange_id"), int)
            or pair.get("category") != "perpetual" or not isinstance(pair.get("market_id"), int)):
        return reject("exchange/market/contract category mismatch")
    if (exchange_registry.get("exchange_slug") != "binance" or
            exchange_registry.get("exchange_id") != exchange.get("exchange_id")):
        return reject("independent CMC exchange registry mismatch")
    own_exchange = pair.get("exchange", exchange)
    if own_exchange.get("exchange_id") != exchange.get("exchange_id"):
        return reject("pair exchange identity mismatch")
    if (base.get("crypto_id") != binding.cmc_id or base.get("exchange_symbol") != binding.binance_base
            or quote.get("crypto_id") != binding.quote_id or quote.get("exchange_symbol") != "USDT"):
        return reject("CMC base or quote identity mismatch")
    if (metadata.get("baseAsset") != binding.binance_base or metadata.get("quoteAsset") != "USDT"
            or metadata.get("marginAsset") != "USDT" or metadata.get("contractType") != "PERPETUAL"
            or metadata.get("status") != "TRADING" or metadata.get("symbol") != binding.binance_base + "USDT"):
        return reject("exchange instrument metadata mismatch")
    if pair.get("outlier_detected") is not False or pair.get("exclusions"):
        return reject("provider exclusions/outlier status not clean", EvidenceState.UNVERIFIED)
    if any(k in metadata and number(metadata[k]) != 1 for k in ("contractSize","contractMultiplier")):
        return reject("non-unit or malformed contract multiplier", EvidenceState.UNVERIFIED)
    instrument = Instrument(cmc_id=binding.cmc_id, market_id=pair["market_id"],
        exchange_id=exchange["exchange_id"], exchange="binance", symbol=metadata["symbol"],
        base=metadata["baseAsset"], quote="USDT", quote_id=binding.quote_id, margin_asset="USDT",
        metadata_ref=stable_id((metadata, metadata_at.isoformat())), binding_ref=stable_id(binding),
        metadata_acquired_at=metadata_at, binding_approved_at=binding.approved_at, map_acquired_at=map_acquired_at,
        exchange_registry_acquired_at=exchange_registry_acquired_at,exchange_registry_ref=stable_id(exchange_registry))
    return MappingResult(state=EvidenceState.VERIFIED, reason="reviewed ID binding and exact perpetual metadata agree", instrument=instrument)
