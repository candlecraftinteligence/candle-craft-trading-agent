"""Global Top-100 shadow counts. Rank 101 never refills an exclusion."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Sequence

from app.universe.symbol_universe import STABLECOIN_BASES, _is_leveraged_token_base

from research.market_data_foundation.cmc import CmcAsset, CmcListingPage, membership_problems
from research.market_data_foundation.constants import TOP100_SIZE


@dataclass(frozen=True)
class VenueContract:
    venue: str
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    underlying_type: str
    quote_volume_usdt: str | None


@dataclass(frozen=True)
class OwnerFixture:
    symbol: str
    state: str


@dataclass(frozen=True)
class StageCounts:
    requested_global_assets: int
    received_rows: int
    valid_ranked_assets: int
    identity_validated_assets: int
    supported_eligible_contracts: int
    liquidity_qualified_contracts: int
    unique_queued_discovery_contracts: int
    scan_attempts: int
    successfully_completed_scans: int
    insufficient_evidence_rejections: int
    other_failures: int
    accepted_outcomes: int
    rejected_outcomes: int
    owner_monitoring_count: int
    owners_outside_discovery: int
    membership_status: str
    count_kind: str
    exclusions: tuple[dict[str, str], ...]
    queued_symbols: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_global_assets": self.requested_global_assets,
            "received_rows": self.received_rows,
            "valid_ranked_assets": self.valid_ranked_assets,
            "identity_validated_assets": self.identity_validated_assets,
            "supported_eligible_contracts": self.supported_eligible_contracts,
            "liquidity_qualified_contracts": self.liquidity_qualified_contracts,
            "unique_queued_discovery_contracts": self.unique_queued_discovery_contracts,
            "scan_attempts": self.scan_attempts,
            "successfully_completed_scans": self.successfully_completed_scans,
            "insufficient_evidence_rejections": self.insufficient_evidence_rejections,
            "other_failures": self.other_failures,
            "accepted_outcomes": self.accepted_outcomes,
            "rejected_outcomes": self.rejected_outcomes,
            "owner_monitoring_count": self.owner_monitoring_count,
            "owners_outside_discovery": self.owners_outside_discovery,
            "membership_status": self.membership_status,
            "count_kind": self.count_kind,
            "scan_count_meaning": "not_executed; a successful scan is not claimed",
            "exclusions": list(self.exclusions),
            "queued_symbols": list(self.queued_symbols),
            "denominators": {
                "requested_global_assets": self.requested_global_assets,
                "received_rows": self.received_rows,
                "owner_monitoring_count": self.owner_monitoring_count,
            },
        }


def evaluate_top100(
    page: CmcListingPage,
    contracts: Sequence[VenueContract],
    *,
    owners: Sequence[OwnerFixture] = (),
    minimum_quote_volume_usdt: Decimal = Decimal("0"),
    expected: int = TOP100_SIZE,
    count_kind: str,
) -> StageCounts:
    problems = membership_problems(page, expected=expected)
    membership_status = "verified" if not problems else "unverified"
    boundary = [asset for asset in page.assets if asset.rank is not None and 1 <= asset.rank <= expected]
    # Exclusions happen after the boundary. Higher ranks are ignored, not promoted.
    identities = _identity_decisions(boundary)
    exclusions: list[dict[str, str]] = []
    queued: list[str] = []
    identity_validated = 0
    supported = 0
    liquid = 0
    insufficient = 0
    other = 0
    contracts_by_base = _admissible_contracts(contracts)
    for asset in boundary:
        decision = identities[asset.provider_id]
        if decision != "validated":
            exclusions.append(
                {
                    "provider_id": asset.provider_id,
                    "symbol": asset.symbol,
                    "rank": str(asset.rank),
                    "reason": decision,
                }
            )
            other += 1
            continue
        identity_validated += 1
        if asset.symbol in STABLECOIN_BASES or _is_leveraged_token_base(asset.symbol):
            exclusions.append(_reason(asset, "existing_stable_or_leveraged_policy"))
            other += 1
            continue
        matches = contracts_by_base.get(asset.symbol, ())
        if len(matches) != 1:
            reason = "ambiguous_venue_contract" if len(matches) > 1 else "no_supported_usdt_perpetual"
            exclusions.append(_reason(asset, reason))
            other += 1
            continue
        supported += 1
        contract = matches[0]
        if contract.quote_volume_usdt is None:
            exclusions.append(_reason(asset, "quote_volume_missing"))
            insufficient += 1
            continue
        try:
            volume = Decimal(contract.quote_volume_usdt)
        except (InvalidOperation, ValueError):
            exclusions.append(_reason(asset, "quote_volume_malformed"))
            insufficient += 1
            continue
        if not volume.is_finite() or volume < minimum_quote_volume_usdt:
            exclusions.append(_reason(asset, "below_existing_liquidity_minimum"))
            other += 1
            continue
        liquid += 1
        if contract.symbol not in queued:
            queued.append(contract.symbol)
    owner_symbols = tuple(dict.fromkeys(owner.symbol.upper() for owner in owners))
    outside = tuple(symbol for symbol in owner_symbols if symbol not in set(queued))
    valid_ranked = sum(1 for asset in page.assets if asset.rank_status == "valid" and asset.rank is not None)
    return StageCounts(
        requested_global_assets=expected,
        received_rows=len(page.assets),
        valid_ranked_assets=valid_ranked,
        identity_validated_assets=identity_validated,
        supported_eligible_contracts=supported,
        liquidity_qualified_contracts=liquid,
        unique_queued_discovery_contracts=len(queued),
        scan_attempts=0,
        successfully_completed_scans=0,
        insufficient_evidence_rejections=insufficient,
        other_failures=other,
        accepted_outcomes=0,
        rejected_outcomes=0,
        owner_monitoring_count=len(owner_symbols),
        owners_outside_discovery=len(outside),
        membership_status=membership_status,
        count_kind=count_kind,
        exclusions=tuple(exclusions),
        queued_symbols=tuple(queued),
    )


def assert_stage_arithmetic(counts: StageCounts) -> None:
    if counts.unique_queued_discovery_contracts > counts.liquidity_qualified_contracts:
        raise ValueError("queued contracts exceed liquidity-qualified contracts")
    if counts.liquidity_qualified_contracts > counts.supported_eligible_contracts:
        raise ValueError("liquidity-qualified contracts exceed supported contracts")
    if counts.supported_eligible_contracts > counts.identity_validated_assets:
        raise ValueError("supported contracts exceed identity-validated assets")
    if counts.scan_attempts != 0 or counts.successfully_completed_scans != 0:
        raise ValueError("this phase must not report an executed scan")
    if counts.accepted_outcomes != 0:
        raise ValueError("this phase must not report an accepted setup")


def select_shadow_sample(queued: Sequence[tuple[str, str]]) -> dict[str, object]:
    """Eight-name rule: BTC, ETH, three higher-volume, three lower-volume.

    ``queued`` is ``(symbol, quote_volume_usdt)`` already inside the eligible set.
    Ties break by symbol. Missing BTC or ETH is reported, not replaced from rank 101.
    """

    ordered = sorted(queued, key=lambda item: (-Decimal(item[1]), item[0]))
    symbols = [symbol for symbol, _volume in ordered]
    required = ("BTCUSDT", "ETHUSDT")
    missing_required = [symbol for symbol in required if symbol not in symbols]
    rest = [item for item in ordered if item[0] not in required]
    midpoint = len(rest) // 2
    liquid = rest[:midpoint]
    less = rest[midpoint:]
    picked_liquid = _spread(liquid, 3)
    picked_less = _spread(less, 3)
    selected = [symbol for symbol in required if symbol in symbols]
    selected.extend(symbol for symbol, _volume in picked_liquid)
    selected.extend(symbol for symbol, _volume in picked_less)
    return {
        "rule": "btc_eth_plus_volume_halves_spread_tiebreak_symbol",
        "eligible_count": len(ordered),
        "selected": selected,
        "missing_required": missing_required,
        "liquid_pool": len(liquid),
        "less_liquid_pool": len(less),
        "shortfall": max(0, 8 - len(selected)),
    }


def _spread(rows: Sequence[tuple[str, str]], count: int) -> list[tuple[str, str]]:
    if not rows or count <= 0:
        return []
    if len(rows) <= count:
        return list(rows)
    indexes = {0, len(rows) // 2, len(rows) - 1}
    ordered = [rows[index] for index in sorted(indexes)]
    return ordered[:count]


def _identity_decisions(assets: Sequence[CmcAsset]) -> dict[str, str]:
    by_symbol: dict[str, list[CmcAsset]] = {}
    for asset in assets:
        by_symbol.setdefault(asset.symbol, []).append(asset)
    ambiguous = {
        symbol
        for symbol, rows in by_symbol.items()
        if len({row.provider_id for row in rows}) > 1
    }
    decisions: dict[str, str] = {}
    for asset in assets:
        if asset.symbol in ambiguous:
            decisions[asset.provider_id] = "ambiguous_symbol"
        elif asset.provider_id.strip() == "":
            decisions[asset.provider_id] = "missing_provider_id"
        else:
            decisions[asset.provider_id] = "validated"
    return decisions


def _admissible_contracts(contracts: Sequence[VenueContract]) -> dict[str, tuple[VenueContract, ...]]:
    grouped: dict[str, list[VenueContract]] = {}
    for contract in contracts:
        if contract.quote_asset.upper() != "USDT":
            continue
        if contract.contract_type.upper() != "PERPETUAL":
            continue
        if contract.status.upper() != "TRADING":
            continue
        if contract.underlying_type.upper() != "COIN":
            continue
        if contract.symbol.upper() != f"{contract.base_asset.upper()}USDT":
            continue
        grouped.setdefault(contract.base_asset.upper(), []).append(contract)
    return {base: tuple(rows) for base, rows in grouped.items()}


def _reason(asset: CmcAsset, reason: str) -> dict[str, str]:
    return {
        "provider_id": asset.provider_id,
        "symbol": asset.symbol,
        "rank": "" if asset.rank is None else str(asset.rank),
        "reason": reason,
    }
