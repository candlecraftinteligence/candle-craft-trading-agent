"""Bridge existing exchange source facts; legacy numbers alone are never evidence."""
from __future__ import annotations

from datetime import datetime

from app.cmc.models import EvidenceState as S, Feature, Instrument, Observation, Comparison, stable_id
from app.cmc.comparison import compare
from app.data.derivatives_source_evidence import DerivativesEvidenceBatch
from app.data.derivatives_source_evidence import acquisition_fact
from app.data.derivatives_instrument import parse_binance_usds_instrument, bind_instrument_batch


def native_observations(evidence: dict, instrument: Instrument) -> tuple[Observation, ...]:
    result = []
    for label in ("open_interest", "funding_rate"):
        batch = evidence.get(label)
        if not isinstance(batch, DerivativesEvidenceBatch):
            continue
        metadata = parse_binance_usds_instrument({"symbols":[{
            "symbol":instrument.symbol, "baseAsset":instrument.base, "quoteAsset":instrument.quote,
            "marginAsset":instrument.margin_asset, "contractType":instrument.contract_type,
        }]}, requested_symbol=instrument.symbol, acquisition=acquisition_fact(
            instrument.metadata_acquired_at, quality="verified"))
        batch = bind_instrument_batch(batch, metadata)
        for row in batch.observations:
            identity = row.identity
            if (identity.exchange != "binance_futures" or identity.requested_symbol != instrument.symbol
                    or identity.returned_symbol != instrument.symbol
                    or set(identity.reason_codes) - {"category_from_request_context"}
                    or row.acquisition.acquired_at is None):
                continue
            for quantity in row.quantities:
                # The independent contract metadata establishes the request category.
                # Quote notional retains USDT rather than assuming parity with USD.
                units = {"base_asset_quantity":"base", "quote_notional":instrument.quote,
                         "funding_fraction":"fraction/interval"}.get(quantity.unit)
                if units is None:
                    continue
                verified = (row.semantic_quality == "verified" and quantity.quality == "verified"
                    and row.metric_time.quality == "verified" and row.acquisition.quality == "verified")
                fields = dict(provider="Binance", endpoint=row.endpoint, metric=label,
                    value=quantity.numeric_value, raw_value=quantity.raw_value, units=units, native_units=quantity.unit,
                    source_at=row.metric_time.at, acquired_at=row.acquisition.acquired_at,
                    state=S.VERIFIED if verified else S.UNVERIFIED, instrument=instrument,
                    cmc_id=instrument.cmc_id, exchange_id=instrument.exchange_id, scope="contract",
                    provenance=stable_id(str(row)), rate_semantics=row.rate_kind if label == "funding_rate" else None,
                    counting_convention=quantity.counting_convention if label == "open_interest" else None)
                result.append(Observation(id=stable_id(fields), **fields))
    return tuple(result)


def consistency_feature(cmc: tuple[Observation, ...], native: tuple[Observation, ...], *, as_of: datetime):
    comparisons = []
    refs, sources, acquired = [], [], []
    for left in cmc:
        if left.instrument is None or left.metric not in {"open_interest", "funding_rate", "basis", "price"}:
            continue
        right_rows = [r for r in native if r.metric == left.metric and r.instrument and r.instrument.key == left.instrument.key]
        if not right_rows:
            continue
        right = max(right_rows, key=lambda r:(r.source_at or r.acquired_at,r.id))
        classification = compare(left, right, as_of=as_of)
        comparisons.append({"left":left.id,"right":right.id,"classification":classification.value})
        if classification in {Comparison.CONSISTENT, Comparison.MATERIAL_DIVERGENCE}:
            refs.extend((left.id,right.id))
            sources.extend((left.source_at,right.source_at))
            acquired.extend((left.acquired_at,right.acquired_at))
    usable = [c["classification"] for c in comparisons if c["classification"] in {"CONSISTENT","MATERIAL_DIVERGENCE"}]
    feature = Feature(name="CmcDataConsistency", value=(-1 if "MATERIAL_DIVERGENCE" in usable else 1) if usable else None,
        units="classification", state=S.VERIFIED if usable else S.MISSING, evidence_ids=tuple(refs),
        input_timestamps=tuple(sources), acquired_timestamps=tuple(acquired),
        reason="Equivalent contract, units, funding semantics and time required", scope="contract")
    return feature, comparisons
