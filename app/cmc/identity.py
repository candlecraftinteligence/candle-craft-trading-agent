"""Explicit asset bindings plus independent exchange metadata, never ticker-only joins."""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from pydantic import Field

from app.cmc.models import EvidenceState, Frozen, Instrument, stable_id, number


class AssetBinding(Frozen):
    cmc_id: int = Field(gt=0)
    cmc_slug: str
    binance_base: str
    quote_id: int = Field(default=825, gt=0)
    approved: bool = False
    evidence_ref: str
    approved_at: datetime | None = None
    contract_symbol: str | None = None
    exchange_id: int | None = Field(default=None, gt=0)
    market_id: int | None = Field(default=None, gt=0)
    acquired_at: datetime | None = None
    reviewed_at: datetime | None = None
    expires_at: datetime | None = None
    owner_approval_ref: str | None = None
    qualification_class: str | None = None
    contract_multiplier: int = Field(default=1, ge=1, le=1000000)


class IdentityStatus(str, Enum):
    EVIDENCE_VERIFIED = "EVIDENCE_VERIFIED"
    IDENTITY_REVIEW_PENDING = "IDENTITY_REVIEW_PENDING"
    OWNER_APPROVED = "OWNER_APPROVED"
    EXPIRED = "EXPIRED"
    UNVERIFIED = "UNVERIFIED"


def binding_status(binding: AssetBinding, as_of: datetime) -> IdentityStatus:
    times = (binding.acquired_at, binding.expires_at)
    if as_of.tzinfo is None or any(t is None or t.tzinfo is None for t in times):
        return IdentityStatus.UNVERIFIED
    if (not binding.acquired_at < binding.expires_at or binding.acquired_at > as_of or not binding.evidence_ref.strip()
            or binding.contract_symbol != binding.binance_base + "USDT"
            or binding.exchange_id is None or binding.market_id is None or binding.quote_id != 825):
        return IdentityStatus.UNVERIFIED
    if as_of >= binding.expires_at:
        return IdentityStatus.EXPIRED
    if binding.reviewed_at is None:
        return IdentityStatus.UNVERIFIED if binding.approved else IdentityStatus.IDENTITY_REVIEW_PENDING
    if (binding.reviewed_at.tzinfo is None or not binding.acquired_at <= binding.reviewed_at < binding.expires_at
            or binding.reviewed_at > as_of):
        return IdentityStatus.UNVERIFIED
    if not binding.approved:
        return IdentityStatus.IDENTITY_REVIEW_PENDING
    ref = (binding.owner_approval_ref or "").strip()
    if (not ref or ref.upper() in {"N/A", "PENDING", "UNVERIFIED"}
            or binding.approved_at is None or binding.approved_at.tzinfo is None
            or not binding.reviewed_at <= binding.approved_at <= as_of):
        return IdentityStatus.UNVERIFIED
    return IdentityStatus.OWNER_APPROVED


class MappingResult(Frozen):
    state: EvidenceState
    reason: str
    instrument: Instrument | None = None


def match_instrument(pair: dict, exchange: dict, metadata: dict, binding: AssetBinding,
                     asset: dict, *, as_of: datetime, metadata_at: datetime,
                     acquired_at: datetime, map_acquired_at: datetime, exchange_registry: dict,
                     exchange_registry_acquired_at: datetime, require_owner_approval: bool = False) -> MappingResult:
    def reject(reason, state=EvidenceState.AMBIGUOUS):
        return MappingResult(state=state, reason=reason)

    times = (binding.approved_at, metadata_at, acquired_at, as_of, map_acquired_at,exchange_registry_acquired_at)
    if any(value is None or value.tzinfo is None for value in times):
        return reject("timezone required", EvidenceState.UNVERIFIED)
    if require_owner_approval and binding_status(binding, as_of) != IdentityStatus.OWNER_APPROVED:
        return reject("current exact-contract owner approval required", EvidenceState.UNVERIFIED)
    expiry = binding.expires_at or binding.approved_at + timedelta(days=30)
    if expiry.tzinfo is None or as_of >= expiry:
        return reject("asset binding expired", EvidenceState.STALE)
    if not binding.approved or not binding.evidence_ref or binding.approved_at > as_of:
        return reject("explicit reviewed asset binding required", EvidenceState.UNVERIFIED)
    if any(t > as_of for t in times) or metadata_at > acquired_at:
        return reject("future identity evidence", EvidenceState.UNVERIFIED)
    if any((as_of-t).total_seconds() > 86400 for t in (metadata_at, map_acquired_at, exchange_registry_acquired_at)):
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
    if ((binding.exchange_id is not None and binding.exchange_id != exchange.get("exchange_id"))
            or (binding.market_id is not None and binding.market_id != pair.get("market_id"))):
        return reject("reviewed exact market/exchange IDs disagree")
    own_exchange = pair.get("exchange", exchange)
    if (own_exchange.get("exchange_id") != exchange.get("exchange_id")
            or own_exchange.get("exchange_slug", "binance") != "binance"):
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
        exchange_registry_acquired_at=exchange_registry_acquired_at,exchange_registry_ref=stable_id(exchange_registry),
        binding_expires_at=expiry)
    return MappingResult(state=EvidenceState.VERIFIED, reason="reviewed ID binding and exact perpetual metadata agree", instrument=instrument)
