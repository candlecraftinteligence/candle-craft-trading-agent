from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.dtos import NA, MaybeDecimal, MaybeInt
from app.data.exceptions import ExchangeMissingFieldError, ExchangeResponseError


def require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExchangeResponseError(f"Expected object at {path}")
    return value


def require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ExchangeResponseError(f"Expected list at {path}")
    return value


def require_field(container: Mapping[str, Any], field: str, path: str) -> Any:
    if field not in container or _is_missing(container[field]):
        raise ExchangeMissingFieldError(f"Missing required field {path}.{field}")
    return container[field]


def require_index(row: Sequence[Any], index: int, path: str) -> Any:
    if isinstance(row, (str, bytes)) or len(row) <= index or _is_missing(row[index]):
        raise ExchangeMissingFieldError(f"Missing required field {path}[{index}]")
    return row[index]


def decimal_from(value: Any, path: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExchangeResponseError(f"Invalid decimal at {path}: {value!r}") from exc


def int_from(value: Any, path: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ExchangeResponseError(f"Invalid integer at {path}: {value!r}") from exc


def optional_decimal(value: Any, path: str) -> MaybeDecimal:
    if _is_missing(value):
        return NA
    return decimal_from(value, path)


def optional_int(value: Any, path: str) -> MaybeInt:
    if _is_missing(value):
        return NA
    return int_from(value, path)


def first_item(items: list[Any], path: str) -> Any:
    if not items:
        raise ExchangeMissingFieldError(f"Missing required item at {path}[0]")
    return items[0]


def last_item(items: list[Any], path: str) -> Any:
    if not items:
        raise ExchangeMissingFieldError(f"Missing required item at {path}[-1]")
    return items[-1]


def _is_missing(value: Any) -> bool:
    return value is None or value == ""
