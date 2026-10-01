"""Serialize an available candle-delivery envelope without strengthening it.

Missing adapter evidence stays missing. Parent links are followed only as far
as the declared depth and node budgets. A truncated walk is incomplete
evidence, not a reconstructed lineage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.data.candle_batch_evidence import (
    AdapterAcquisitionObservation,
    BatchHandoffAssociation,
    CacheDeliveryObservation,
    CandleBatchDelivery,
    CandleBatchSnapshot,
    CandleProjectionRecord,
    SelectionObservation,
    TransformObservation,
)
from app.research.durable_source_replay.bounds import BoundExceeded, BoundLimits
from app.research.durable_source_replay.codec import CodecError


def serialize_delivery(
    delivery: Any,
    handoff: Any,
    *,
    limits: BoundLimits,
) -> dict[str, Any]:
    state = _Walk(limits=limits)
    payload = {
        "delivery_present": delivery is not None,
        "handoff_present": handoff is not None,
        "delivery": None if delivery is None else state.delivery(delivery),
        "handoff": None if handoff is None else state.handoff(handoff),
        "parent_depth": state.depth,
        "node_count": state.nodes,
        "truncated": False,
        "truncation_reason": None,
    }
    if state.nodes > limits.max_reference_count:
        raise BoundExceeded("delivery_reference_limit")
    return payload


class _Walk:
    def __init__(self, *, limits: BoundLimits) -> None:
        self.limits = limits
        self.depth = 0
        self.nodes = 0

    def _enter(self, depth: int) -> int:
        self.nodes += 1
        if self.nodes > self.limits.max_delivery_nodes:
            raise BoundExceeded("delivery_node_limit")
        if depth > self.depth:
            self.depth = depth
        if depth > self.limits.max_parent_depth:
            raise BoundExceeded("parent_depth_limit")
        return depth

    def delivery(self, value: Any, *, depth: int = 0) -> dict[str, Any]:
        self._enter(depth)
        if not isinstance(value, CandleBatchDelivery):
            return {
                "kind": "unavailable",
                "reason": "envelope_type_unavailable",
                "type_name": type(value).__name__,
            }
        return {
            "kind": "candle_batch_delivery",
            "format_version": value.format_version,
            "occurrence_token": value.occurrence_token,
            "producer_boundary": value.producer_boundary,
            "disposition": value.disposition,
            "unavailable_reason": value.unavailable_reason,
            "limitations": list(value.limitations),
            "observed_at": value.observed_at,
            "request_symbol": value.request_symbol,
            "request_interval": value.request_interval,
            "request_limit": value.request_limit,
            "logical_cutoff": value.logical_cutoff,
            "lineage_complete": bool(value.lineage_complete),
            "returned_sequence": [_candle(item) for item in value.returned_sequence],
            "snapshot": self.snapshot(value.snapshot, depth=depth),
            "adapter": None if value.adapter is None else self.adapter(value.adapter, depth=depth),
            "cache": None if value.cache is None else self.cache(value.cache, depth=depth),
            "selection": None if value.selection is None else self.selection(value.selection, depth=depth),
            "transform": None if value.transform is None else self.transform(value.transform, depth=depth),
            "parent": None if value.parent is None else self.delivery(value.parent, depth=depth + 1),
        }

    def handoff(self, value: Any) -> dict[str, Any]:
        self._enter(0)
        if not isinstance(value, BatchHandoffAssociation):
            return {
                "kind": "unavailable",
                "reason": "handoff_type_unavailable",
                "type_name": type(value).__name__,
            }
        return {
            "kind": "batch_handoff",
            "disposition": value.disposition,
            "reason": value.reason,
            "execution_timeframe": value.execution_timeframe,
            "logical_cutoff": value.logical_cutoff,
            "matched_fingerprint": value.matched_fingerprint,
            "observed_at": value.observed_at,
            "execution_snapshot": None
            if value.execution_snapshot is None
            else self.snapshot(value.execution_snapshot, depth=0),
            "delivery": None if value.delivery is None else self.delivery(value.delivery, depth=0),
        }

    def snapshot(self, value: CandleBatchSnapshot, *, depth: int) -> dict[str, Any]:
        self._enter(depth)
        return {
            "format_version": value.format_version,
            "record_count": value.record_count,
            "included_fields": list(value.included_fields),
            "excluded_fields": list(value.excluded_fields),
            "unavailable": bool(value.unavailable),
            "unavailable_reason": value.unavailable_reason,
            "content_fingerprint": value.content_fingerprint,
            "records": [self.projection(record) for record in value.records],
        }

    def projection(self, record: CandleProjectionRecord) -> dict[str, Any]:
        return {
            "values": _raw_pairs(record.values),
            "missing_supported_fields": list(record.missing_supported_fields),
            "representation_unsupported": bool(record.representation_unsupported),
        }

    def adapter(self, value: AdapterAcquisitionObservation, *, depth: int) -> dict[str, Any]:
        self._enter(depth)
        return {
            "occurrence_token": value.occurrence_token,
            "requested_symbol": value.requested_symbol,
            "requested_interval": value.requested_interval,
            "requested_limit": value.requested_limit,
            "effective_symbol": value.effective_symbol,
            "effective_interval": value.effective_interval,
            "effective_limit": value.effective_limit,
            "endpoint_path": value.endpoint_path,
            "adapter_class_label": value.adapter_class_label,
            "observed_completed_at": value.observed_completed_at,
            "limitations": list(value.limitations),
            "http_client_injected": bool(value.http_client_injected),
            "base_url_label": value.base_url_label,
            "snapshot": self.snapshot(value.snapshot, depth=depth),
        }

    def cache(self, value: CacheDeliveryObservation, *, depth: int) -> dict[str, Any]:
        self._enter(depth)
        upstream = value.upstream_acquisition
        return {
            "occurrence_token": value.occurrence_token,
            "delivery_kind": value.delivery_kind,
            "cache_bookkeeping_created_at": value.cache_bookkeeping_created_at,
            "cache_expires_at": value.cache_expires_at,
            "observed_completed_at": value.observed_completed_at,
            "upstream_unavailable_reason": value.upstream_unavailable_reason,
            "limitations": list(value.limitations),
            "cache_enabled": bool(value.cache_enabled),
            "snapshot": self.snapshot(value.snapshot, depth=depth),
            "upstream_acquisition": None if upstream is None else self.adapter(upstream, depth=depth + 1),
        }

    def selection(self, value: SelectionObservation, *, depth: int) -> dict[str, Any]:
        self._enter(depth)
        return {
            "occurrence_token": value.occurrence_token,
            "kind": value.kind,
            "logical_cutoff": value.logical_cutoff,
            "selected_membership_indices": None
            if value.selected_membership_indices is None
            else list(value.selected_membership_indices),
            "membership_complete": bool(value.membership_complete),
            "projection_equivalent_indices": None
            if value.projection_equivalent_indices is None
            else list(value.projection_equivalent_indices),
            "output_snapshot": self.snapshot(value.output_snapshot, depth=depth),
        }

    def transform(self, value: TransformObservation, *, depth: int) -> dict[str, Any]:
        self._enter(depth)
        return {
            "occurrence_token": value.occurrence_token,
            "transformation_kind": value.transformation_kind,
            "target_interval": value.target_interval,
            "source_pairs": None if value.source_pairs is None else [list(pair) for pair in value.source_pairs],
            "mapping_complete": bool(value.mapping_complete),
            "output_snapshot": self.snapshot(value.output_snapshot, depth=depth),
        }


def _raw_pairs(value: Mapping[Any, Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise CodecError("candle_key_unsupported")
        pairs.append({"k": str(key), "v": item})
    return pairs


def _candle(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {"shape": "mapping", "fields": _raw_pairs(value)}
    fields = (
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
    present: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in fields:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            raise CodecError("candle_sequence_unsupported")
        if not hasattr(value, name):
            missing.append(name)
            continue
        present.append({"k": name, "v": getattr(value, name)})
    return {
        "shape": "object",
        "type_name": type(value).__name__,
        "fields": present,
        "missing_fields": missing,
        "reconstruction": "ordinary_field_projection",
    }
