"""Shared observation record. Missing evidence is never stored as zero."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from research.market_data_foundation.constants import CCI_MARK, DISPOSITIONS

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|authorization|password|signature)", re.I)
_SECRET_VALUE = re.compile(r"(?i)(bearer\s+\S+|x-cmc[_-][a-z0-9-]*api[_-]?key)")


@dataclass(frozen=True)
class Observation:
    evidence_id: str
    run_id: str
    code_sha: str
    policy_digest: str
    provider: str
    endpoint: str
    request_id: str
    parameters: dict[str, Any]
    venue: str
    instrument_id: str
    asset_id: str
    native_value: str | None
    native_unit: str
    normalized_value: str | None
    normalized_unit: str
    timestamps: dict[str, str | None]
    interval_semantics: str
    cache_status: str
    lineage: str
    response_status: str
    latency_ms: int | None
    retry_count: int
    request_weight: str | None
    raw_sha256: str | None
    validity: str
    reason_code: str
    dimensions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.validity not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {self.validity}")
        if self.validity == "missing" and self.normalized_value in {"0", "0.0"}:
            raise ValueError("missing evidence must not be normalized to zero")

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cci_mark"] = CCI_MARK[self.validity]
        payload["parameters"] = sanitize(self.parameters)
        return payload


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                cleaned[str(key)] = "[redacted]"
            else:
                cleaned[str(key)] = sanitize(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[redacted]", value)
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(sanitize(payload), sort_keys=True, separators=(",", ":"), default=str)


def digest(payload: Any) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))
