"""Deterministic typed codec for one outcome-evaluator invocation.

Existing candle projection fingerprints are not this codec. Equal projection
hashes do not identify an invocation, and this codec does not replace them.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.lifecycle.models import SetupLifecycleState, SetupTransitionReason

CODEC_HASH_PREFIX = "sha256:"


class CodecError(ValueError):
    """A value cannot be encoded or decoded without inventing information."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_ENUMS: dict[str, type[Enum]] = {
    "app.lifecycle.models.SetupLifecycleState": SetupLifecycleState,
    "app.lifecycle.models.SetupTransitionReason": SetupTransitionReason,
}


def content_hash(payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    return f"{CODEC_HASH_PREFIX}{digest}"


def encode_canonical(value: Any) -> bytes:
    try:
        tagged = _tag(value)
    except CodecError:
        raise
    except Exception as exc:
        raise CodecError(f"encode_failed:{type(exc).__name__}") from exc
    try:
        text = json.dumps(tagged, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise CodecError(f"canonical_json_failed:{type(exc).__name__}") from exc
    return text.encode("utf-8")


def decode_canonical(payload: bytes) -> Any:
    if not isinstance(payload, (bytes, bytearray)):
        raise CodecError("payload_must_be_bytes")
    try:
        tagged = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CodecError(f"payload_json_invalid:{type(exc).__name__}") from exc
    try:
        return _untag(tagged)
    except CodecError:
        raise
    except Exception as exc:
        raise CodecError(f"decode_failed:{type(exc).__name__}") from exc


def _tag(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("non_finite_float")
        return {"t": "float", "v": repr(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CodecError("non_finite_decimal")
        parts = value.as_tuple()
        digits = "".join(str(digit) for digit in parts.digits) or "0"
        return {"t": "decimal", "s": int(parts.sign), "d": digits, "e": int(parts.exponent)}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return {"t": "datetime", "naive": True, "v": value.isoformat()}
        return {"t": "datetime", "naive": False, "v": value.isoformat()}
    if isinstance(value, Enum):
        key = f"{type(value).__module__}.{type(value).__qualname__}"
        if key not in _ENUMS:
            raise CodecError(f"unsupported_enum:{key}")
        return {"t": "enum", "m": key, "v": value.value}
    if isinstance(value, tuple):
        return {"t": "tuple", "v": [_tag(item) for item in value]}
    if isinstance(value, list):
        return {"t": "list", "v": [_tag(item) for item in value]}
    if isinstance(value, Mapping):
        encoded: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CodecError("mapping_key_must_be_str")
            if key in encoded:
                raise CodecError("duplicate_mapping_key")
            encoded[key] = _tag(item)
        return {"t": "dict", "v": encoded}
    raise CodecError(f"unsupported_type:{type(value).__module__}.{type(value).__qualname__}")


def _untag(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, bool) or value is None or isinstance(value, str):
            if not isinstance(value, dict):
                # Bare JSON scalars are not a supported codec value. Every
                # encoded leaf is null or a tagged object, except that JSON
                # null represents None.
                if value is None:
                    return None
                raise CodecError("untagged_scalar")
        raise CodecError("untagged_scalar")
    if not isinstance(value, dict):
        raise CodecError("tagged_value_must_be_object_or_null")
    kind = value.get("t")
    if kind == "bool":
        if not isinstance(value.get("v"), bool):
            raise CodecError("bool_tag_mismatch")
        return value["v"]
    if kind == "int":
        text = value.get("v")
        if not isinstance(text, str) or text in {"", "+", "-"}:
            raise CodecError("int_tag_mismatch")
        try:
            return int(text, 10)
        except ValueError as exc:
            raise CodecError("int_tag_mismatch") from exc
    if kind == "float":
        text = value.get("v")
        if not isinstance(text, str):
            raise CodecError("float_tag_mismatch")
        try:
            number = float(text)
        except ValueError as exc:
            raise CodecError("float_tag_mismatch") from exc
        if not math.isfinite(number):
            raise CodecError("non_finite_float")
        return number
    if kind == "decimal":
        sign = value.get("s")
        digits = value.get("d")
        exponent = value.get("e")
        if sign not in {0, 1} or not isinstance(digits, str) or not digits.isdigit():
            raise CodecError("decimal_tag_mismatch")
        if isinstance(exponent, bool) or not isinstance(exponent, int):
            raise CodecError("decimal_tag_mismatch")
        return Decimal((sign, tuple(int(char) for char in digits), exponent))
    if kind == "str":
        text = value.get("v")
        if not isinstance(text, str):
            raise CodecError("str_tag_mismatch")
        return text
    if kind == "datetime":
        text = value.get("v")
        naive = value.get("naive")
        if not isinstance(text, str) or not isinstance(naive, bool):
            raise CodecError("datetime_tag_mismatch")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise CodecError("datetime_tag_mismatch") from exc
        if naive and parsed.tzinfo is not None:
            raise CodecError("datetime_naive_mismatch")
        if not naive and parsed.tzinfo is None:
            raise CodecError("datetime_aware_mismatch")
        return parsed
    if kind == "enum":
        key = value.get("m")
        raw = value.get("v")
        enum_type = _ENUMS.get(key) if isinstance(key, str) else None
        if enum_type is None:
            raise CodecError("unsupported_enum")
        try:
            return enum_type(raw)
        except ValueError as exc:
            raise CodecError("enum_value_mismatch") from exc
    if kind == "list":
        items = value.get("v")
        if not isinstance(items, list):
            raise CodecError("list_tag_mismatch")
        return [_untag(item) for item in items]
    if kind == "tuple":
        items = value.get("v")
        if not isinstance(items, list):
            raise CodecError("tuple_tag_mismatch")
        return tuple(_untag(item) for item in items)
    if kind == "dict":
        items = value.get("v")
        if not isinstance(items, dict):
            raise CodecError("dict_tag_mismatch")
        return {str(key): _untag(item) for key, item in items.items()}
    raise CodecError(f"unknown_tag:{kind}")


def mapping_pairs(value: Mapping[Any, Any]) -> list[dict[str, Any]]:
    """Preserve present keys in order. Absent keys are not invented."""

    pairs: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise CodecError("candle_key_unsupported")
        pairs.append({"k": str(key), "v": _tag(item)})
    return pairs


def pairs_to_dict(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    restored: dict[str, Any] = {}
    for pair in pairs:
        key = pair.get("k")
        if not isinstance(key, str) or key in restored:
            raise CodecError("candle_key_invalid")
        restored[key] = _untag(pair.get("v"))
    return restored
