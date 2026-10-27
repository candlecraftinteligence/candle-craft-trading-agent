"""Read versioned HTF diagnostics without relabeling historical 2D evidence."""
from collections.abc import Mapping
from typing import Any

from app.data.dtos import NA


def htf_label(diagnostics: Mapping[str, Any]) -> str:
    return "1D" if diagnostics.get("htf_timeframe") == "1d" else "2D"


def htf_value(diagnostics: Mapping[str, Any], suffix: str = "trend") -> Any:
    timeframe = "1d" if diagnostics.get("htf_timeframe") == "1d" else "2d"
    key = f"candles_{timeframe}_count" if suffix == "count" else f"htf_{timeframe}_{suffix}"
    return diagnostics.get(key, NA)
