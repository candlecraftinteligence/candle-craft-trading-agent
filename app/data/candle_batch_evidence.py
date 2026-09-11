"""In-memory candle-batch delivery capture (PROSPECTIVE_BATCH_DELIVERY_CAPTURE).

Capture-only. Associates locally observed adapter/cache returns and subsequent
candle transformations with the actual sequence reaching the lifecycle handoff.

This module does not persist evidence, mint source/admission/episode IDs, bind
evaluator policy, authenticate a venue, or prove possession at a logical cutoff.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

from app.data.dtos import NA

CAPTURE_FORMAT_VERSION: Final[str] = "cci-candle-batch-delivery-v1"
KLINES_DELIVERY_CAPTURE_MARKER: Final[str] = "_cci_klines_delivery_capture"
DAY_MS: Final[int] = 86_400_000

PRODUCER_ADAPTER_NORMALIZATION_RETURN: Final[str] = "adapter_normalization_return"
PRODUCER_CACHE_RETURN: Final[str] = "cache_return"
PRODUCER_CLIENT_RETURN: Final[str] = "client_return_unsupported_capture"
PRODUCER_FILTER_SELECTION: Final[str] = "closed_candle_selection"
PRODUCER_TRANSFORM_2D: Final[str] = "synthetic_2d_resample"
PRODUCER_EMPTY_EXECUTION: Final[str] = "empty_execution_selection"
PRODUCER_UNAVAILABLE: Final[str] = "unavailable"

CACHE_KIND_MISS: Final[str] = "miss"
CACHE_KIND_HIT: Final[str] = "hit"
CACHE_KIND_EXPIRY_REFETCH: Final[str] = "expiry_refetch"
CACHE_KIND_NOT_APPLICABLE: Final[str] = "not_applicable"
CACHE_KIND_UNAVAILABLE: Final[str] = "unavailable"

DISPOSITION_CAPTURED: Final[str] = "captured"
DISPOSITION_UNAVAILABLE: Final[str] = "unavailable"
DISPOSITION_MISMATCHED: Final[str] = "mismatched"

HANDOFF_MATCHED: Final[str] = "matched"
HANDOFF_MISMATCHED: Final[str] = "mismatched"
HANDOFF_UNAVAILABLE: Final[str] = "unavailable"

SNAPSHOT_INCLUDED_FIELDS: Final[tuple[str, ...]] = (
    "exchange",
    "symbol",
    "interval",
    "timestamp",
    "open_timestamp",
    "opened_at",
    "close_timestamp",
    "closed_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
)
SNAPSHOT_EXCLUDED_FIELDS: Final[tuple[str, ...]] = (
    "raw_source",
    "http_body",
    "retry_history",
    "headers",
    "request_body",
)
IDENTITY_AND_VALUE_FIELDS: Final[tuple[str, ...]] = SNAPSHOT_INCLUDED_FIELDS

LIMIT_LOCAL_COMPLETION_NOT_VENUE: Final[str] = "local_completion_is_not_venue_publication_time"
LIMIT_CLASS_NOT_VENUE: Final[str] = "class_identity_is_not_venue_proof"
LIMIT_CONFIG_NOT_PROVENANCE: Final[str] = "config_exchange_is_not_provenance"
LIMIT_CUTOFF_NOT_POSSESSION: Final[str] = "cutoff_is_not_possession"
LIMIT_POSSESSION_UNAVAILABLE: Final[str] = "possession_at_logical_cutoff_unavailable"
LIMIT_INJECTED_TRANSPORT: Final[str] = "injected_transport_is_not_venue_proof"
LIMIT_CACHE_REUSE: Final[str] = "cache_hit_is_not_new_acquisition"
LIMIT_RESAMPLE_NOT_ACQUISITION: Final[str] = "resample_is_not_new_venue_observation"
LIMIT_WEAKER_CLIENT_RETURN: Final[str] = "unsupported_client_return_is_not_adapter_normalization"
LIMIT_FILE_CACHE_ACQUISITION: Final[str] = "legacy_cache_entry_lacks_acquisition_observation"
LIMIT_UNINSTRUMENTED_INSERT: Final[str] = "cache_entry_populated_without_capture"
LIMIT_PROJECTION_NOT_RAW_EQUALITY: Final[str] = "projection_equality_is_not_raw_response_equality"
LIMIT_PROJECTION_EQUIVALENCE_NOT_EXACT_MEMBERSHIP: Final[str] = (
    "closed_selection_projection_equivalent_not_exact_membership"
)
LIMIT_ONE_METHOD_RETURN_NOT_RETRY_LEDGER: Final[str] = "successful_method_return_is_not_http_retry_ledger"
LIMIT_WALL_CLOCK_NOT_TOTAL_ORDER: Final[str] = "wall_clock_observation_is_not_trusted_total_order"

ADAPTER_LIMITATIONS: Final[tuple[str, ...]] = (
    LIMIT_LOCAL_COMPLETION_NOT_VENUE,
    LIMIT_CLASS_NOT_VENUE,
    LIMIT_CONFIG_NOT_PROVENANCE,
    LIMIT_CUTOFF_NOT_POSSESSION,
    LIMIT_POSSESSION_UNAVAILABLE,
    LIMIT_INJECTED_TRANSPORT,
    LIMIT_PROJECTION_NOT_RAW_EQUALITY,
    LIMIT_ONE_METHOD_RETURN_NOT_RETRY_LEDGER,
    LIMIT_WALL_CLOCK_NOT_TOTAL_ORDER,
)


@dataclass(frozen=True)
class CandleProjectionRecord:
    values: MappingProxyType[str, Any]
    missing_supported_fields: tuple[str, ...]
    representation_unsupported: bool = False

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "values": {key: _canonical_scalar(self.values[key]) for key in sorted(self.values)},
            "missing_supported_fields": list(self.missing_supported_fields),
            "representation_unsupported": self.representation_unsupported,
        }


@dataclass(frozen=True)
class CandleBatchSnapshot:
    format_version: str
    record_count: int
    records: tuple[CandleProjectionRecord, ...]
    included_fields: tuple[str, ...]
    excluded_fields: tuple[str, ...]
    unavailable: bool = False
    unavailable_reason: str | None = None
    _fingerprint: str | None = None

    @property
    def content_fingerprint(self) -> str:
        if self.unavailable:
            return ""
        if self._fingerprint:
            return self._fingerprint
        computed = _fingerprint_records(self.records)
        object.__setattr__(self, "_fingerprint", computed)
        return computed

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "record_count": self.record_count,
            "records": [record.to_canonical_dict() for record in self.records],
            "content_fingerprint": self.content_fingerprint,
            "included_fields": list(self.included_fields),
            "excluded_fields": list(self.excluded_fields),
            "unavailable": self.unavailable,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class AdapterAcquisitionObservation:
    occurrence_token: str
    requested_symbol: str
    requested_interval: str
    requested_limit: int
    effective_symbol: str
    effective_interval: str
    effective_limit: int
    endpoint_path: str
    adapter_class_label: str
    observed_completed_at: datetime
    snapshot: CandleBatchSnapshot
    limitations: tuple[str, ...]
    http_client_injected: bool
    base_url_label: str | None = None


@dataclass(frozen=True)
class CacheDeliveryObservation:
    occurrence_token: str
    delivery_kind: str
    cache_bookkeeping_created_at: float | None
    cache_expires_at: float | None
    observed_completed_at: datetime
    snapshot: CandleBatchSnapshot
    upstream_acquisition: AdapterAcquisitionObservation | None
    upstream_unavailable_reason: str | None
    limitations: tuple[str, ...]
    cache_enabled: bool


@dataclass(frozen=True)
class SelectionObservation:
    occurrence_token: str
    kind: str
    logical_cutoff: datetime | None
    selected_membership_indices: tuple[int, ...] | None
    membership_complete: bool
    output_snapshot: CandleBatchSnapshot
    projection_equivalent_indices: tuple[int, ...] | None = None


@dataclass(frozen=True)
class TransformObservation:
    occurrence_token: str
    transformation_kind: str
    target_interval: str
    source_pairs: tuple[tuple[int, int], ...] | None
    mapping_complete: bool
    output_snapshot: CandleBatchSnapshot


@dataclass(frozen=True)
class CandleBatchDelivery:
    format_version: str
    occurrence_token: str
    returned_sequence: tuple[Any, ...]
    snapshot: CandleBatchSnapshot
    producer_boundary: str
    disposition: str
    unavailable_reason: str | None
    limitations: tuple[str, ...]
    observed_at: datetime | None
    request_symbol: str | None = None
    request_interval: str | None = None
    request_limit: int | None = None
    adapter: AdapterAcquisitionObservation | None = None
    cache: CacheDeliveryObservation | None = None
    selection: SelectionObservation | None = None
    transform: TransformObservation | None = None
    parent: CandleBatchDelivery | None = None
    logical_cutoff: datetime | None = None
    lineage_complete: bool = True


@dataclass(frozen=True)
class BatchHandoffAssociation:
    disposition: str
    reason: str | None
    delivery: CandleBatchDelivery | None
    execution_timeframe: str | None
    logical_cutoff: datetime | None
    execution_snapshot: CandleBatchSnapshot | None
    matched_fingerprint: str | None
    observed_at: datetime | None


def new_capture_occurrence_token() -> str:
    return secrets.token_hex(16)


def default_capture_clock() -> datetime:
    return datetime.now(UTC)


def snapshot_candle_batch(candles: Any) -> CandleBatchSnapshot:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        return _unavailable_snapshot("unsupported_batch_shape")
    records: list[CandleProjectionRecord] = []
    for item in candles:
        records.append(_project_candle(item))
    frozen_records = tuple(records)
    if any(record.representation_unsupported for record in frozen_records):
        return _unavailable_snapshot(
            "unsupported_candle_shape",
            records=frozen_records,
        )
    return CandleBatchSnapshot(
        format_version=CAPTURE_FORMAT_VERSION,
        record_count=len(frozen_records),
        records=frozen_records,
        included_fields=SNAPSHOT_INCLUDED_FIELDS,
        excluded_fields=SNAPSHOT_EXCLUDED_FIELDS,
    )


def observe_adapter_normalized_batch(
    candles: Sequence[Any],
    *,
    requested_symbol: str,
    requested_interval: str,
    requested_limit: int,
    effective_symbol: str,
    effective_interval: str,
    effective_limit: int,
    endpoint_path: str,
    adapter_class_label: str,
    capture_clock: Callable[[], datetime],
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
    http_client_injected: bool,
    base_url_label: str | None = None,
) -> CandleBatchDelivery:
    occurrence = occurrence_factory()
    observed_at = _sample_capture_clock(capture_clock)
    snapshot = snapshot_candle_batch(candles)
    adapter = AdapterAcquisitionObservation(
        occurrence_token=occurrence,
        requested_symbol=requested_symbol,
        requested_interval=requested_interval,
        requested_limit=requested_limit,
        effective_symbol=effective_symbol,
        effective_interval=effective_interval,
        effective_limit=effective_limit,
        endpoint_path=endpoint_path,
        adapter_class_label=adapter_class_label,
        observed_completed_at=observed_at,
        snapshot=snapshot,
        limitations=ADAPTER_LIMITATIONS,
        http_client_injected=http_client_injected,
        base_url_label=base_url_label,
    )
    disposition = DISPOSITION_UNAVAILABLE if snapshot.unavailable else DISPOSITION_CAPTURED
    return CandleBatchDelivery(
        format_version=CAPTURE_FORMAT_VERSION,
        occurrence_token=occurrence,
        returned_sequence=tuple(candles),
        snapshot=snapshot,
        producer_boundary=PRODUCER_ADAPTER_NORMALIZATION_RETURN,
        disposition=disposition,
        unavailable_reason=snapshot.unavailable_reason,
        limitations=ADAPTER_LIMITATIONS,
        observed_at=observed_at,
        request_symbol=effective_symbol,
        request_interval=effective_interval,
        request_limit=effective_limit,
        adapter=adapter,
        lineage_complete=not snapshot.unavailable,
    )


def observe_unsupported_client_return(
    candles: Sequence[Any],
    *,
    symbol: str,
    interval: str,
    limit: int,
    capture_clock: Callable[[], datetime],
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
    client_class_label: str | None = None,
) -> CandleBatchDelivery:
    occurrence = occurrence_factory()
    observed_at = _sample_capture_clock(capture_clock)
    snapshot = snapshot_candle_batch(candles)
    limitations = (
        LIMIT_WEAKER_CLIENT_RETURN,
        LIMIT_CLASS_NOT_VENUE,
        LIMIT_CUTOFF_NOT_POSSESSION,
        LIMIT_POSSESSION_UNAVAILABLE,
        LIMIT_PROJECTION_NOT_RAW_EQUALITY,
        LIMIT_WALL_CLOCK_NOT_TOTAL_ORDER,
    )
    disposition = DISPOSITION_UNAVAILABLE if snapshot.unavailable else DISPOSITION_CAPTURED
    return CandleBatchDelivery(
        format_version=CAPTURE_FORMAT_VERSION,
        occurrence_token=occurrence,
        returned_sequence=tuple(candles),
        snapshot=snapshot,
        producer_boundary=PRODUCER_CLIENT_RETURN,
        disposition=disposition,
        unavailable_reason=snapshot.unavailable_reason or (
            None if disposition == DISPOSITION_CAPTURED else "unsupported_client_capture"
        ),
        limitations=limitations + ((f"client_class_label={client_class_label}",) if client_class_label else ()),
        observed_at=observed_at,
        request_symbol=symbol,
        request_interval=interval,
        request_limit=limit,
        lineage_complete=False,
    )


def observe_cache_delivery(
    candles: Sequence[Any],
    *,
    delivery_kind: str,
    capture_clock: Callable[[], datetime],
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
    cache_bookkeeping_created_at: float | None,
    cache_expires_at: float | None,
    cache_enabled: bool,
    upstream: CandleBatchDelivery | None,
    upstream_unavailable_reason: str | None,
    symbol: str | None,
    interval: str | None,
    limit: int | None,
) -> CandleBatchDelivery:
    occurrence = occurrence_factory()
    observed_at = _sample_capture_clock(capture_clock)
    if upstream is not None and _same_object_sequence(upstream.returned_sequence, candles):
        snapshot = upstream.snapshot
    else:
        snapshot = snapshot_candle_batch(candles)
    limitations = [
        LIMIT_CUTOFF_NOT_POSSESSION,
        LIMIT_POSSESSION_UNAVAILABLE,
        LIMIT_PROJECTION_NOT_RAW_EQUALITY,
        LIMIT_WALL_CLOCK_NOT_TOTAL_ORDER,
    ]
    if delivery_kind == CACHE_KIND_HIT:
        limitations.append(LIMIT_CACHE_REUSE)
    adapter = None
    if upstream is not None and upstream.adapter is not None:
        adapter = upstream.adapter
        limitations.extend(item for item in upstream.limitations if item not in limitations)
    elif upstream_unavailable_reason == LIMIT_FILE_CACHE_ACQUISITION:
        limitations.append(LIMIT_FILE_CACHE_ACQUISITION)
    elif upstream_unavailable_reason == LIMIT_UNINSTRUMENTED_INSERT:
        limitations.append(LIMIT_UNINSTRUMENTED_INSERT)
    elif delivery_kind != CACHE_KIND_NOT_APPLICABLE:
        limitations.append(LIMIT_WEAKER_CLIENT_RETURN)
    if delivery_kind == CACHE_KIND_NOT_APPLICABLE and upstream is not None:
        limitations.extend(item for item in upstream.limitations if item not in limitations)
        adapter = upstream.adapter
    cache_obs = CacheDeliveryObservation(
        occurrence_token=occurrence,
        delivery_kind=delivery_kind,
        cache_bookkeeping_created_at=cache_bookkeeping_created_at,
        cache_expires_at=cache_expires_at,
        observed_completed_at=observed_at,
        snapshot=snapshot,
        upstream_acquisition=adapter,
        upstream_unavailable_reason=upstream_unavailable_reason,
        limitations=tuple(limitations),
        cache_enabled=cache_enabled,
    )
    parent = upstream if delivery_kind == CACHE_KIND_NOT_APPLICABLE else None
    producer_boundary = PRODUCER_CACHE_RETURN
    if delivery_kind == CACHE_KIND_NOT_APPLICABLE:
        producer_boundary = upstream.producer_boundary if upstream is not None else PRODUCER_CLIENT_RETURN
        parent = upstream
    disposition = DISPOSITION_UNAVAILABLE if snapshot.unavailable else DISPOSITION_CAPTURED
    lineage_complete = (
        not snapshot.unavailable
        and (
            adapter is not None
            or delivery_kind == CACHE_KIND_NOT_APPLICABLE and upstream is not None and upstream.lineage_complete
        )
    )
    return CandleBatchDelivery(
        format_version=CAPTURE_FORMAT_VERSION,
        occurrence_token=occurrence,
        returned_sequence=tuple(candles),
        snapshot=snapshot,
        producer_boundary=producer_boundary,
        disposition=disposition,
        unavailable_reason=snapshot.unavailable_reason,
        limitations=tuple(limitations),
        observed_at=observed_at,
        request_symbol=symbol if symbol is not None else (upstream.request_symbol if upstream else None),
        request_interval=interval if interval is not None else (upstream.request_interval if upstream else None),
        request_limit=limit if limit is not None else (upstream.request_limit if upstream else None),
        adapter=adapter if adapter is not None else (upstream.adapter if upstream else None),
        cache=cache_obs,
        parent=parent,
        lineage_complete=bool(lineage_complete),
    )


def observe_closed_subset(
    parent: CandleBatchDelivery,
    selected_candles: Sequence[Any],
    *,
    logical_cutoff: datetime | None,
    capture_clock: Callable[[], datetime],
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
) -> CandleBatchDelivery:
    occurrence = occurrence_factory()
    observed_at = _sample_capture_clock(capture_clock)
    indices = _exact_membership_indices(parent.returned_sequence, selected_candles)
    membership_complete = indices is not None
    projection_equivalent_indices = None
    if not membership_complete:
        projection_equivalent_indices = _projection_equivalent_indices(
            parent.returned_sequence,
            selected_candles,
        )
    if (
        membership_complete
        and indices is not None
        and not parent.snapshot.unavailable
        and len(parent.snapshot.records) == len(parent.returned_sequence)
    ):
        output_snapshot = CandleBatchSnapshot(
            format_version=CAPTURE_FORMAT_VERSION,
            record_count=len(indices),
            records=tuple(parent.snapshot.records[index] for index in indices),
            included_fields=SNAPSHOT_INCLUDED_FIELDS,
            excluded_fields=SNAPSHOT_EXCLUDED_FIELDS,
        )
    else:
        output_snapshot = snapshot_candle_batch(selected_candles)
    selection = SelectionObservation(
        occurrence_token=occurrence,
        kind=PRODUCER_FILTER_SELECTION,
        logical_cutoff=logical_cutoff,
        selected_membership_indices=indices,
        membership_complete=membership_complete,
        output_snapshot=output_snapshot,
        projection_equivalent_indices=projection_equivalent_indices,
    )
    limitations = parent.limitations + (LIMIT_CUTOFF_NOT_POSSESSION, LIMIT_POSSESSION_UNAVAILABLE)
    if not membership_complete:
        limitations = limitations + ("closed_selection_membership_incomplete",)
        if projection_equivalent_indices is not None:
            limitations = limitations + (LIMIT_PROJECTION_EQUIVALENCE_NOT_EXACT_MEMBERSHIP,)
    disposition = DISPOSITION_UNAVAILABLE if output_snapshot.unavailable else DISPOSITION_CAPTURED
    return CandleBatchDelivery(
        format_version=CAPTURE_FORMAT_VERSION,
        occurrence_token=occurrence,
        returned_sequence=tuple(selected_candles),
        snapshot=output_snapshot,
        producer_boundary=PRODUCER_FILTER_SELECTION,
        disposition=disposition,
        unavailable_reason=output_snapshot.unavailable_reason,
        limitations=tuple(dict.fromkeys(limitations)),
        observed_at=observed_at,
        request_symbol=parent.request_symbol,
        request_interval=parent.request_interval,
        request_limit=parent.request_limit,
        adapter=parent.adapter,
        cache=parent.cache,
        selection=selection,
        parent=parent,
        logical_cutoff=logical_cutoff,
        lineage_complete=parent.lineage_complete and membership_complete and not output_snapshot.unavailable,
    )


def observe_synthetic_2d_resample(
    parent: CandleBatchDelivery,
    output_candles: Sequence[Any],
    *,
    target_interval: str,
    logical_cutoff: datetime | None,
    capture_clock: Callable[[], datetime],
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
) -> CandleBatchDelivery:
    occurrence = occurrence_factory()
    observed_at = _sample_capture_clock(capture_clock)
    output_snapshot = snapshot_candle_batch(output_candles)
    pairs, mapping_complete = _infer_2d_source_pairs(parent.returned_sequence, output_candles)
    transform = TransformObservation(
        occurrence_token=occurrence,
        transformation_kind=PRODUCER_TRANSFORM_2D,
        target_interval=target_interval,
        source_pairs=pairs,
        mapping_complete=mapping_complete,
        output_snapshot=output_snapshot,
    )
    limitations = parent.limitations + (
        LIMIT_RESAMPLE_NOT_ACQUISITION,
        LIMIT_CUTOFF_NOT_POSSESSION,
        LIMIT_POSSESSION_UNAVAILABLE,
    )
    if not mapping_complete:
        limitations = limitations + ("resample_parent_mapping_incomplete",)
    disposition = DISPOSITION_UNAVAILABLE if output_snapshot.unavailable else DISPOSITION_CAPTURED
    return CandleBatchDelivery(
        format_version=CAPTURE_FORMAT_VERSION,
        occurrence_token=occurrence,
        returned_sequence=tuple(output_candles),
        snapshot=output_snapshot,
        producer_boundary=PRODUCER_TRANSFORM_2D,
        disposition=disposition,
        unavailable_reason=output_snapshot.unavailable_reason,
        limitations=tuple(dict.fromkeys(limitations)),
        observed_at=observed_at,
        request_symbol=parent.request_symbol,
        request_interval=target_interval,
        request_limit=parent.request_limit,
        adapter=parent.adapter,
        cache=parent.cache,
        selection=parent.selection,
        transform=transform,
        parent=parent,
        logical_cutoff=logical_cutoff,
        lineage_complete=parent.lineage_complete and mapping_complete and not output_snapshot.unavailable,
    )


def observe_empty_execution(
    *,
    execution_timeframe: str,
    logical_cutoff: datetime | None,
    capture_clock: Callable[[], datetime],
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
    parent: CandleBatchDelivery | None = None,
) -> CandleBatchDelivery:
    occurrence = occurrence_factory()
    observed_at = _sample_capture_clock(capture_clock)
    snapshot = snapshot_candle_batch(())
    return CandleBatchDelivery(
        format_version=CAPTURE_FORMAT_VERSION,
        occurrence_token=occurrence,
        returned_sequence=(),
        snapshot=snapshot,
        producer_boundary=PRODUCER_EMPTY_EXECUTION,
        disposition=DISPOSITION_CAPTURED,
        unavailable_reason=None,
        limitations=(
            "empty_execution_is_not_usable_market_evidence",
            LIMIT_CUTOFF_NOT_POSSESSION,
            LIMIT_POSSESSION_UNAVAILABLE,
        ),
        observed_at=observed_at,
        request_interval=execution_timeframe,
        parent=parent,
        logical_cutoff=logical_cutoff,
        lineage_complete=False,
    )


def unavailable_delivery(
    candles: Sequence[Any] | None,
    *,
    reason: str,
    capture_clock: Callable[[], datetime] | None = None,
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
    producer_boundary: str = PRODUCER_UNAVAILABLE,
) -> CandleBatchDelivery:
    occurrence = occurrence_factory()
    observed_at = None
    if capture_clock is not None:
        try:
            observed_at = _sample_capture_clock(capture_clock)
        except Exception:
            observed_at = None
    sequence = tuple(candles) if isinstance(candles, Sequence) and not isinstance(candles, (str, bytes)) else ()
    snapshot = _unavailable_snapshot(reason)
    return CandleBatchDelivery(
        format_version=CAPTURE_FORMAT_VERSION,
        occurrence_token=occurrence,
        returned_sequence=sequence,
        snapshot=snapshot,
        producer_boundary=producer_boundary,
        disposition=DISPOSITION_UNAVAILABLE,
        unavailable_reason=reason,
        limitations=(reason, LIMIT_POSSESSION_UNAVAILABLE),
        observed_at=observed_at,
        lineage_complete=False,
    )


def associate_execution_handoff(
    delivery: CandleBatchDelivery | None,
    *,
    execution_candles: Sequence[Any] | None,
    execution_timeframe: str | None,
    logical_cutoff: Any,
    capture_clock: Callable[[], datetime] = default_capture_clock,
) -> BatchHandoffAssociation:
    observed_at: datetime | None
    try:
        observed_at = _sample_capture_clock(capture_clock)
    except Exception:
        observed_at = None
    cutoff = _optional_datetime(logical_cutoff)
    if execution_candles is None:
        return BatchHandoffAssociation(
            disposition=HANDOFF_UNAVAILABLE,
            reason="execution_candles_none",
            delivery=None,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=None,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    try:
        execution_snapshot = snapshot_candle_batch(execution_candles)
    except Exception:
        return BatchHandoffAssociation(
            disposition=HANDOFF_UNAVAILABLE,
            reason="execution_snapshot_failed",
            delivery=None,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=None,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    if delivery is None:
        return BatchHandoffAssociation(
            disposition=HANDOFF_UNAVAILABLE,
            reason="lifecycle_cannot_invent_producer_history",
            delivery=None,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=execution_snapshot,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    if delivery.disposition == DISPOSITION_UNAVAILABLE:
        return BatchHandoffAssociation(
            disposition=HANDOFF_UNAVAILABLE,
            reason=delivery.unavailable_reason or "captured_delivery_unavailable",
            delivery=delivery,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=execution_snapshot,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    if execution_snapshot.unavailable or delivery.snapshot.unavailable:
        return BatchHandoffAssociation(
            disposition=HANDOFF_UNAVAILABLE,
            reason="snapshot_unavailable",
            delivery=delivery,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=execution_snapshot,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    if not _projections_match(execution_snapshot, delivery.snapshot):
        return BatchHandoffAssociation(
            disposition=HANDOFF_MISMATCHED,
            reason="execution_projection_does_not_match_captured_batch",
            delivery=delivery,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=execution_snapshot,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    expected_tf = _normalize_interval(execution_timeframe)
    captured_tf = _normalize_interval(delivery.request_interval)
    if expected_tf and captured_tf and expected_tf != captured_tf:
        return BatchHandoffAssociation(
            disposition=HANDOFF_MISMATCHED,
            reason="execution_timeframe_does_not_match_captured_batch",
            delivery=delivery,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=execution_snapshot,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    if delivery.logical_cutoff is not None and cutoff is not None and delivery.logical_cutoff != cutoff:
        return BatchHandoffAssociation(
            disposition=HANDOFF_MISMATCHED,
            reason="logical_cutoff_does_not_match_captured_batch",
            delivery=delivery,
            execution_timeframe=execution_timeframe,
            logical_cutoff=cutoff,
            execution_snapshot=execution_snapshot,
            matched_fingerprint=None,
            observed_at=observed_at,
        )
    return BatchHandoffAssociation(
        disposition=HANDOFF_MATCHED,
        reason=None,
        delivery=delivery,
        execution_timeframe=execution_timeframe,
        logical_cutoff=cutoff,
        execution_snapshot=execution_snapshot,
        matched_fingerprint=execution_snapshot.content_fingerprint,
        observed_at=observed_at,
    )


def client_offers_same_path_klines_delivery(client: Any) -> bool:
    if client is None:
        return False
    client_type = type(client)
    if not getattr(client_type, KLINES_DELIVERY_CAPTURE_MARKER, False):
        return False
    capture = getattr(client, "get_klines_with_delivery", None)
    get_klines = getattr(client, "get_klines", None)
    if not callable(capture) or not callable(get_klines):
        return False
    capture_owner = None
    for cls in client_type.mro():
        if "get_klines_with_delivery" in cls.__dict__:
            capture_owner = cls
            break
    if capture_owner is None or "get_klines" not in capture_owner.__dict__:
        return False
    resolved_klines = inspect.unwrap(client_type.get_klines)
    owner_klines = inspect.unwrap(capture_owner.__dict__["get_klines"])
    if resolved_klines is not owner_klines:
        return False
    instance_func = getattr(get_klines, "__func__", get_klines)
    if inspect.unwrap(instance_func) is not owner_klines:
        return False
    return True


async def fetch_klines_with_delivery(
    client: Any,
    symbol: str,
    interval: str,
    limit: int,
    *,
    capture_clock: Callable[[], datetime],
    occurrence_factory: Callable[[], str] = new_capture_occurrence_token,
) -> CandleBatchDelivery:
    if client_offers_same_path_klines_delivery(client):
        return await client.get_klines_with_delivery(
            symbol,
            interval,
            limit,
            capture_clock=capture_clock,
            occurrence_factory=occurrence_factory,
        )
    candles = await client.get_klines(symbol, interval, limit)
    try:
        return observe_unsupported_client_return(
            candles,
            symbol=symbol,
            interval=interval,
            limit=limit,
            capture_clock=capture_clock,
            occurrence_factory=occurrence_factory,
            client_class_label=type(client).__name__,
        )
    except Exception:
        return unavailable_delivery(
            candles if isinstance(candles, Sequence) else (),
            reason="capture_representation_failed",
            capture_clock=capture_clock,
            occurrence_factory=occurrence_factory,
            producer_boundary=PRODUCER_CLIENT_RETURN,
        )


def safe_observe(factory: Callable[[], CandleBatchDelivery], fallback_candles: Sequence[Any] | None) -> CandleBatchDelivery:
    try:
        return factory()
    except Exception:
        return unavailable_delivery(fallback_candles, reason="capture_representation_failed")


def _project_candle(candle: Any) -> CandleProjectionRecord:
    if candle is None or isinstance(candle, (str, bytes, int, float, bool)):
        return CandleProjectionRecord(
            values=MappingProxyType({}),
            missing_supported_fields=SNAPSHOT_INCLUDED_FIELDS,
            representation_unsupported=True,
        )
    values: dict[str, Any] = {}
    missing: list[str] = []
    for field in SNAPSHOT_INCLUDED_FIELDS:
        raw = _field(candle, field)
        if _is_absent(raw):
            if field in {"open", "high", "low", "close", "volume", "timestamp"}:
                missing.append(field)
            continue
        isolated = _isolate_value(raw)
        if isolated is _UNSUPPORTED:
            return CandleProjectionRecord(
                values=MappingProxyType({}),
                missing_supported_fields=SNAPSHOT_INCLUDED_FIELDS,
                representation_unsupported=True,
            )
        values[field] = isolated
    return CandleProjectionRecord(
        values=MappingProxyType(values),
        missing_supported_fields=tuple(missing),
    )


def _unavailable_snapshot(
    reason: str,
    *,
    records: tuple[CandleProjectionRecord, ...] = (),
) -> CandleBatchSnapshot:
    return CandleBatchSnapshot(
        format_version=CAPTURE_FORMAT_VERSION,
        record_count=len(records),
        records=records,
        included_fields=SNAPSHOT_INCLUDED_FIELDS,
        excluded_fields=SNAPSHOT_EXCLUDED_FIELDS,
        unavailable=True,
        unavailable_reason=reason,
    )


def _fingerprint_records(records: tuple[CandleProjectionRecord, ...]) -> str:
    payload = {
        "format_version": CAPTURE_FORMAT_VERSION,
        "records": [record.to_canonical_dict() for record in records],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, datetime):
        return {"datetime": value.astimezone(UTC).isoformat()}
    if value is NA:
        return {"na": True}
    return value


_UNSUPPORTED = object()


def _isolate_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return Decimal(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, str, bool)) or value is None or value is NA:
        return value
    if isinstance(value, float):
        return value
    return _UNSUPPORTED


def _field(candle: Any, name: str) -> Any:
    if isinstance(candle, Mapping):
        return candle.get(name, None)
    return getattr(candle, name, None)


def _is_absent(value: Any) -> bool:
    return value is None or value == ""


def _sample_capture_clock(capture_clock: Callable[[], datetime]) -> datetime:
    sampled = capture_clock()
    if not isinstance(sampled, datetime):
        raise TypeError("capture clock must return datetime")
    if sampled.tzinfo is None:
        raise ValueError("capture clock must return timezone-aware datetime")
    return sampled


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "" or value == NA:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return None


def _same_object_sequence(left: Sequence[Any], right: Sequence[Any]) -> bool:
    if left is right:
        return True
    if len(left) != len(right):
        return False
    return all(first is second for first, second in zip(left, right, strict=True))


def _projections_match(left: CandleBatchSnapshot, right: CandleBatchSnapshot) -> bool:
    if left.unavailable or right.unavailable:
        return False
    if left.record_count != right.record_count or len(left.records) != len(right.records):
        return False
    return all(
        left_record.to_canonical_dict() == right_record.to_canonical_dict()
        for left_record, right_record in zip(left.records, right.records, strict=True)
    )


def _normalize_interval(value: str | None) -> str:
    if value is None or value == NA:
        return ""
    return str(value).strip().lower()


def _int_timestamp(candle: Any) -> int | None:
    for name in ("timestamp", "open_timestamp", "opened_at"):
        raw = _field(candle, name)
        if _is_absent(raw) or raw == NA:
            continue
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, Decimal):
            return int(raw)
        if isinstance(raw, str):
            try:
                return int(raw)
            except ValueError:
                continue
    return None


def _exact_membership_indices(
    parent_sequence: Sequence[Any],
    selected: Sequence[Any],
) -> tuple[int, ...] | None:
    used: set[int] = set()
    indices: list[int] = []
    for child in selected:
        found: int | None = None
        for index, parent in enumerate(parent_sequence):
            if index in used:
                continue
            if parent is child:
                found = index
                break
        if found is None:
            return None
        used.add(found)
        indices.append(found)
    return tuple(indices)


def _projection_equivalent_indices(
    parent_sequence: Sequence[Any],
    selected: Sequence[Any],
) -> tuple[int, ...] | None:
    used: set[int] = set()
    indices: list[int] = []
    parent_projections = [_project_candle(item) for item in parent_sequence]
    for child in selected:
        child_record = _project_candle(child)
        found: int | None = None
        for index, parent_record in enumerate(parent_projections):
            if index in used:
                continue
            if (
                not child_record.representation_unsupported
                and parent_record.to_canonical_dict() == child_record.to_canonical_dict()
            ):
                found = index
                break
        if found is None:
            return None
        used.add(found)
        indices.append(found)
    return tuple(indices)


def _infer_2d_source_pairs(
    parent_sequence: Sequence[Any],
    output_sequence: Sequence[Any],
) -> tuple[tuple[tuple[int, int], ...] | None, bool]:
    index_by_ts: dict[int, int] = {}
    for index, candle in enumerate(parent_sequence):
        timestamp = _int_timestamp(candle)
        if timestamp is not None:
            index_by_ts[timestamp] = index
    pairs: list[tuple[int, int]] = []
    complete = True
    for candle in output_sequence:
        timestamp = _int_timestamp(candle)
        if timestamp is None:
            complete = False
            continue
        first = index_by_ts.get(timestamp)
        second = index_by_ts.get(timestamp + DAY_MS)
        if first is None or second is None:
            complete = False
            continue
        pairs.append((first, second))
    if len(pairs) != len(output_sequence):
        complete = False
    if not pairs and output_sequence:
        return None, False
    return tuple(pairs), complete
