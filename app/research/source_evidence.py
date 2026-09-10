"""Immutable source-evidence claim contract (SOURCE_EVIDENCE_BOUNDARY).

Contract-only library. Describes declared evaluation purpose, declared input
origin, delivery path, and temporal claims for one market-data batch. It does
not capture acquisition, bind producers, persist evidence, mint source IDs, or
admit episodes.

A structurally valid descriptor is always UNBOUND. Validation certifies shape,
not historical truth, original possession, authentic venue receipt, or
admission. Tests may observe producer facts in a synthetic harness; those
observations are not an owned acquisition evidence chain.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final

CONTRACT_FORMAT_VERSION: Final[str] = "cci-source-evidence-contract-v1"
BINDING_STATUS_UNBOUND: Final[str] = "unbound"

PURPOSE_SCANNER_OBSERVATION: Final[str] = "scanner_observation"
PURPOSE_REPLAY_EXPERIMENT: Final[str] = "replay_experiment"
PURPOSE_DIRECT_EVALUATION_RECONSTRUCTION: Final[str] = "direct_evaluation_reconstruction"
PURPOSE_UNSPECIFIED: Final[str] = "unspecified"

ORIGIN_EXTERNALLY_SUPPLIED: Final[str] = "externally_supplied"
ORIGIN_SYNTHETIC_FIXTURE: Final[str] = "synthetic_fixture"
ORIGIN_HISTORICAL_RECONSTRUCTION: Final[str] = "historical_reconstruction"
ORIGIN_ACQUISITION_CLAIM: Final[str] = "acquisition_claim"

DELIVERY_DIRECT_SUPPLIED: Final[str] = "direct_supplied_input"
DELIVERY_ADAPTER_RETURN: Final[str] = "adapter_return_path"
DELIVERY_CACHE_RETURN: Final[str] = "cache_return_path"
DELIVERY_TRANSFORMED_RESAMPLED: Final[str] = "transformed_resampled_input"
DELIVERY_UNKNOWN: Final[str] = "unknown"

CACHE_KIND_MISS: Final[str] = "miss"
CACHE_KIND_HIT: Final[str] = "hit"
CACHE_KIND_EXPIRY_REFETCH: Final[str] = "expiry_refetch"
CACHE_KIND_UNAVAILABLE: Final[str] = "unavailable"
CACHE_KIND_NOT_APPLICABLE: Final[str] = "not_applicable"

TRANSFORMATION_SYNTHETIC_2D_RESAMPLE: Final[str] = "synthetic_2d_resample"
TRANSFORMATION_NOT_APPLICABLE: Final[str] = "not_applicable"

CLAIM_DECLARED: Final[str] = "declared"
CLAIM_UNAVAILABLE: Final[str] = "unavailable"
CLAIM_NOT_APPLICABLE: Final[str] = "not_applicable"

SUPPORT_UNVERIFIED_DECLARATION: Final[str] = "unverified_declaration"
SUPPORT_UNAVAILABLE: Final[str] = "unavailable"
SUPPORT_NOT_APPLICABLE: Final[str] = "not_applicable"

TEMPORAL_CANDLE_OPEN: Final[str] = "candle_open_time"
TEMPORAL_CANDLE_CLOSE: Final[str] = "candle_close_time"
TEMPORAL_LOGICAL_DECISION_CUTOFF: Final[str] = "logical_decision_cutoff"
TEMPORAL_PROCESSING_TIME: Final[str] = "processing_time"
TEMPORAL_CACHE_BOOKKEEPING: Final[str] = "cache_bookkeeping_time"
TEMPORAL_DECLARED_ACQUISITION_OBSERVATION: Final[str] = "declared_acquisition_observation"

REPRESENTATION_UTC_ISO8601: Final[str] = "utc_iso8601"
REPRESENTATION_EPOCH_MS: Final[str] = "epoch_ms"

_EVALUATION_PURPOSES: Final[frozenset[str]] = frozenset(
    {
        PURPOSE_SCANNER_OBSERVATION,
        PURPOSE_REPLAY_EXPERIMENT,
        PURPOSE_DIRECT_EVALUATION_RECONSTRUCTION,
        PURPOSE_UNSPECIFIED,
    }
)
_INPUT_ORIGINS: Final[frozenset[str]] = frozenset(
    {
        ORIGIN_EXTERNALLY_SUPPLIED,
        ORIGIN_SYNTHETIC_FIXTURE,
        ORIGIN_HISTORICAL_RECONSTRUCTION,
        ORIGIN_ACQUISITION_CLAIM,
    }
)
_DELIVERY_PATHS: Final[frozenset[str]] = frozenset(
    {
        DELIVERY_DIRECT_SUPPLIED,
        DELIVERY_ADAPTER_RETURN,
        DELIVERY_CACHE_RETURN,
        DELIVERY_TRANSFORMED_RESAMPLED,
        DELIVERY_UNKNOWN,
    }
)
_CACHE_KINDS: Final[frozenset[str]] = frozenset(
    {
        CACHE_KIND_MISS,
        CACHE_KIND_HIT,
        CACHE_KIND_EXPIRY_REFETCH,
        CACHE_KIND_UNAVAILABLE,
        CACHE_KIND_NOT_APPLICABLE,
    }
)
_TRANSFORMATION_KINDS: Final[frozenset[str]] = frozenset(
    {TRANSFORMATION_SYNTHETIC_2D_RESAMPLE, TRANSFORMATION_NOT_APPLICABLE}
)
_CLAIM_STATUSES: Final[frozenset[str]] = frozenset(
    {CLAIM_DECLARED, CLAIM_UNAVAILABLE, CLAIM_NOT_APPLICABLE}
)
_TEMPORAL_KINDS: Final[tuple[str, ...]] = (
    TEMPORAL_CANDLE_OPEN,
    TEMPORAL_CANDLE_CLOSE,
    TEMPORAL_LOGICAL_DECISION_CUTOFF,
    TEMPORAL_PROCESSING_TIME,
    TEMPORAL_CACHE_BOOKKEEPING,
    TEMPORAL_DECLARED_ACQUISITION_OBSERVATION,
)
_LABEL_KEYS: Final[tuple[str, ...]] = (
    "venue_label",
    "adapter_class_label",
    "config_exchange_label",
)
_REPRESENTATIONS: Final[frozenset[str]] = frozenset(
    {REPRESENTATION_UTC_ISO8601, REPRESENTATION_EPOCH_MS}
)
_REQUIRED_ACQUISITION_LIMITATIONS: Final[tuple[str, ...]] = (
    "local_completion_is_not_venue_publication_time",
    "class_identity_is_not_venue_proof",
    "config_exchange_is_not_provenance",
    "cutoff_is_not_possession",
)
_OPTIONAL_ACQUISITION_LIMITATIONS: Final[frozenset[str]] = frozenset(
    {
        "injected_transport_permitted",
        "cache_may_return_prior_batch",
        "normalized_exchange_field_is_not_receipt",
        "legacy_cache_entry_lacks_acquisition_observation",
    }
)
_ALLOWED_ACQUISITION_LIMITATIONS: Final[frozenset[str]] = frozenset(_REQUIRED_ACQUISITION_LIMITATIONS) | (
    _OPTIONAL_ACQUISITION_LIMITATIONS
)
_TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (
    "contract_format_version",
    "evaluation_purpose",
    "input_origin",
    "delivery_path",
    "cache_delivery_kind",
    "transformation_kind",
    "venue_label",
    "adapter_class_label",
    "config_exchange_label",
    "acquisition_claim_limitations",
    "temporal_claims",
)
_FORBIDDEN_AUTHORITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authoritative",
        "source_verified",
        "complete",
        "witnessed",
        "bound",
        "verified",
        "source_id",
        "source_namespace",
        "policy_id",
        "evaluation_policy_id",
        "admission_id",
        "evaluation_episode_id",
        "binding_status",
        "factual_support",
    }
)
_FORBIDDEN_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"unknown", "n/a", "na", "unresolved", "tbd", "placeholder"}
)
_CLAIM_VALUE_KEYS: Final[tuple[str, ...]] = ("status", "value")
_TEMPORAL_VALUE_KEYS: Final[tuple[str, ...]] = ("status", "value", "representation")


class SourceEvidenceError(ValueError):
    """Raised when a source-evidence payload is incomplete, unknown, or malformed."""


@dataclass(frozen=True, slots=True)
class SourceClaim:
    """One declared, unavailable, or not-applicable scalar claim."""

    status: str
    value: str | None


@dataclass(frozen=True, slots=True)
class TemporalClaim:
    """One named temporal claim. Declared values remain unverified declarations."""

    status: str
    value: str | int | None
    representation: str | None


@dataclass(frozen=True, slots=True)
class SourceEvidenceDescriptor:
    """Validated immutable source-claim contract. Always unbound."""

    payload: Mapping[str, Any]
    canonical_bytes: bytes

    @property
    def binding_status(self) -> str:
        return BINDING_STATUS_UNBOUND

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return a defensive JSON-round-trip copy of the validated payload."""

        loaded = json.loads(self.canonical_bytes.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise SourceEvidenceError("canonical_bytes_must_decode_to_object")
        return loaded


@dataclass(frozen=True, slots=True)
class SourceEvidenceAssessment:
    """Factual-support assessment. Never upgrades a declaration into witnessed fact."""

    binding_status: str
    structural_validity: str
    factual_support: Mapping[str, str]
    witnessed_acquisition_chain: str
    possession_at_logical_cutoff: str
    authentic_venue_provenance: str
    admission_authority: str
    original_historical_availability: str
    claim_conflicts: tuple[str, ...]
    limitation_codes: tuple[str, ...]


def build_source_evidence_descriptor(
    *,
    evaluation_purpose: str,
    input_origin: str,
    delivery_path: str,
    cache_delivery_kind: str | None = None,
    transformation_kind: str | None = None,
    venue_label: str | None = None,
    adapter_class_label: str | None = None,
    config_exchange_label: str | None = None,
    acquisition_claim_limitations: Sequence[str] = (),
    temporal_claims: Mapping[str, Any] | None = None,
) -> SourceEvidenceDescriptor:
    """Build a structurally valid unbound descriptor from named claims."""

    resolved_cache_kind = cache_delivery_kind
    if resolved_cache_kind is None:
        resolved_cache_kind = (
            CACHE_KIND_UNAVAILABLE if delivery_path == DELIVERY_CACHE_RETURN else CACHE_KIND_NOT_APPLICABLE
        )
    if transformation_kind is None:
        if delivery_path == DELIVERY_TRANSFORMED_RESAMPLED:
            raise SourceEvidenceError("missing_field:transformation_kind")
        resolved_transformation = TRANSFORMATION_NOT_APPLICABLE
    else:
        resolved_transformation = transformation_kind
    payload = {
        "contract_format_version": CONTRACT_FORMAT_VERSION,
        "evaluation_purpose": evaluation_purpose,
        "input_origin": input_origin,
        "delivery_path": delivery_path,
        "cache_delivery_kind": resolved_cache_kind,
        "transformation_kind": resolved_transformation,
        "venue_label": _claim_from_optional_text(venue_label),
        "adapter_class_label": _claim_from_optional_text(adapter_class_label),
        "config_exchange_label": _claim_from_optional_text(config_exchange_label),
        "acquisition_claim_limitations": list(acquisition_claim_limitations),
        "temporal_claims": _default_temporal_claims(delivery_path, temporal_claims),
    }
    return parse_source_evidence_payload(payload)


def parse_source_evidence_payload(payload: Mapping[str, Any]) -> SourceEvidenceDescriptor:
    """Validate a source-claim payload. Success certifies structure only."""

    if not isinstance(payload, Mapping):
        raise SourceEvidenceError("payload_must_be_a_mapping")
    _reject_forbidden_authority_keys(payload)
    _reject_unknown_keys(payload, _TOP_LEVEL_KEYS, path="")
    for key in _TOP_LEVEL_KEYS:
        if key not in payload:
            raise SourceEvidenceError(f"missing_field:{key}")
    format_version = _require_token(
        payload["contract_format_version"],
        path="contract_format_version",
        allowed={CONTRACT_FORMAT_VERSION},
        allow_unknown_token=False,
    )
    purpose = _require_token(
        payload["evaluation_purpose"],
        path="evaluation_purpose",
        allowed=_EVALUATION_PURPOSES,
        allow_unknown_token=False,
    )
    origin = _require_token(
        payload["input_origin"],
        path="input_origin",
        allowed=_INPUT_ORIGINS,
        allow_unknown_token=False,
    )
    delivery = _require_token(
        payload["delivery_path"],
        path="delivery_path",
        allowed=_DELIVERY_PATHS,
        allow_unknown_token=True,
    )
    cache_kind = _require_token(
        payload["cache_delivery_kind"],
        path="cache_delivery_kind",
        allowed=_CACHE_KINDS,
        allow_unknown_token=False,
    )
    transformation = _require_token(
        payload["transformation_kind"],
        path="transformation_kind",
        allowed=_TRANSFORMATION_KINDS,
        allow_unknown_token=False,
    )
    _validate_delivery_cross_fields(delivery=delivery, cache_kind=cache_kind, transformation=transformation)
    labels = {
        key: _validate_label_claim(payload[key], path=key)
        for key in _LABEL_KEYS
    }
    limitations = _validate_acquisition_limitations(
        payload["acquisition_claim_limitations"],
        origin=origin,
    )
    temporal = _validate_temporal_claims(payload["temporal_claims"], delivery_path=delivery)
    validated = {
        "contract_format_version": format_version,
        "evaluation_purpose": purpose,
        "input_origin": origin,
        "delivery_path": delivery,
        "cache_delivery_kind": cache_kind,
        "transformation_kind": transformation,
        "venue_label": labels["venue_label"],
        "adapter_class_label": labels["adapter_class_label"],
        "config_exchange_label": labels["config_exchange_label"],
        "acquisition_claim_limitations": limitations,
        "temporal_claims": temporal,
    }
    frozen = _freeze(validated)
    return SourceEvidenceDescriptor(
        payload=frozen,
        canonical_bytes=_canonical_json_bytes(validated),
    )


def assess_source_evidence(descriptor: SourceEvidenceDescriptor) -> SourceEvidenceAssessment:
    """Assess factual support. Never certifies possession, venue authenticity, or admission."""

    if not isinstance(descriptor, SourceEvidenceDescriptor):
        raise SourceEvidenceError("descriptor_must_be_source_evidence_descriptor")
    payload = descriptor.to_canonical_dict()
    support = _factual_support_map(payload)
    conflicts = _claim_conflicts(payload)
    return SourceEvidenceAssessment(
        binding_status=BINDING_STATUS_UNBOUND,
        structural_validity="valid",
        factual_support=MappingProxyType(support),
        witnessed_acquisition_chain=SUPPORT_UNAVAILABLE,
        possession_at_logical_cutoff=SUPPORT_UNAVAILABLE,
        authentic_venue_provenance=SUPPORT_UNAVAILABLE,
        admission_authority=SUPPORT_UNAVAILABLE,
        original_historical_availability=SUPPORT_UNAVAILABLE,
        claim_conflicts=conflicts,
        limitation_codes=_limitation_codes(payload, conflicts),
    )


def _claim_from_optional_text(value: str | None) -> dict[str, Any]:
    if value is None:
        return {"status": CLAIM_UNAVAILABLE, "value": None}
    return {"status": CLAIM_DECLARED, "value": value}


def _default_temporal_claims(delivery_path: str, supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    defaults = {
        kind: _empty_temporal(
            CLAIM_NOT_APPLICABLE if kind == TEMPORAL_CACHE_BOOKKEEPING and delivery_path != DELIVERY_CACHE_RETURN else CLAIM_UNAVAILABLE
        )
        for kind in _TEMPORAL_KINDS
    }
    if supplied is None:
        return defaults
    if not isinstance(supplied, Mapping):
        raise SourceEvidenceError("temporal_claims_must_be_object")
    merged = dict(defaults)
    merged.update(dict(supplied))
    return merged


def _empty_temporal(status: str) -> dict[str, Any]:
    return {"status": status, "value": None, "representation": None}


def _validate_delivery_cross_fields(*, delivery: str, cache_kind: str, transformation: str) -> None:
    if delivery == DELIVERY_CACHE_RETURN:
        if cache_kind == CACHE_KIND_NOT_APPLICABLE:
            raise SourceEvidenceError("cache_delivery_kind_required_for_cache_return_path")
    elif cache_kind != CACHE_KIND_NOT_APPLICABLE:
        raise SourceEvidenceError("cache_delivery_kind_not_applicable_off_cache_path")
    if delivery == DELIVERY_TRANSFORMED_RESAMPLED:
        if transformation == TRANSFORMATION_NOT_APPLICABLE:
            raise SourceEvidenceError("transformation_kind_required_for_transformed_path")
    elif transformation != TRANSFORMATION_NOT_APPLICABLE:
        raise SourceEvidenceError("transformation_kind_not_applicable_off_transformed_path")


def _validate_label_claim(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceEvidenceError(f"invalid_claim_object:{path}")
    _reject_forbidden_authority_keys(value, path=path)
    _reject_unknown_keys(value, _CLAIM_VALUE_KEYS, path=path)
    status = _require_token(value.get("status"), path=f"{path}.status", allowed=_CLAIM_STATUSES, allow_unknown_token=False)
    raw = value.get("value")
    if status == CLAIM_DECLARED:
        if not isinstance(raw, str):
            raise SourceEvidenceError(f"declared_label_must_be_string:{path}")
        text = raw.strip()
        if not text:
            raise SourceEvidenceError(f"declared_label_empty:{path}")
        _reject_placeholder(text, path=f"{path}.value")
        return {"status": status, "value": text}
    if raw is not None:
        raise SourceEvidenceError(f"non_declared_claim_must_have_null_value:{path}")
    return {"status": status, "value": None}


def _validate_acquisition_limitations(value: Any, *, origin: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SourceEvidenceError("acquisition_claim_limitations_must_be_string_list")
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise SourceEvidenceError(f"invalid_string_list:acquisition_claim_limitations[{index}]")
        token = item.strip()
        _reject_placeholder(token, path=f"acquisition_claim_limitations[{index}]")
        if token not in _ALLOWED_ACQUISITION_LIMITATIONS:
            raise SourceEvidenceError(f"unknown_value:acquisition_claim_limitations[{index}]")
        if token in seen:
            raise SourceEvidenceError("duplicate_acquisition_claim_limitation")
        seen.add(token)
        items.append(token)
    if origin == ORIGIN_ACQUISITION_CLAIM:
        missing = [token for token in _REQUIRED_ACQUISITION_LIMITATIONS if token not in seen]
        if missing:
            raise SourceEvidenceError("acquisition_claim_missing_required_limitations")
    elif items:
        raise SourceEvidenceError("acquisition_claim_limitations_only_valid_for_acquisition_claim")
    return items


def _validate_temporal_claims(value: Any, *, delivery_path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceEvidenceError("temporal_claims_must_be_object")
    _reject_forbidden_authority_keys(value, path="temporal_claims")
    _reject_unknown_keys(value, _TEMPORAL_KINDS, path="temporal_claims")
    validated: dict[str, Any] = {}
    for kind in _TEMPORAL_KINDS:
        if kind not in value:
            raise SourceEvidenceError(f"missing_field:temporal_claims.{kind}")
        claim = _validate_temporal_claim(value[kind], path=f"temporal_claims.{kind}")
        if kind == TEMPORAL_CACHE_BOOKKEEPING:
            if delivery_path == DELIVERY_CACHE_RETURN:
                if claim["status"] == CLAIM_NOT_APPLICABLE:
                    raise SourceEvidenceError("cache_bookkeeping_not_applicable_on_cache_path")
            elif claim["status"] != CLAIM_NOT_APPLICABLE:
                raise SourceEvidenceError("cache_bookkeeping_only_applicable_on_cache_path")
        validated[kind] = claim
    return validated


def _validate_temporal_claim(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceEvidenceError(f"invalid_claim_object:{path}")
    _reject_forbidden_authority_keys(value, path=path)
    _reject_unknown_keys(value, _TEMPORAL_VALUE_KEYS, path=path)
    status = _require_token(value.get("status"), path=f"{path}.status", allowed=_CLAIM_STATUSES, allow_unknown_token=False)
    raw = value.get("value")
    representation = value.get("representation")
    if status != CLAIM_DECLARED:
        if raw is not None or representation is not None:
            raise SourceEvidenceError(f"non_declared_temporal_must_have_null_value:{path}")
        return {"status": status, "value": None, "representation": None}
    token = _require_token(
        representation,
        path=f"{path}.representation",
        allowed=_REPRESENTATIONS,
        allow_unknown_token=False,
    )
    if token == REPRESENTATION_UTC_ISO8601:
        if not isinstance(raw, str):
            raise SourceEvidenceError(f"utc_iso8601_must_be_string:{path}")
        text = raw.strip()
        if not text:
            raise SourceEvidenceError(f"declared_temporal_empty:{path}")
        _reject_placeholder(text, path=f"{path}.value")
        _require_utc_iso8601(text, path=path)
        return {"status": status, "value": text, "representation": token}
    if isinstance(raw, bool) or type(raw) is not int:
        raise SourceEvidenceError(f"epoch_ms_must_be_int:{path}")
    if raw < 0:
        raise SourceEvidenceError(f"epoch_ms_must_be_non_negative:{path}")
    return {"status": status, "value": raw, "representation": token}


def _require_utc_iso8601(value: str, *, path: str) -> None:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SourceEvidenceError(f"invalid_utc_iso8601:{path}") from exc
    if parsed.tzinfo is None:
        raise SourceEvidenceError(f"naive_datetime_forbidden:{path}")
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise SourceEvidenceError(f"non_utc_datetime_forbidden:{path}")


def _factual_support_map(payload: Mapping[str, Any]) -> dict[str, str]:
    support = {
        "evaluation_purpose": SUPPORT_UNVERIFIED_DECLARATION,
        "input_origin": SUPPORT_UNVERIFIED_DECLARATION,
        "delivery_path": (
            SUPPORT_UNAVAILABLE if payload["delivery_path"] == DELIVERY_UNKNOWN else SUPPORT_UNVERIFIED_DECLARATION
        ),
        "cache_delivery_kind": _token_support(payload["cache_delivery_kind"]),
        "transformation_kind": _token_support(payload["transformation_kind"]),
        "acquisition_claim_limitations": (
            SUPPORT_UNVERIFIED_DECLARATION
            if payload["input_origin"] == ORIGIN_ACQUISITION_CLAIM
            else SUPPORT_NOT_APPLICABLE
        ),
    }
    for key in _LABEL_KEYS:
        support[key] = _claim_support(payload[key]["status"])
    for kind in _TEMPORAL_KINDS:
        support[f"temporal_claims.{kind}"] = _claim_support(payload["temporal_claims"][kind]["status"])
    return support


def _token_support(token: str) -> str:
    if token == "not_applicable":
        return SUPPORT_NOT_APPLICABLE
    if token == "unavailable" or token == DELIVERY_UNKNOWN:
        return SUPPORT_UNAVAILABLE
    return SUPPORT_UNVERIFIED_DECLARATION


def _claim_support(status: str) -> str:
    if status == CLAIM_DECLARED:
        return SUPPORT_UNVERIFIED_DECLARATION
    if status == CLAIM_NOT_APPLICABLE:
        return SUPPORT_NOT_APPLICABLE
    return SUPPORT_UNAVAILABLE


def _claim_conflicts(payload: Mapping[str, Any]) -> tuple[str, ...]:
    conflicts: list[str] = []
    venue = payload["venue_label"]
    config_exchange = payload["config_exchange_label"]
    if (
        venue["status"] == CLAIM_DECLARED
        and config_exchange["status"] == CLAIM_DECLARED
        and venue["value"].casefold() != config_exchange["value"].casefold()
    ):
        conflicts.append("venue_label_differs_from_config_exchange_label")
    return tuple(conflicts)


def _limitation_codes(payload: Mapping[str, Any], conflicts: Sequence[str]) -> tuple[str, ...]:
    codes = [
        "validation_certifies_structure_only",
        "descriptor_remains_unbound",
        "no_owned_acquisition_evidence_chain",
        "logical_cutoff_is_not_possession",
        "cache_bookkeeping_is_not_receipt",
        "labels_are_not_venue_provenance",
        "policy_id_is_not_source_authority",
        "equal_declared_fields_are_not_opportunity_identity",
        "local_completion_is_not_venue_publication_time",
        "class_identity_is_not_venue_proof",
        "config_exchange_is_not_provenance",
    ]
    if payload["cache_delivery_kind"] == CACHE_KIND_HIT:
        codes.append("cache_hit_is_not_fresh_exchange_observation")
    if payload["transformation_kind"] == TRANSFORMATION_SYNTHETIC_2D_RESAMPLE:
        codes.append("resample_is_not_new_venue_observation")
    if payload["input_origin"] == ORIGIN_SYNTHETIC_FIXTURE:
        codes.append("fixture_is_not_live_market_provenance")
    if payload["input_origin"] == ORIGIN_HISTORICAL_RECONSTRUCTION:
        codes.append("historical_reconstruction_is_not_original_possession")
    if payload["delivery_path"] == DELIVERY_UNKNOWN:
        codes.append("delivery_path_unavailable")
    if payload["evaluation_purpose"] == PURPOSE_UNSPECIFIED:
        codes.append("invocation_purpose_unspecified")
    if payload["cache_delivery_kind"] == CACHE_KIND_UNAVAILABLE:
        codes.append("legacy_cache_entry_lacks_acquisition_observation")
    if payload["temporal_claims"][TEMPORAL_DECLARED_ACQUISITION_OBSERVATION]["status"] == CLAIM_DECLARED:
        codes.append("declared_acquisition_observation_is_unverified")
    codes.extend(conflicts)
    return tuple(codes)


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    ready = _json_ready(payload)
    try:
        text = json.dumps(
            ready,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SourceEvidenceError("canonical_json_rejected") from exc
    return text.encode("utf-8")


def _require_token(value: Any, *, path: str, allowed: set[str] | frozenset[str], allow_unknown_token: bool) -> str:
    if not isinstance(value, str):
        raise SourceEvidenceError(f"invalid_token:{path}")
    token = value.strip()
    if not token:
        raise SourceEvidenceError(f"invalid_token:{path}")
    if token not in allowed:
        # delivery_path uses the reserved token "unknown"; other free-text
        # placeholders remain forbidden.
        if not allow_unknown_token or token.casefold() != DELIVERY_UNKNOWN:
            _reject_placeholder(token, path=path)
        raise SourceEvidenceError(f"unknown_value:{path}")
    if token != DELIVERY_UNKNOWN:
        _reject_placeholder(token, path=path)
    return token


def _reject_unknown_keys(value: Mapping[str, Any], allowed: Sequence[str], *, path: str) -> None:
    allowed_set = set(allowed)
    for key in value:
        location = f"{path}.{key}" if path else str(key)
        if not isinstance(key, str):
            raise SourceEvidenceError(f"non_string_key:{location}")
        if key not in allowed_set:
            raise SourceEvidenceError(f"unknown_field:{location}")


def _reject_forbidden_authority_keys(value: Mapping[str, Any], *, path: str = "") -> None:
    for key in value:
        if not isinstance(key, str):
            continue
        if key in _FORBIDDEN_AUTHORITY_KEYS or key.casefold() in _FORBIDDEN_AUTHORITY_KEYS:
            location = f"{path}.{key}" if path else key
            raise SourceEvidenceError(f"authority_escape_hatch_forbidden:{location}")


def _reject_placeholder(value: str, *, path: str) -> None:
    if value.strip().casefold() in _FORBIDDEN_PLACEHOLDERS:
        raise SourceEvidenceError(f"unresolved_placeholder:{path}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        raise SourceEvidenceError("non_finite_or_float_forbidden")
    raise SourceEvidenceError("unsupported_canonical_type")


__all__ = [
    "BINDING_STATUS_UNBOUND",
    "CACHE_KIND_EXPIRY_REFETCH",
    "CACHE_KIND_HIT",
    "CACHE_KIND_MISS",
    "CACHE_KIND_NOT_APPLICABLE",
    "CACHE_KIND_UNAVAILABLE",
    "CLAIM_DECLARED",
    "CLAIM_NOT_APPLICABLE",
    "CLAIM_UNAVAILABLE",
    "CONTRACT_FORMAT_VERSION",
    "DELIVERY_ADAPTER_RETURN",
    "DELIVERY_CACHE_RETURN",
    "DELIVERY_DIRECT_SUPPLIED",
    "DELIVERY_TRANSFORMED_RESAMPLED",
    "DELIVERY_UNKNOWN",
    "ORIGIN_ACQUISITION_CLAIM",
    "ORIGIN_EXTERNALLY_SUPPLIED",
    "ORIGIN_HISTORICAL_RECONSTRUCTION",
    "ORIGIN_SYNTHETIC_FIXTURE",
    "PURPOSE_DIRECT_EVALUATION_RECONSTRUCTION",
    "PURPOSE_REPLAY_EXPERIMENT",
    "PURPOSE_SCANNER_OBSERVATION",
    "PURPOSE_UNSPECIFIED",
    "REPRESENTATION_EPOCH_MS",
    "REPRESENTATION_UTC_ISO8601",
    "SUPPORT_NOT_APPLICABLE",
    "SUPPORT_UNAVAILABLE",
    "SUPPORT_UNVERIFIED_DECLARATION",
    "SourceClaim",
    "SourceEvidenceAssessment",
    "SourceEvidenceDescriptor",
    "SourceEvidenceError",
    "TEMPORAL_CACHE_BOOKKEEPING",
    "TEMPORAL_CANDLE_CLOSE",
    "TEMPORAL_CANDLE_OPEN",
    "TEMPORAL_DECLARED_ACQUISITION_OBSERVATION",
    "TEMPORAL_LOGICAL_DECISION_CUTOFF",
    "TEMPORAL_PROCESSING_TIME",
    "TRANSFORMATION_NOT_APPLICABLE",
    "TRANSFORMATION_SYNTHETIC_2D_RESAMPLE",
    "TemporalClaim",
    "assess_source_evidence",
    "build_source_evidence_descriptor",
    "parse_source_evidence_payload",
]
