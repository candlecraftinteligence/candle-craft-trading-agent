from __future__ import annotations

import pytest

from app.analytics.public_alert_funnel import (
    classify_block_stage,
    normalize_public_block_reasons,
)


@pytest.mark.parametrize(
    ("reason", "category", "stage"),
    (
        ("public_block_scan_cap", "PUBLIC_RATE_LIMIT", "RATE_LIMIT_GATE"),
        ("public_block_hourly_cap", "PUBLIC_RATE_LIMIT", "RATE_LIMIT_GATE"),
        ("public_block_daily_cap", "PUBLIC_RATE_LIMIT", "RATE_LIMIT_GATE"),
        (
            "public_block_same_symbol_same_side_cooldown",
            "PUBLIC_COOLDOWN",
            "COOLDOWN_GATE",
        ),
        (
            "public_block_same_symbol_opposite_side_cooldown",
            "PUBLIC_COOLDOWN",
            "COOLDOWN_GATE",
        ),
    ),
)
def test_public_delivery_rate_limit_and_cooldown_reasons_are_classified(
    reason: str,
    category: str,
    stage: str,
) -> None:
    assert normalize_public_block_reasons(reason) == [category]
    assert classify_block_stage(reason) == stage
